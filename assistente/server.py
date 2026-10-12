import asyncio
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
import uuid
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request

from assistente.__main__ import (
    CEREBRO_SYSTEM_PROMPT,
    GRAPH_RECURSION_LIMIT,
    MODEL,
    PROJECT_ROOT,
    TOOLS,
    USER_DATA,
    BrowserPlugin,
    GrindrModeState,
    _grindr_background_monitor,
    build_chat_graph,
    connect_mcp,
    ensure_model,
    invoke_tool,
    parse_grindr_control_request,
    prepare_auto_improve_command,
)
from assistente.hubs import hub_browser_url, hub_directory, hub_statuses
from assistente.project_processes import ProjectProcessManager
from assistente.runtime import RuntimeStore, trim_context
from assistente.workspace import WORKSPACE_PATHS, prepare_workspace

API_TOKEN = os.environ.get("CEREBRO_API_TOKEN", "")
IMPROVEMENTS_UPLOAD_TOKEN = os.environ.get("CEREBRO_IMPROVEMENTS_UPLOAD_TOKEN", "")
MAX_MESSAGE_LENGTH = 8000
MAX_IMPROVEMENT_REPORT_BYTES = 4096
IMPROVEMENT_UPLOAD_MIN_INTERVAL_SECONDS = 60
APPROVAL_TIMEOUT_SECONDS = 180
ENABLE_DESKTOP = os.environ.get("CEREBRO_ENABLE_DESKTOP", "0") == "1"
IMPROVEMENT_REPORT_FIELDS = {
    "schema_version",
    "report_id",
    "check",
    "status",
    "files_checked",
    "diagnostics_count",
}


class ApprovalBroker:
    def __init__(self) -> None:
        self.pending: dict[str, dict[str, Any]] = {}

    async def request(self, session_id: str, action: str) -> bool:
        action_id = secrets.token_urlsafe(18)
        future = asyncio.get_running_loop().create_future()
        self.pending[action_id] = {
            "session_id": session_id,
            "action": action,
            "future": future,
        }
        print(f"Aguardando aprovação do cliente para: {action}", flush=True)
        try:
            return await asyncio.wait_for(future, APPROVAL_TIMEOUT_SECONDS)
        except TimeoutError:
            print(f"Aprovação expirada: {action_id}", flush=True)
            return False
        finally:
            self.pending.pop(action_id, None)


approval_broker = ApprovalBroker()


@asynccontextmanager
async def lifespan(app: FastAPI):
    if len(API_TOKEN) < 32:
        raise RuntimeError(
            "Configure CEREBRO_API_TOKEN com um segredo aleatório de pelo menos 32 caracteres."
        )
    await asyncio.to_thread(ensure_model, False)
    prepare_workspace(WORKSPACE_PATHS)
    stack = AsyncExitStack()
    try:
        mcp_tools, mcp_routes = await connect_mcp(stack)
        app.state.mcp_routes = mcp_routes
        app.state.mcp_tools = mcp_tools
        app.state.sessions = {}
        app.state.session_locks = {}
        app.state.browsers = {}
        app.state.project_processes = ProjectProcessManager(PROJECT_ROOT)
        app.state.runtime_store = RuntimeStore(USER_DATA / "runtime.sqlite3")
        grindr_modes: GrindrModeState = {
            "automatico": False,
            "treinamento": False,
            "generation": 0,
        }
        app.state.grindr_modes = grindr_modes
        app.state.grindr_monitor = (
            asyncio.create_task(_grindr_background_monitor(app.state.runtime_store, grindr_modes))
            if ENABLE_DESKTOP
            else None
        )
        yield
    finally:
        grindr_monitor = getattr(app.state, "grindr_monitor", None)
        if grindr_monitor is not None:
            grindr_monitor.cancel()
            await asyncio.gather(grindr_monitor, return_exceptions=True)
        if hasattr(app.state, "project_processes"):
            await asyncio.to_thread(app.state.project_processes.close)
        for browser in getattr(app.state, "browsers", {}).values():
            await asyncio.to_thread(browser.close)
        await stack.aclose()


app = FastAPI(title="Cerebro API", version="1.0.0", lifespan=lifespan)


def require_auth(authorization: str | None) -> str:
    if not API_TOKEN or not authorization:
        raise HTTPException(status_code=401, detail="Token de acesso obrigatório.")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(token, API_TOKEN):
        raise HTTPException(status_code=401, detail="Token de acesso inválido.")
    return token


