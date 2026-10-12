import concurrent.futures
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from typing import Any

from assistente.terminal import print_response, print_status, read_prompt, show_welcome

DEFAULT_CEREBRO_SERVER_URL = "https://cerebrocentra.com"


def request_json(
    server: str,
    token: str,
    path: str,
    payload: dict[str, Any] | None = None,
    timeout: int = 300,
) -> dict[str, Any]:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        f"{server.rstrip('/')}{path}",
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST" if payload is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def remote_chat() -> None:
    show_welcome()
    server = os.environ.get("CEREBRO_SERVER_URL", DEFAULT_CEREBRO_SERVER_URL).rstrip("/")
    token = os.environ.get("CEREBRO_API_TOKEN", "")
    if not server or not token:
        raise RuntimeError(
            "Configure CEREBRO_SERVER_URL e CEREBRO_API_TOKEN antes de conectar ao servidor."
        )
    session_id = str(uuid.uuid4())
    print_status(f"Conectado ao Cerebro em {server}. Digite /sair para encerrar.")
    try:
        health = request_json(server, token, "/health")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Servidor Cerebro indisponível ({exc.code}).") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Não foi possível conectar ao Cerebro: {exc.reason}") from exc
    print_status(f"Servidor pronto; modelo central: {health.get('model', 'desconhecido')}.")

    while True:
        try:
            message = read_prompt("\nVocê> ")
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if message.strip().lower() in {"/sair", "/exit", "/quit"}:
            break
        if message.strip().lower() == "/hubs":
            try:
                result = request_json(server, token, "/v1/hubs")
            except urllib.error.HTTPError as exc:
                print(f"\nErro ao consultar os hubs ({exc.code}).")
                continue
            except urllib.error.URLError as exc:
                print(f"\nFalha ao consultar os hubs: {exc.reason}")
                continue
            configured = result.get("hubs", [])
            if not configured:
                print("\nNenhum hub externo habilitado no servidor.")
            else:
                for hub in configured:
                    print(f"\n{hub['name']}: {hub['status']}")
            continue
        if message.strip().lower() == "/execucoes":
            try:
                result = request_json(server, token, "/v1/runs")
            except urllib.error.HTTPError as exc:
                print(f"\nErro ao consultar execuções ({exc.code}).")
                continue
            except urllib.error.URLError as exc:
                print(f"\nFalha ao consultar execuções: {exc.reason}")
                continue
            runs = result.get("runs", [])
            if not runs:
                print("\nNenhuma execução registrada no servidor.")
            for run in runs:
                print(
                    f"\n{run['id'][:8]} {run['status']} agente="
                    f"{run.get('agent') or 'não selecionado'} "
                    f"início={run['started_at']}"
                )
            continue
        if message.strip().lower() == "/auditoria-seguranca":
            print(
                "\nA auditoria local não está disponível no modo remoto; "
                "ela inspeciona somente os arquivos do projeto deste computador."
            )
            continue
        if message.strip().lower() == "/auditoria-elevacao":
            print(
                "\nA auditoria de elevação não está disponível no modo remoto; "
                "ela consulta somente o processo local deste computador."
            )
            continue
        if message.strip().lower() == "/hub" or message.strip().lower().startswith("/hub/"):
            path = message.strip()[len("/hub") :].strip("/")
            try:
                result = request_json(
                    server,
                    token,
                    "/v1/hub-directory?" + urllib.parse.urlencode({"path": path}),
                )
            except urllib.error.HTTPError as exc:
                print(f"\nErro ao consultar as plataformas ({exc.code}).")
                continue
            except urllib.error.URLError as exc:
                print(f"\nFalha ao consultar as plataformas: {exc.reason}")
                continue
            print(f"\n{result.get('content', '')}")
            browser_url = result.get("browser_url")
            if isinstance(browser_url, str) and browser_url:
                try:
                    if webbrowser.open(browser_url):
                        print(f"Navegador aberto: {browser_url}")
                    else:
                        print(
                            f"Não foi possível abrir o navegador automaticamente. Acesse: {browser_url}"
                        )
                except webbrowser.Error as exc:
                    print(f"Falha ao abrir o navegador: {exc}. Acesse: {browser_url}")
            continue
        if not message.strip():
            continue

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                request_json,
                server,
                token,
                "/v1/chat",
                {"session_id": session_id, "message": message},
            )
            while not future.done():
                try:
                    pending = request_json(
                        server,
                        token,
                        f"/v1/approvals?session_id={session_id}",
                        timeout=10,
                    ).get("pending", [])
                    for approval in pending:
                        print(f"\nAção solicitada pelo Cerebro: {approval['action']}")
                        approved = input("Autorizar no servidor? [s/N] ").strip().lower() in {
                            "s",
                            "sim",
                            "y",
                            "yes",
                        }
                        request_json(
                            server,
                            token,
                            f"/v1/approvals/{approval['id']}",
                            {"session_id": session_id, "approved": approved},
                            timeout=10,
                        )
                except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
                    pass
                if not future.done():
                    time.sleep(0.5)
            try:
                result = future.result()
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode(errors="replace")
                print(f"\nErro do servidor ({exc.code}): {detail[:1000]}")
                continue
            except urllib.error.URLError as exc:
                print(f"\nFalha de conexão: {exc.reason}")
                continue
        print_response(
            f"Cerebro Central [{str(result.get('run_id') or 'sem-id')[:8]}]:",
            result.get("response", ""),
        )
