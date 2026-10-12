import asyncio
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any

from assistente.workspace import resolve_workspace_paths

PROJECT_ROOT = Path(__file__).resolve().parent.parent
AGENT_PLATFORMS = (
    (
        "GitHub Copilot CLI",
        "Agente personalizado via CLI; autentique com /login-copilot (OAuth no navegador).",
    ),
    (
        "Apple Intelligence",
        "Modelo local do macOS via Foundation Models; requer um Mac compatível com Apple Intelligence.",
    ),
    (
        "Google Antigravity",
        "IDE de agentes do Google; abre no navegador, sem conector direto de execução no Cerebro.",
    ),
    (
        "OpenHands",
        "Servidor de agente local ou hospedado compatível com API OpenAI; requer endpoint e chave.",
    ),
    (
        "OpenRouter",
        "Modelos via API OpenAI compatível; requer OPENROUTER_API_KEY e nome do modelo.",
    ),
    (
        "CrewAI AMP",
        "Crew implantado no AMP; requer URL do crew e token de API.",
    ),
    (
        "AutoGen Studio",
        "Equipe local exportada; requer ambiente Python com autogenstudio e arquivo de equipe.",
    ),
    (
        "Agno",
        "Framework e plataforma de agentes; abre o portal, sem conector direto de execução no Cerebro.",
    ),
    (
        "LangGraph",
        "Framework de agentes LangChain; abre o LangSmith, sem conector direto de execução no Cerebro.",
    ),
    (
        "Microsoft Azure AI Foundry",
        "Portal de agentes do Azure; abre o AI Foundry, sem conector direto de execução no Cerebro.",
    ),
    (
        "Amazon Bedrock",
        "Plataforma de agentes e modelos AWS; abre o console, sem conector direto de execução no Cerebro.",
    ),
    (
        "Dify",
        "Plataforma para criar e publicar agentes; abre o portal, sem conector direto de execução no Cerebro.",
    ),
    (
        "n8n",
        "Automação de workflows e agentes; abre o portal, sem conector direto de execução no Cerebro.",
    ),
    (
        "Mastra",
        "Framework de agentes em TypeScript; abre o portal, sem conector direto de execução no Cerebro.",
    ),
    (
        "Microsoft Agent Framework",
        "Framework de agentes da Microsoft; abre a documentação, sem conector direto de execução no Cerebro.",
    ),
    (
        "MetaGPT",
        "Framework open-source multiagente; abre o repositório, sem conector direto de execução no Cerebro.",
    ),
)
HUB_DIRECTORY = (
    (
        "apple/intelligence",
        "Apple Intelligence",
        "Modelo Foundation Models executado localmente; requer macOS compatível com Apple Intelligence.",
        "apple_foundation_models",
    ),
    (
        "github/copilot",
        "GitHub Copilot CLI",
        "Agente via CLI; login OAuth pelo navegador com /login-copilot.",
        "copilot_cli",
    ),
    (
        "google/gemini",
        "Google Gemini",
        "API compatível com OpenAI; configure endpoint, modelo e GEMINI_API_KEY.",
        "openai_compatible",
    ),
    (
        "google/antigravity",
        "Google Antigravity",
        "IDE de agentes do Google; abre a página oficial. Integração direta com o Cerebro não está disponível.",
        None,
    ),
    (
        "openai",
        "OpenAI",
        "API compatível; configure endpoint, modelo e OPENAI_API_KEY.",
        "openai_compatible",
    ),
    (
        "anthropic/claude",
        "Anthropic Claude",
        "Sem adaptador nativo neste momento; pode ser acessado via OpenRouter.",
        None,
    ),
    (
        "openhands",
        "OpenHands",
        "Servidor de agente local ou hospedado compatível com API OpenAI.",
        "openai_compatible",
    ),
    (
        "openrouter",
        "OpenRouter",
        "Modelos de vários provedores via API compatível; requer chave e modelo.",
        "openai_compatible",
    ),
    (
        "crewai/amp",
        "CrewAI AMP",
        "Crew implantado; requer URL e token de API.",
        "crewai_amp",
    ),
    (
        "microsoft/autogen-studio",
        "AutoGen Studio",
        "Equipe local exportada; requer ambiente Python e arquivo de equipe.",
        "autogen_studio",
    ),
    (
        "agno",
        "Agno",
        "Framework/plataforma de agentes; abre o portal. Conector direto ainda não disponível.",
        None,
    ),
    (
        "langgraph",
        "LangGraph",
        "Framework/plataforma de agentes; abre o LangSmith. Conector direto ainda não disponível.",
        None,
    ),
    (
        "azure",
        "Microsoft Azure AI Foundry",
        "Portal de agentes do Azure; abre o AI Foundry. Conector direto ainda não disponível.",
        None,
    ),
    (
        "aws/bedrock",
        "Amazon Bedrock",
        "Plataforma AWS de modelos e agentes; abre o console. Conector direto ainda não disponível.",
        None,
    ),
    (
        "dify",
        "Dify",
        "Plataforma de agentes e workflows; abre o portal. Conector direto ainda não disponível.",
        None,
    ),
    (
        "n8n",
        "n8n",
        "Plataforma de automação e agentes; abre o portal. Conector direto ainda não disponível.",
        None,
    ),
    (
        "mastra",
        "Mastra",
        "Framework TypeScript de agentes; abre o portal. Conector direto ainda não disponível.",
        None,
    ),
    (
        "microsoft/agent-framework",
        "Microsoft Agent Framework",
        "Framework de agentes; abre a documentação oficial. Conector direto ainda não disponível.",
        None,
    ),
    (
        "metagpt",
        "MetaGPT",
        "Framework open-source multiagente; abre o repositório. Conector direto ainda não disponível.",
        None,
    ),
)
HUBS_CONFIG = resolve_workspace_paths().hubs_config
MAX_PROMPT_LENGTH = 8000
MAX_RESULT_LENGTH = 6000
DEFAULT_TIMEOUT_SECONDS = 300
MAX_HUB_ACCOUNTS = 10
SECRET_PATTERNS = (
    re.compile(
        r"(?i)\b(api[_-]?key|access[_-]?token|auth[_-]?token|token|password|passwd|"
        r"secret|client[_-]?secret|credential|private[_-]?key)"
        r"(\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
    ),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE),
    re.compile(
        r"\b(?:sk-[A-Za-z0-9_-]{16,}|sk_(?:live|test)_[A-Za-z0-9]{16,}|"
        r"rk_live_[A-Za-z0-9]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|"
        r"github_pat_[A-Za-z0-9_]{20,})\b"
    ),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
)