def require_improvements_upload_auth(authorization: str | None) -> str:
    if len(IMPROVEMENTS_UPLOAD_TOKEN) < 32 or not authorization:
        raise HTTPException(status_code=401, detail="Credencial de upload obrigatória.")
    scheme, _, token = authorization.partition(" ")
    try:
        valid = hmac.compare_digest(token, IMPROVEMENTS_UPLOAD_TOKEN)
    except TypeError:
        valid = False
    if scheme.lower() != "bearer" or not valid:
        raise HTTPException(status_code=401, detail="Credencial de upload inválida.")
    return token


def _store_improvement_report(report: dict[str, Any], upload_token: str) -> bool:
    USER_DATA.mkdir(parents=True, exist_ok=True)
    database = USER_DATA / "improvements.sqlite3"
    token_fingerprint = hashlib.sha256(upload_token.encode("utf-8")).hexdigest()
    connection = sqlite3.connect(database, timeout=10)
    try:
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS improvement_reports (
                report_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                check_name TEXT NOT NULL,
                status TEXT NOT NULL,
                files_checked INTEGER NOT NULL,
                diagnostics_count INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS improvement_upload_limits (
                token_fingerprint TEXT PRIMARY KEY,
                uploaded_at REAL NOT NULL
            )
            """
        )
        connection.execute("BEGIN IMMEDIATE")
        with connection:
            existing = connection.execute(
                "SELECT 1 FROM improvement_reports WHERE report_id = ?",
                (report["report_id"],),
            ).fetchone()
            if existing:
                return False
            previous = connection.execute(
                "SELECT uploaded_at FROM improvement_upload_limits WHERE token_fingerprint = ?",
                (token_fingerprint,),
            ).fetchone()
            now = time.time()
            if previous and now - previous[0] < IMPROVEMENT_UPLOAD_MIN_INTERVAL_SECONDS:
                raise HTTPException(
                    status_code=429,
                    detail="Aguarde antes de enviar outro relatório.",
                    headers={
                        "Retry-After": str(
                            max(
                                1,
                                int(IMPROVEMENT_UPLOAD_MIN_INTERVAL_SECONDS - (now - previous[0])),
                            )
                        )
                    },
                )
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO improvement_reports
                    (report_id, check_name, status, files_checked, diagnostics_count)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    report["report_id"],
                    report["check"],
                    report["status"],
                    report["files_checked"],
                    report["diagnostics_count"],
                ),
            )
            if cursor.rowcount == 0:
                return False
            connection.execute(
                """
                INSERT INTO improvement_upload_limits (token_fingerprint, uploaded_at)
                VALUES (?, ?)
                ON CONFLICT(token_fingerprint) DO UPDATE SET uploaded_at = excluded.uploaded_at
                """,
                (token_fingerprint, now),
            )
            connection.execute(
                """
                DELETE FROM improvement_reports
                WHERE report_id NOT IN (
                    SELECT report_id FROM improvement_reports
                    ORDER BY created_at DESC, rowid DESC
                    LIMIT 1000
                )
                """
            )
        if os.name != "nt":
            database.chmod(0o600)
        return True
    finally:
        connection.close()


def session_key(token: str, session_id: str) -> str:
    return f"{token[:8]}:{session_id}"


def grindr_control_response(message: str, modes: GrindrModeState) -> str | None:
    normalized = message.strip().casefold()
    if normalized in {"/grindr", "/grindr status"}:
        availability = (
            "disponível neste servidor"
            if ENABLE_DESKTOP
            else "indisponível: desktop Android desabilitado no servidor"
        )
        if normalized == "/grindr":
            return (
                "Comandos: /grindr status | /grindr automatico ativar|desativar | "
                "/grindr treinamento ativar|desativar. "
                f"Monitor Android: {availability}."
            )
        return (
            f"Rascunhos automáticos: "
            f"{'ativados' if modes['automatico'] else 'desativados'}; "
            f"treinamento: {'ativado' if modes['treinamento'] else 'desativado'}. "
            f"Monitor Android: {availability}."
        )

    request = parse_grindr_control_request(message)
    if request is None:
        return None
    mode, enabled = request
    if enabled and not ENABLE_DESKTOP:
        return (
            "Treinamento não ativado: o monitor Android está desabilitado neste servidor. "
            "Configure CEREBRO_ENABLE_DESKTOP=1 no servidor confiável e reinicie a API."
        )
    current_value = modes["automatico"] if mode == "automatico" else modes["treinamento"]
    if current_value != enabled:
        if mode == "automatico":
            modes["automatico"] = enabled
        else:
            modes["treinamento"] = enabled
        modes["generation"] += 1
    if mode == "automatico":
        if enabled:
            return (
                "Rascunhos automáticos ativados para novas mensagens. "
                "Nenhuma resposta será enviada sem aprovação explícita."
            )
        return "Rascunhos automáticos desativados."
    return (
        "Treinamento ativado: prévias da caixa de entrada ensinam somente categorias genéricas; "
        "conversas abertas ensinam pares genéricos de mensagem/resposta."
        if enabled
        else "Treinamento desativado."
    )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "model": MODEL}


@app.post("/v1/improvements")
async def upload_improvement_report(
    request: Request,
    authorization: str | None = Header(default=None),
):
    upload_token = require_improvements_upload_auth(authorization)
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > MAX_IMPROVEMENT_REPORT_BYTES:
                raise HTTPException(status_code=413, detail="Relatório excede o limite permitido.")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Content-Length inválido.") from exc
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_IMPROVEMENT_REPORT_BYTES:
            raise HTTPException(status_code=413, detail="Relatório excede o limite permitido.")
    try:
        report = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="JSON inválido.") from exc
    if not isinstance(report, dict) or set(report) != IMPROVEMENT_REPORT_FIELDS:
        raise HTTPException(status_code=400, detail="Formato de relatório inválido.")
    if (
        isinstance(report["schema_version"], bool)
        or not isinstance(report["schema_version"], int)
        or report["schema_version"] != 1
        or not isinstance(report["check"], str)
        or report["check"] != "python_syntax"
    ):
        raise HTTPException(status_code=400, detail="Versão ou verificação não suportada.")
    if not isinstance(report["report_id"], str):
        raise HTTPException(status_code=400, detail="report_id inválido.")
    try:
        if (
            len(report["report_id"]) != 36
            or str(uuid.UUID(report["report_id"])) != report["report_id"]
        ):
            raise ValueError
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="report_id inválido.") from exc
    if not isinstance(report["status"], str) or report["status"] not in {
        "clean",
        "issues",
        "incomplete",
    }:
        raise HTTPException(status_code=400, detail="status inválido.")
    for field in ("files_checked", "diagnostics_count"):
        value = report[field]
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2500:
            raise HTTPException(status_code=400, detail=f"{field} inválido.")
    if (report["status"] == "clean") != (report["diagnostics_count"] == 0):
        raise HTTPException(status_code=400, detail="status não corresponde aos diagnósticos.")
    try:
        accepted = await asyncio.to_thread(_store_improvement_report, report, upload_token)
    except HTTPException:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise HTTPException(
            status_code=500,
            detail="Não foi possível armazenar o relatório.",
        ) from exc
    return {"status": "accepted" if accepted else "duplicate", "report_id": report["report_id"]}


@app.get("/v1/hubs")
async def hubs(authorization: str | None = Header(default=None)):
    require_auth(authorization)
    try:
        statuses = hub_statuses()
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"hubs": [{"name": name, "status": status} for name, status in statuses]}


@app.get("/v1/runs")
async def recent_runs(
    request: Request,
    limit: int = 20,
    authorization: str | None = Header(default=None),
):
    require_auth(authorization)
    if not 1 <= limit <= 100:
        raise HTTPException(status_code=400, detail="limit deve estar entre 1 e 100.")
    return {"runs": request.app.state.runtime_store.recent_runs(limit)}


@app.get("/v1/runs/{run_id}/events")
async def run_events(
    run_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
):
    require_auth(authorization)
    if len(run_id) != 32 or any(char not in "0123456789abcdef" for char in run_id):
        raise HTTPException(status_code=404, detail="Execução não encontrada.")
    return {"events": request.app.state.runtime_store.run_events(run_id)}


@app.get("/v1/hub-directory")
async def hub_directory_page(
    path: str = "",
    authorization: str | None = Header(default=None),
):
    require_auth(authorization)
    return {
        "content": hub_directory(path),
        "browser_url": hub_browser_url(path),
    }


@app.get("/v1/tools")
async def list_tools(
    request: Request,
    authorization: str | None = Header(default=None),
):
    require_auth(authorization)
    return {
        "tools": [
            tool
            for tool in TOOLS
            if ENABLE_DESKTOP or not tool["function"]["name"].startswith("desktop_")
        ]
        + list(request.app.state.mcp_tools.values())
    }


@app.post("/v1/tools/execute")
async def execute_tool(
    request: Request,
    authorization: str | None = Header(default=None),
):
    token = require_auth(authorization)
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="JSON inválido.") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="O corpo deve ser um objeto JSON.")
    name = body.get("name")
    args = body.get("arguments", {})
    session_id = body.get("session_id", "direct-tools")
    if not isinstance(name, str) or not name.strip():
        raise HTTPException(status_code=400, detail="Nome da ferramenta obrigatório.")
    if not isinstance(args, dict):
        raise HTTPException(status_code=400, detail="arguments deve ser um objeto JSON.")

    key = session_key(token, session_id)
    browser = request.app.state.browsers.setdefault(key, BrowserPlugin())

    async def approve(action: str) -> bool:
        return await approval_broker.request(session_id, action)

    try:
        output = await invoke_tool(
            name,
            args,
            request.app.state.mcp_routes,
            approve=approve,
            browser=browser,
            process_manager=request.app.state.project_processes,
        )
        return {"output": output}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Erro ao executar {name}: {exc}") from exc


@app.post("/v1/chat")
async def chat(request: Request, authorization: str | None = Header(default=None)):
    token = require_auth(authorization)
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="JSON inválido.") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="O corpo deve ser um objeto JSON.")
    session_id = body.get("session_id")
    message = body.get("message")
    if not isinstance(session_id, str) or not 1 <= len(session_id) <= 100:
        raise HTTPException(status_code=400, detail="session_id inválido.")
    if not isinstance(message, str) or not message.strip() or len(message) > MAX_MESSAGE_LENGTH:
        raise HTTPException(status_code=400, detail="Mensagem inválida ou longa demais.")

    try:
        prepared_auto_improve = prepare_auto_improve_command(message)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"Comando /autoaprimorar inválido: {exc}") from exc
    if prepared_auto_improve is not None:
        message = prepared_auto_improve

    control_response = grindr_control_response(message, request.app.state.grindr_modes)
    if control_response is not None:
        return {"response": control_response, "agent": "controle", "run_id": ""}

    key = session_key(token, session_id)
    lock = request.app.state.session_locks.setdefault(key, asyncio.Lock())
    async with lock:
        messages = request.app.state.sessions.setdefault(
            key,
            [
                {
                    "role": "system",
                    "content": (
                        CEREBRO_SYSTEM_PROMPT
                        + " O runtime remoto é o Cérebro Central; agentes especialistas são "
                        "capacidades internas coordenadas por ele. Ações do navegador, desktop, "
                        "MCP e comandos não rotineiros exigem aprovação explícita do usuário remoto."
                    ),
                }
            ],
        )
        messages.append({"role": "user", "content": message.strip()})
        messages = trim_context(messages)
        run_store = request.app.state.runtime_store
        run_id = run_store.start_run("remote")

        def record_event(
            event: str,
            component: str,
            status: str,
            agent: str | None = None,
            tool: str | None = None,
        ) -> None:
            run_store.record_event(run_id, event, component, status, agent, tool)

        async def approve(action: str) -> bool:
            return await approval_broker.request(session_id, action)

        browser = request.app.state.browsers.setdefault(key, BrowserPlugin())
        # Atualiza o handler de aprovação no ciclo da conversa, sem permitir
        # que a sessão dependa de entrada interativa no servidor.
        workflow = build_chat_graph(
            request.app.state.mcp_routes,
            [
                tool
                for tool in TOOLS
                if ENABLE_DESKTOP or not tool["function"]["name"].startswith("desktop_")
            ]
            + list(request.app.state.mcp_tools.values()),
            approve,
            browser,
            event_callback=record_event,
            process_manager=request.app.state.project_processes,
            runtime_store=run_store,
        )
        try:
            result = await workflow.ainvoke(
                {"messages": messages, "agent": ""},
                config={"recursion_limit": GRAPH_RECURSION_LIMIT},
            )
        except Exception as exc:
            run_store.finish_run(run_id, "failed")
            raise HTTPException(status_code=502, detail=f"Falha ao executar agente: {exc}") from exc
        request.app.state.sessions[key] = result["messages"]
        run_store.finish_run(run_id, "completed")
        return {
            "response": result["messages"][-1].get("content", ""),
            "agent": result.get("agent", "geral"),
            "run_id": run_id,
        }


@app.get("/v1/approvals")
async def approvals(
    session_id: str,
    authorization: str | None = Header(default=None),
):
    require_auth(authorization)
    return {
        "pending": [
            {"id": action_id, "action": item["action"]}
            for action_id, item in approval_broker.pending.items()
            if item["session_id"] == session_id
        ]
    }


@app.post("/v1/approvals/{action_id}")
async def decide_approval(
    action_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
):
    require_auth(authorization)
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="JSON inválido.") from exc
    item = approval_broker.pending.get(action_id)
    if item is None or item["session_id"] != body.get("session_id"):
        raise HTTPException(status_code=404, detail="Aprovação não encontrada.")
    if not isinstance(body.get("approved"), bool):
        raise HTTPException(status_code=400, detail="approved deve ser booleano.")
    if not item["future"].done():
        item["future"].set_result(body["approved"])
    return {"status": "approved" if body["approved"] else "rejected"}