def agent_platforms() -> tuple[tuple[str, str], ...]:
    return AGENT_PLATFORMS


def load_hubs(include_disabled: bool = False) -> list[dict[str, Any]]:
    if not HUBS_CONFIG.exists():
        return []
    try:
        config = json.loads(HUBS_CONFIG.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Configuração de hubs inválida em {HUBS_CONFIG}: {exc}") from exc
    if not isinstance(config, dict) or not isinstance(config.get("hubs"), list):
        raise ValueError(f"A configuração {HUBS_CONFIG} deve conter uma lista 'hubs'.")
    if len(config["hubs"]) > 12:
        raise ValueError("Configure no máximo 12 hubs para limitar custos e chamadas simultâneas.")

    hubs = []
    names = set()
    supported_types = {
        "openai_compatible",
        "copilot_cli",
        "crewai_amp",
        "autogen_studio",
        "apple_foundation_models",
    }
    for index, hub in enumerate(config["hubs"]):
        if not isinstance(hub, dict):
            raise ValueError(f"Hub na posição {index} deve ser um objeto.")
        name = hub.get("name")
        kind = hub.get("type")
        if not isinstance(name, str) or not name.strip() or name in names:
            raise ValueError(f"Nome de hub ausente ou duplicado na posição {index}.")
        if not isinstance(kind, str) or kind not in supported_types:
            raise ValueError(f"Tipo de hub não suportado para '{name}': {kind!r}.")
        if "enabled" in hub and not isinstance(hub["enabled"], bool):
            raise ValueError(f"enabled deve ser booleano para o hub '{name}'.")
        if "accounts" in hub:
            if kind not in {"openai_compatible", "crewai_amp"}:
                raise ValueError(f"accounts não é suportado para o tipo '{kind}' do hub '{name}'.")
            accounts = hub["accounts"]
            if not isinstance(accounts, list) or not 1 <= len(accounts) <= MAX_HUB_ACCOUNTS:
                raise ValueError(
                    f"accounts deve conter de 1 a {MAX_HUB_ACCOUNTS} contas para o hub '{name}'."
                )
            account_names = set()
            for account in accounts:
                if not isinstance(account, dict):
                    raise ValueError(f"Conta inválida para o hub '{name}'.")
                account_name = account.get("name")
                key_env = account.get("api_key_env")
                if (
                    not isinstance(account_name, str)
                    or not account_name.strip()
                    or account_name in account_names
                ):
                    raise ValueError(f"Nome de conta ausente ou duplicado para o hub '{name}'.")
                if not isinstance(key_env, str) or not key_env.strip():
                    raise ValueError(f"Configure api_key_env para cada conta do hub '{name}'.")
                account_names.add(account_name)
        names.add(name)
        if include_disabled or hub.get("enabled", True):
            hubs.append(hub)
    return hubs


def hub_directory(path: str = "") -> str:
    normalized = path.strip().strip("/").lower()
    normalized = {
        "crewia": "crewai/amp",
        "crewai": "crewai/amp",
        "antigravity": "google/antigravity",
    }.get(normalized, normalized)
    if normalized in {"", "hub"}:
        lines = ["Plataformas de agentes disponíveis:"]
        lines.extend(
            f"/hub/{route} — {name}: {description}" for route, name, description, _ in HUB_DIRECTORY
        )
        lines.append("Abra /hub/<plataforma> para detalhes e agentes/modelos.")
        return "\n".join(lines)

    agent_listing = normalized.endswith("/agentes")
    route = normalized.removesuffix("/agentes")
    route = {
        "crewia": "crewai/amp",
        "crewai": "crewai/amp",
        "antigravity": "google/antigravity",
    }.get(route, route)
    platform = next((item for item in HUB_DIRECTORY if item[0] == route), None)
    if platform is None:
        valid_routes = ", ".join(f"/hub/{item[0]}" for item in HUB_DIRECTORY)
        return f"Plataforma não reconhecida. Rotas disponíveis: {valid_routes}"

    _, name, description, supported_type = platform
    lines = [f"{name}: {description}"]
    try:
        configured = [
            hub
            for hub in load_hubs(include_disabled=True)
            if supported_type is not None
            and hub["type"] == supported_type
            and _hub_matches_platform(hub, route)
        ]
    except ValueError as exc:
        return f"{name}\nErro na configuração de hubs: {exc}"

    if not configured:
        lines.append("Status: ainda não configurado em hubs.json.")
    else:
        for hub in configured:
            enabled = hub.get("enabled", True)
            state = "habilitado" if enabled else "desabilitado"
            lines.append(f"Hub '{hub['name']}': {state}.")
            model = hub.get("model")
            if isinstance(model, str) and model:
                lines.append(f"Modelo: {model}")
            agent = hub.get("agent")
            if isinstance(agent, str) and agent:
                lines.append(f"Agente configurado: {agent}")
            account_envs = _account_envs(hub)
            if account_envs:
                configured_accounts = sum(
                    bool(os.environ.get(key_env)) for _, key_env in account_envs
                )
                lines.append(
                    f"Credenciais: {configured_accounts}/{len(account_envs)} contas configuradas."
                )
            if hub.get("type") == "copilot_cli":
                command = hub.get("command", "copilot")
                lines.append(
                    "CLI: disponível."
                    if isinstance(command, str) and shutil.which(command)
                    else "CLI: não encontrado."
                )

    if route == "github/copilot":
        directories: list[Path] = []
        configured_dirs = [
            _configured_path(hub.get("agent_dir"))
            for hub in configured
            if isinstance(hub.get("agent_dir"), str)
        ]
        directories.extend(directory for directory in configured_dirs if directory is not None)
        directories.append(Path.cwd())
        agent_files: dict[str, Path] = {}
        for directory in directories:
            agents_dir = directory / ".github" / "agents"
            if agents_dir.is_dir():
                for agent_file in sorted(agents_dir.glob("*.agent.md")):
                    agent_files.setdefault(agent_file.stem.removesuffix(".agent"), agent_file)
        lines.append("Agentes personalizados encontrados:")
        lines.extend(f"- {agent}" for agent in sorted(agent_files))
        if not agent_files:
            lines.append("- Nenhum em .github/agents/*.agent.md neste diretório.")
    elif supported_type is None:
        lines.append("A integração direta ainda não está disponível.")
    elif supported_type == "openai_compatible":
        models = sorted(
            {
                hub["model"]
                for hub in configured
                if isinstance(hub.get("model"), str) and hub["model"]
            }
        )
        if models:
            lines.append("Modelos configurados: " + ", ".join(models))
        else:
            lines.append("Use /hub/<plataforma>/agentes para ver modelos configurados.")
    elif route == "crewai/amp":
        lines.append("A lista de crews disponíveis é definida pelo serviço AMP.")
    elif route == "microsoft/autogen-studio":
        lines.append("A equipe é definida pelo arquivo team_config da configuração.")
    elif supported_type == "apple_foundation_models":
        lines.append("A disponibilidade do modelo Apple Intelligence é verificada ao chamar.")
    if not agent_listing:
        lines.append(f"Detalhes: /hub/{route}/agentes")
    return "\n".join(lines)


def hub_browser_url(path: str = "") -> str | None:
    normalized = path.strip().strip("/").lower()
    if normalized in {"crewia", "crewai", "crewai/amp"}:
        return "https://app.crewai.com/"
    if normalized in {"antigravity", "google/antigravity"}:
        return "https://antigravity.google/"
    platform_urls = {
        "agno": "https://os.agno.com/",
        "langgraph": "https://smith.langchain.com/",
        "azure": "https://ai.azure.com/",
        "aws/bedrock": "https://console.aws.amazon.com/bedrock/home",
        "dify": "https://cloud.dify.ai/",
        "n8n": "https://app.n8n.cloud/",
        "mastra": "https://cloud.mastra.ai/",
        "microsoft/agent-framework": "https://microsoft.github.io/agent-framework/",
        "metagpt": "https://github.com/FoundationAgents/MetaGPT",
    }
    if normalized in platform_urls:
        return platform_urls[normalized]
    return None


def open_hub_platform(path: str = "") -> str | None:
    url = hub_browser_url(path)
    if url is None:
        return None
    try:
        if webbrowser.open(url):
            return f"Navegador aberto: {url}"
        return f"Não foi possível abrir o navegador automaticamente. Acesse: {url}"
    except webbrowser.Error as exc:
        return f"Falha ao abrir o navegador: {exc}. Acesse: {url}"


def _hub_matches_platform(hub: dict[str, Any], route: str) -> bool:
    name = str(hub.get("name", "")).lower()
    aliases = {
        "apple/intelligence": ("apple-intelligence", "apple"),
        "github/copilot": ("github-copilot", "copilot"),
        "google/gemini": ("gemini",),
        "openai": ("openai",),
        "anthropic/claude": ("claude", "anthropic"),
        "openhands": ("openhands",),
        "openrouter": ("openrouter",),
        "crewai/amp": ("crewai", "amp"),
        "microsoft/autogen-studio": ("autogen",),
    }
    return any(alias in name for alias in aliases.get(route, (route,)))


def hub_statuses() -> list[tuple[str, str]]:
    statuses = []
    for hub in load_hubs():
        name = hub["name"]
        kind = hub["type"]
        base_url = hub.get("base_url", "")
        model = hub.get("model", "")
        if kind in {"openai_compatible", "crewai_amp"} and (
            not isinstance(base_url, str) or "YOUR_" in base_url.upper()
        ):
            statuses.append((name, "URL do endpoint ainda não configurada"))
        elif kind == "openai_compatible" and (
            not isinstance(model, str) or not model or "YOUR_" in model.upper()
        ):
            statuses.append((name, "modelo ainda não configurado"))
        elif kind in {"openai_compatible", "crewai_amp"}:
            try:
                _validate_endpoint_url(base_url, name)
            except ValueError as exc:
                statuses.append((name, str(exc)))
                continue
            accounts = _account_envs(hub)
            configured = sum(bool(os.environ.get(key_env)) for _, key_env in accounts)
            if configured == 0:
                statuses.append((name, "aguardando credenciais de conta"))
            elif configured < len(accounts):
                statuses.append((name, f"{configured}/{len(accounts)} credenciais configuradas"))
            else:
                statuses.append((name, "configurado; autenticação será verificada ao chamar"))
        elif kind == "copilot_cli" and not shutil.which(hub.get("command", "copilot")):
            statuses.append((name, "CLI não encontrado"))
        elif kind == "autogen_studio":
            python = _configured_path(hub.get("python"))
            team_config = _configured_path(hub.get("team_config"))
            if python is None or not python.is_file():
                statuses.append((name, "interpretador Python não encontrado"))
            elif team_config is None or not team_config.is_file():
                statuses.append((name, "arquivo de equipe não encontrado"))
            else:
                statuses.append((name, "configurado"))
        elif kind == "apple_foundation_models":
            if sys.platform != "darwin":
                statuses.append((name, "disponível somente quando o Cérebro roda no macOS"))
            elif not isinstance(hub.get("command", "swift"), str) or not shutil.which(
                hub.get("command", "swift")
            ):
                statuses.append((name, "Swift não encontrado; instale as ferramentas de linha de comando do Xcode"))
            else:
                statuses.append(
                    (name, "macOS detectado; disponibilidade do modelo será verificada ao chamar")
                )
        else:
            statuses.append((name, "configurado; autenticação será verificada ao chamar"))
    return statuses


def login_copilot_cli() -> str:
    command = shutil.which("copilot")
    if not command:
        return "GitHub Copilot CLI não encontrado; instale o CLI antes de autenticar."
    try:
        result = subprocess.run([command, "login", "--web-flow"], check=False)
    except OSError as exc:
        return f"Não foi possível iniciar o login do Copilot: {exc}"
    if result.returncode:
        return f"Login do GitHub Copilot terminou com código {result.returncode}."
    return "Login do GitHub Copilot concluído."


def show_copilot_usage() -> str:
    command = shutil.which("copilot")
    if not command:
        return "GitHub Copilot CLI não encontrado; instale o CLI para consultar o uso."
    print(
        "No Copilot CLI, use /statusline quota para ver o saldo do plano "
        "(créditos de IA ou requisições premium, conforme sua conta) e /usage "
        "para o consumo da sessão atual. Saia do CLI para voltar ao Cerebro."
    )
    try:
        result = subprocess.run([command], check=False)
    except OSError as exc:
        return f"Não foi possível abrir o Copilot CLI: {exc}"
    if result.returncode:
        return f"Copilot CLI terminou com código {result.returncode}."
    return "Consulta do uso encerrada."


class HubHTTPError(RuntimeError):
    def __init__(self, status_code: int):
        self.status_code = status_code
        super().__init__(f"HTTP {status_code} ao chamar o hub.")


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _read_json_response(request: urllib.request.Request, timeout: int) -> dict[str, Any]:
    try:
        opener = urllib.request.build_opener(NoRedirectHandler())
        with opener.open(request, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        raise HubHTTPError(exc.code) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"Falha de conexão com o hub: {exc}") from exc
    try:
        result = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("O hub retornou uma resposta JSON inválida.") from exc
    if not isinstance(result, dict):
        raise RuntimeError("O hub retornou um formato de resposta inválido.")
    return result


def _account_envs(hub: dict[str, Any]) -> list[tuple[str, str]]:
    if "accounts" in hub:
        accounts = hub["accounts"]
        if not isinstance(accounts, list) or not accounts:
            raise ValueError(f"Configure ao menos uma conta para o hub '{hub['name']}'.")
        result = []
        names = set()
        for account in accounts:
            if not isinstance(account, dict):
                raise ValueError(f"Conta inválida para o hub '{hub['name']}'.")
            name = account.get("name")
            key_env = account.get("api_key_env")
            if not isinstance(name, str) or not name.strip() or name in names:
                raise ValueError(f"Nome de conta ausente ou duplicado para o hub '{hub['name']}'.")
            if not isinstance(key_env, str) or not key_env.strip():
                raise ValueError(f"Configure api_key_env para cada conta do hub '{hub['name']}'.")
            result.append((name, key_env))
            names.add(name)
        return result
    key_env = hub.get("api_key_env")
    if isinstance(key_env, str) and key_env:
        return [(hub["name"], key_env)]
    return []


def _available_account_keys(hub: dict[str, Any]) -> list[tuple[str, str, str]]:
    accounts = _account_envs(hub)
    available = [
        (name, key_env, os.environ[key_env])
        for name, key_env in accounts
        if os.environ.get(key_env)
    ]
    if not available:
        missing = ", ".join(key_env for _, key_env in accounts)
        if not missing:
            raise ValueError(f"Configure api_key_env ou accounts para o hub '{hub['name']}'.")
        raise ValueError(f"Variáveis de autenticação não definidas: {missing}.")
    return available


def _api_key(hub: dict[str, Any]) -> str:
    return _available_account_keys(hub)[0][2]


def _timeout(hub: dict[str, Any]) -> int:
    timeout = hub.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    if not isinstance(timeout, int) or not 1 <= timeout <= 1800:
        raise ValueError(f"timeout_seconds inválido para o hub '{hub['name']}'.")
    return timeout


def _validate_endpoint_url(value: Any, hub_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"base_url inválida para o hub '{hub_name}'.")
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError as exc:
        raise ValueError(f"base_url inválida para o hub '{hub_name}'.") from exc
    hostname = parsed.hostname
    is_loopback = hostname == "localhost"
    if hostname and not is_loopback:
        try:
            is_loopback = ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            is_loopback = False
    secure_endpoint = parsed.scheme == "https" and bool(hostname)
    local_endpoint = parsed.scheme == "http" and is_loopback
    if (secure_endpoint or local_endpoint) and parsed.username is None and parsed.password is None:
        return value
    raise ValueError(
        f"base_url do hub '{hub_name}' deve usar HTTPS; HTTP só é permitido em loopback."
    )


def _redact_secrets(prompt: str) -> str:
    redacted = prompt
    for pattern in SECRET_PATTERNS:
        if pattern.groups == 2:
            redacted = pattern.sub(r"\1\2[REDACTED]", redacted)
        else:
            redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def _openai_compatible(hub: dict[str, Any], prompt: str) -> str:
    base_url = _validate_endpoint_url(hub.get("base_url"), hub["name"])
    model = hub.get("model")
    if "YOUR_" in base_url.upper():
        raise ValueError(f"base_url inválida para o hub '{hub['name']}'.")
    if not isinstance(model, str) or not model or "YOUR_" in model.upper():
        raise ValueError(f"Configure model para o hub '{hub['name']}'.")

    url = base_url.rstrip("/")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }
    headers = {
        "Authorization": f"Bearer {_api_key(hub)}",
        "Content-Type": "application/json",
    }
    extra_headers = hub.get("headers", {})
    if not isinstance(extra_headers, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in extra_headers.items()
    ):
        raise ValueError(f"headers inválidos para o hub '{hub['name']}'.")
    if any(key.lower() == "authorization" for key in extra_headers):
        raise ValueError(
            f"Authorization não pode ser substituído em headers do hub '{hub['name']}'."
        )
    timeout = _timeout(hub)
    result = None
    last_auth_error = None
    auth_scheme = "Bearer"
    for _, _, api_key in _available_account_keys(hub):
        headers["Authorization"] = f"{auth_scheme} {api_key}"
        headers.update(extra_headers)
        headers["Authorization"] = f"Bearer {api_key}"
        headers["Authorization"] = f"{auth_scheme} {api_key}"
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers=headers,
            method="POST",
        )
        try:
            result = _read_json_response(request, timeout)
            break
        except HubHTTPError as exc:
            if exc.status_code != 401:
                raise
            last_auth_error = exc
    if result is None:
        if last_auth_error is not None:
            raise RuntimeError("Todas as credenciais configuradas foram recusadas (HTTP 401).")
        raise RuntimeError(f"Nenhuma credencial disponível para o hub '{hub['name']}'.")
    try:
        content = result["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("O hub não retornou texto em choices[0].message.content.") from exc
    if isinstance(content, list):
        content = "\n".join(
            part["text"]
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("O hub retornou uma resposta vazia.")
    return content.strip()


def _copilot_cli(hub: dict[str, Any], prompt: str) -> str:
    command = hub.get("command", "copilot")
    if not isinstance(command, str) or not shutil.which(command):
        raise ValueError(f"CLI do hub '{hub['name']}' não encontrado: {command!r}.")
    args = [command, "--prompt", prompt, "--silent", "--deny-tool", "*"]
    agent = hub.get("agent")
    if agent is not None:
        if not isinstance(agent, str) or not agent.strip():
            raise ValueError(f"Configure um nome de agente válido para o hub '{hub['name']}'.")
        args.extend(["--agent", agent.strip()])
    agent_dir = hub.get("agent_dir")
    if agent_dir is not None:
        agent_path = _configured_path(agent_dir)
        if agent_path is None or not agent_path.is_dir():
            raise ValueError(f"Configure um diretório de agentes existente para '{hub['name']}'.")
        args.extend(["--add-dir", str(agent_path)])
    model = hub.get("model")
    if model:
        args.extend(["--model", model])
    try:
        result = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            timeout=_timeout(hub),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Tempo limite excedido no hub '{hub['name']}'.") from exc
    if result.returncode:
        detail = result.stderr.strip() or f"processo terminou com código {result.returncode}"
        raise RuntimeError(f"Falha no hub '{hub['name']}': {detail[:500]}")
    if not result.stdout.strip():
        raise RuntimeError(f"O hub '{hub['name']}' retornou uma resposta vazia.")
    return result.stdout.strip()


def _crewai_amp(hub: dict[str, Any], prompt: str) -> str:
    base_url = _validate_endpoint_url(hub.get("base_url"), hub["name"])
    if "YOUR_" in base_url.upper():
        raise ValueError(f"base_url inválida para o hub '{hub['name']}'.")
    inputs = hub.get("inputs", {"task": "{prompt}"})
    if not isinstance(inputs, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in inputs.items()
    ):
        raise ValueError(f"inputs inválidos para o hub '{hub['name']}'.")
    rendered_inputs = {key: value.replace("{prompt}", prompt) for key, value in inputs.items()}
    headers = {
        "Authorization": f"Bearer {_api_key(hub)}",
        "Content-Type": "application/json",
    }
    timeout = _timeout(hub)
    kickoff = None
    selected_key = None
    last_auth_error = None
    auth_scheme = "Bearer"
    for _, _, api_key in _available_account_keys(hub):
        headers["Authorization"] = f"Bearer {api_key}"
        headers["Authorization"] = f"{auth_scheme} {api_key}"
        kickoff_request = urllib.request.Request(
            f"{base_url.rstrip('/')}/kickoff",
            data=json.dumps({"inputs": rendered_inputs}).encode(),
            headers=headers,
            method="POST",
        )
        try:
            kickoff = _read_json_response(kickoff_request, timeout)
            selected_key = api_key
            break
        except HubHTTPError as exc:
            if exc.status_code != 401:
                raise
            last_auth_error = exc
    if kickoff is None or selected_key is None:
        if last_auth_error is not None:
            raise RuntimeError("Todas as credenciais configuradas foram recusadas (HTTP 401).")
        raise RuntimeError(f"Nenhuma credencial disponível para o hub '{hub['name']}'.")
    kickoff_id = kickoff.get("kickoff_id")
    if not isinstance(kickoff_id, str) or not kickoff_id:
        raise RuntimeError("CrewAI AMP não retornou kickoff_id.")

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status_request = urllib.request.Request(
            f"{base_url.rstrip('/')}/status/{kickoff_id}",
            headers={"Authorization": f"Bearer {selected_key}"},
        )
        status_request.add_header("Authorization", f"{auth_scheme} {selected_key}")
        status = _read_json_response(status_request, min(timeout, 30))
        state = str(status.get("state", "")).upper()
        if state in {"SUCCESS", "COMPLETED"}:
            output = status.get("result")
            if output is None:
                output = status.get("result_json")
            if isinstance(output, (dict, list)):
                output = json.dumps(output, ensure_ascii=False)
            if isinstance(output, str) and output.strip():
                return output.strip()
            raise RuntimeError("CrewAI AMP concluiu sem retornar resultado.")
        if state in {"FAILED", "REVOKED"}:
            raise RuntimeError(f"CrewAI AMP encerrou a tarefa com estado {state}.")
        time.sleep(min(2, max(0, deadline - time.monotonic())))
    raise RuntimeError(f"Tempo limite excedido aguardando CrewAI AMP ({kickoff_id}).")


def _autogen_studio(hub: dict[str, Any], prompt: str) -> str:
    python = hub.get("python")
    team_config = hub.get("team_config")
    python_path = _configured_path(python)
    team_path = _configured_path(team_config)
    if python_path is None or not python_path.is_file():
        raise ValueError(f"Configure o caminho do Python do AutoGen Studio para '{hub['name']}'.")
    if team_path is None or not team_path.is_file():
        raise ValueError(f"Configure um arquivo de equipe existente para '{hub['name']}'.")
    script = (
        "import contextlib, json, sys\n"
        "from autogenstudio.teammanager import TeamManager\n"
        "payload = json.load(sys.stdin)\n"
        "with contextlib.redirect_stdout(sys.stderr):\n"
        "    result = TeamManager().run(task=payload['task'], team_config=payload['team_config'])\n"
        "print(json.dumps({'result': str(result)}))\n"
    )
    try:
        result = subprocess.run(
            [str(python_path), "-c", script],
            input=json.dumps({"task": prompt, "team_config": str(team_path)}),
            capture_output=True,
            text=True,
            timeout=_timeout(hub),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Tempo limite excedido no hub '{hub['name']}'.") from exc
    if result.returncode:
        detail = result.stderr.strip() or f"processo terminou com código {result.returncode}"
        raise RuntimeError(f"Falha no hub '{hub['name']}': {detail[:500]}")
    try:
        output = json.loads(result.stdout)["result"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RuntimeError("AutoGen Studio retornou uma resposta inválida.") from exc
    if not isinstance(output, str) or not output.strip():
        raise RuntimeError("AutoGen Studio retornou uma resposta vazia.")
    return output.strip()


def _apple_foundation_models(hub: dict[str, Any], prompt: str) -> str:
    if sys.platform != "darwin":
        raise RuntimeError("Apple Intelligence só está disponível quando o Cérebro roda no macOS.")
    command = hub.get("command", "swift")
    executable = shutil.which(command) if isinstance(command, str) else None
    if executable is None:
        raise ValueError(f"Swift não encontrado para o hub '{hub['name']}'.")
    runner = Path(__file__).with_name("apple_foundation_models.swift")
    if not runner.is_file():
        raise RuntimeError("O adaptador Swift do Apple Intelligence não foi encontrado.")
    try:
        result = subprocess.run(
            [executable, str(runner)],
            input=prompt,
            check=False,
            capture_output=True,
            text=True,
            timeout=_timeout(hub),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Tempo limite excedido no hub '{hub['name']}'.") from exc
    if result.returncode:
        detail = result.stderr.strip() or f"processo terminou com código {result.returncode}"
        raise RuntimeError(f"Falha no hub '{hub['name']}': {detail[:400]}")
    if not result.stdout.strip():
        raise RuntimeError(f"O hub '{hub['name']}' retornou uma resposta vazia.")
    return result.stdout.strip()


def _configured_path(value: Any) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    path = Path(os.path.expandvars(os.path.expanduser(value)))
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


def invoke_hub(hub: dict[str, Any], prompt: str) -> dict[str, str]:
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MAX_PROMPT_LENGTH:
        raise ValueError("A tarefa enviada aos hubs está vazia ou excede o limite.")
    safe_prompt = _redact_secrets(prompt)
    try:
        if hub["type"] == "openai_compatible":
            answer = _openai_compatible(hub, safe_prompt)
        elif hub["type"] == "copilot_cli":
            answer = _copilot_cli(hub, safe_prompt)
        elif hub["type"] == "crewai_amp":
            answer = _crewai_amp(hub, safe_prompt)
        elif hub["type"] == "autogen_studio":
            answer = _autogen_studio(hub, safe_prompt)
        elif hub["type"] == "apple_foundation_models":
            answer = _apple_foundation_models(hub, safe_prompt)
        else:
            raise ValueError(f"Tipo de hub desconhecido: {hub['type']}")
    except (OSError, RuntimeError, ValueError) as exc:
        return {"name": hub["name"], "status": "error", "content": str(exc)[:500]}
    return {
        "name": hub["name"],
        "status": "ok",
        "content": answer[:MAX_RESULT_LENGTH],
    }


async def collect_hub_votes(prompt: str, role: str) -> list[dict[str, str]]:
    hubs = load_hubs()
    if not hubs:
        return []
    prefix = (
        f"Função do agente: {role}\n"
        "Responda à tarefa diretamente e de forma concisa. Não execute ações nem "
        "presuma que pode alterar arquivos, aplicativos ou serviços.\n\n"
        "Tarefa do usuário:\n"
    )
    task = prefix + prompt[: max(0, MAX_PROMPT_LENGTH - len(prefix))]
    votes = await asyncio.gather(*(asyncio.to_thread(invoke_hub, hub, task) for hub in hubs))
    for vote in votes:
        if vote["status"] == "ok":
            print(f"[Hub {vote['name']}: resposta recebida]")
        else:
            print(f"[Hub {vote['name']}: erro — {vote['content']}]")
    return votes
