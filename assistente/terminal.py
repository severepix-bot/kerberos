import getpass
import os
import shutil
import socket
import sys
import textwrap
import unicodedata
from typing import TextIO

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style

from assistente.hubs import HUB_DIRECTORY

_CYAN = "\033[36m"
_TITLE_CYAN = "\033[1;38;5;51m"
_YELLOW = "\033[1;33m"
_CYAN_TEXT = "\033[96m"
_RESET = "\033[0m"
MAX_TERMINAL_MESSAGE_CHARS = 8000
_SLASH_COMMANDS = (
    ("/projeto", "Identificar a pasta de projeto ativa para alterações"),
    ("/ajuda projeto", "Fluxo guiado para trabalhar no projeto"),
    ("/ajuda navegador", "Fluxo guiado para usar o navegador"),
    ("/ajuda integracoes", "Fluxo guiado para usar MCP e integrações"),
    ("/ajuda terminal", "Fluxo guiado para executar comandos e processos"),
    ("/ajuda seguranca", "Permissões e aprovações"),
    ("/ajuda memoria", "Memória e busca local"),
    ("/ferramentas projeto", "Ferramentas do projeto"),
    ("/ferramentas terminal", "Ferramentas de terminal"),
    ("/ferramentas navegador", "Ferramentas do navegador"),
    ("/ferramentas desktop", "Ferramentas de desktop e Android"),
    ("/ferramentas mcp", "Ferramentas MCP conectadas"),
    ("/ferramentas local", "Outras ferramentas locais"),
    ("/provedores", "Listar provedores disponíveis"),
    ("/hubs", "Status dos hubs habilitados"),
    ("/login-copilot", "Autenticar no GitHub Copilot"),
    ("/mcp", "Autenticar servidores MCP com OAuth ou CLI"),
    ("/mcp login github", "Autenticar o MCP do GitHub via CLI"),
    ("/mcp login stripe", "Autenticar o MCP Stripe com OAuth no navegador"),
    ("/mcp login cloudflare", "Autenticar o MCP Cloudflare com OAuth no navegador"),
    ("/status", "Consultar o status do assistente"),
    ("/ferramentas", "Listar ferramentas"),
    ("/execucoes", "Consultar histórico operacional local"),
    ("/plugins", "Inventário e configuração de servidores MCP"),
    ("/plugins adicionar", "Adicionar um servidor MCP ao perfil do usuário"),
    ("/skills", "Listar Skills do projeto"),
    ("/seguranca", "Ver escopo de cibersegurança defensiva"),
    ("/auditoria-seguranca", "Auditar arquivos locais com verificações heurísticas"),
    ("/autoauditoria", "Auditar e corrigir problemas confirmados no projeto"),
    ("/autoaprimorar", "Pesquisar novidades e propor melhorias do projeto"),
    ("/auditoria-elevacao", "Consultar somente o nível de elevação do processo atual"),
    ("/rag", "Consultar o índice RAG local e suas fontes permitidas"),
    ("/aprendizado", "Ver notas compactas aprendidas e seu uso"),
    ("/aprendizado guardar", "Guardar uma orientação na memória de longo prazo"),
    ("/memoria", "Acessar a memória"),
    ("/exportar", "Exportar dados"),
    ("/ajuda", "Mostrar ajuda"),
    ("/limpar", "Limpar a conversa"),
    ("/sair", "Encerrar o Cerebro"),
    ("/hub", "Listar hubs e provedores"),
    ("/plataformas", "Listar integrações"),
    ("/copilot-uso", "Consultar cota e uso do Copilot"),
)
_MCP_PLUGIN_OPTIONS = (
    ("stripe", "Stripe — pagamentos e clientes; exige STRIPE_SECRET_KEY."),
    ("github", "GitHub — repositórios e issues; exige GITHUB_PERSONAL_ACCESS_TOKEN."),
    ("google-workspace", "Google Workspace — Gmail, Drive e Calendar; exige OAuth."),
    ("microsoft", "Microsoft Work IQ — contexto de Microsoft 365; exige login e consentimento."),
    ("cloudflare", "Cloudflare — Workers e recursos da conta; exige Wrangler e account ID."),
    ("apple-photos", "Apple Photos — apenas macOS; login explícito e leitura padrão."),
    ("next-devtools", "Next.js DevTools — ferramentas de diagnóstico para Next.js."),
    ("arquivos-do-projeto", "Arquivos do projeto — acesso restrito à raiz atual."),
    ("memoria-local", "Memória local — armazenamento local do Cérebro."),
    ("terminal", "Terminal MCP — verificações seguras dentro da raiz do projeto."),
    ("computer-use", "Controle de computador nativo — ferramentas desktop sujeitas a aprovação."),
    ("navegador-use", "Navegador Playwright nativo — já integrado, sem servidor MCP adicional."),
)
_HELP_TOPICS = {
    "projeto": (
        "PROJETO — identifique a raiz com /projeto. Depois peça, por exemplo: "
        "“inspecione o projeto e encontre onde a autenticação é configurada”, "
        "“corrija o erro nos testes e valide” ou “explique este arquivo”. "
        "O Cérebro inspeciona antes de editar e executa as verificações adequadas. "
        "Para usar outra pasta, configure CEREBRO_PROJECT_ROOT com um caminho absoluto "
        "antes de iniciar o assistente."
    ),
    "navegador": (
        "NAVEGADOR — peça em linguagem natural para abrir uma página, localizar "
        "informações ou interagir com um site. O Cérebro examina a tela entre etapas; "
        "ações que alteram dados aguardam aprovação."
    ),
    "integracoes": (
        "INTEGRAÇÕES — use /plugins para ver MCPs conectados e /ferramentas mcp "
        "para listar suas ferramentas. Peça a ação em linguagem natural; cada chamada "
        "MCP solicita confirmação. Integrações não conectadas podem depender de credenciais "
        "no ambiente do servidor."
    ),
    "terminal": (
        "TERMINAL — peça para executar um comando ou iniciar um processo do projeto. "
        "Verificações conhecidas podem rodar diretamente; comandos fora da lista segura, "
        "instalações e deploys exigem aprovação. Para acompanhar processos, peça para "
        "iniciar e depois consultar ou encerrar pelo ID. Para uma VM, use um alias já "
        "configurado em ~/.ssh/config; para o Google Cloud Shell, use a conta gcloud já "
        "autenticada. Conexões remotas exigem aprovação e depois usam terminal_read/send/stop."
    ),
    "seguranca": (
        "PERMISSÕES — /ferramentas lista capacidades, não concede acesso. Chamadas MCP, "
        "ações de navegador/desktop e comandos não rotineiros mantêm as confirmações existentes. "
        "Essas ações solicitam confirmação quando necessário. O Cérebro não pede senhas nem "
        "armazena segredos no histórico de ferramentas."
    ),
    "memoria": (
        "MEMÓRIA E BUSCA — peça para consultar ou guardar um fato na memória local. "
        "Use /aprendizado guardar <assunto> | <orientação> para registrar orientação "
        "de longo prazo, /aprendizado para revisar notas e /aprendizado apagar para removê-las. "
        "O fine-tuning com notas é explícito via `python -m assistente.training train "
        "--include-learnings`; ele cria um adaptador, não altera o modelo-base. Use /rag "
        "para consultar o índice de fontes oficiais."
    ),
}


def terminal_help(topic: str = "") -> str:
    normalized = topic.strip().casefold()
    if normalized:
        return _HELP_TOPICS.get(
            normalized,
            "Tema não reconhecido. Temas disponíveis: "
            + ", ".join(_HELP_TOPICS)
            + ". Use /ajuda para o guia rápido.",
        )
    return (
        "GUIA RÁPIDO — CÉREBRO CENTRAL\n"
        "Digite / para descobrir comandos; use /ajuda <tema> para exemplos guiados.\n"
        "1. Projeto: /projeto; depois peça para localizar, inspecionar, corrigir e testar.\n"
        "2. Navegador: descreva a página ou tarefa; confirme ações quando solicitado.\n"
        "3. Integrações: /plugins e /ferramentas mcp mostram o que está conectado.\n"
        "4. Processos: peça para executar um comando ou iniciar/acompanhar um processo.\n"
        "5. Referência: /ferramentas [categoria] lista ferramentas disponíveis.\n"
        "6. Melhorias: /autoaprimorar [escopo] pesquisa novidades e recomendações; "
        "use --aplicar <caminho> para permitir patches limitados a esse caminho.\n"
        "Categorias: projeto, terminal, navegador, desktop, mcp, local.\n"
        "A listagem é informativa: ações continuam sujeitas às aprovações existentes.\n"
        "Temas: " + ", ".join(_HELP_TOPICS) + "."
    )


def format_tool_inventory(tools: list[dict], category: str = "") -> str:
    groups = (
        ("projeto", "Projeto", ("project_",)),
        ("terminal", "Terminal", ("terminal_", "remote_terminal_")),
        ("navegador", "Navegador", ("browser_",)),
        ("desktop", "Desktop/Android", ("desktop_",)),
        ("mcp", "MCP", ("mcp_",)),
        ("local", "Outras ferramentas locais", ()),
    )
    normalized_category = category.strip().casefold()
    valid_categories = [key for key, _, _ in groups]
    if normalized_category and normalized_category not in valid_categories:
        return (
            f"Categoria '{category}' não reconhecida. Categorias: "
            + ", ".join(valid_categories)
            + "."
        )

    grouped: dict[str, list[tuple[str, str]]] = {key: [] for key, _, _ in groups}
    for tool in tools:
        function = tool.get("function", {})
        name = function.get("name", "")
        description = function.get("description", "")
        if not isinstance(name, str):
            continue
        category_key = next(
            (
                key
                for key, _, prefixes in groups[:-1]
                if any(name.startswith(prefix) for prefix in prefixes)
                or (key == "terminal" and name == "notebooklm_login")
            ),
            "local",
        )
        short_description = (
            description.strip().splitlines()[0] if isinstance(description, str) else ""
        )
        if len(short_description) > 110:
            short_description = short_description[:107].rstrip() + "..."
        grouped[category_key].append((name, short_description))

    selected_groups = [
        (key, title, grouped[key])
        for key, title, _ in groups
        if (not normalized_category or key == normalized_category) and grouped[key]
    ]
    if normalized_category and not selected_groups:
        return f"Nenhuma ferramenta disponível na categoria '{normalized_category}'."
    lines = [f"FERRAMENTAS DISPONÍVEIS — {sum(len(items) for _, _, items in selected_groups)}"]
    for _, title, items in selected_groups:
        lines.append(f"\n{title} ({len(items)})")
        lines.extend(f"- {name}: {description}" for name, description in items)
    lines.append("\nListagem informativa; chamadas continuam sujeitas às aprovações existentes.")
    return "\n".join(lines)


def parse_terminal_help_command(prompt: str) -> tuple[str, str] | None:
    parts = prompt.strip().split(maxsplit=1)
    if not parts:
        return None
    command = parts[0].casefold()
    if command not in {"/ajuda", "/ferramentas"}:
        return None
    argument = parts[1].strip() if len(parts) == 2 else ""
    return command, argument


def mcp_plugin_catalog() -> str:
    lines = ["PLUGINS E FERRAMENTAS DISPONÍVEIS — digite @<nome> ou /plugins adicionar @<nome>"]
    lines.extend(f"@{name} — {description}" for name, description in _MCP_PLUGIN_OPTIONS)
    lines.extend(
        [
            "",
            "A seleção mostra instruções e status; não instala nem autentica contas.",
            "Para um servidor MCP externo, use /plugins adicionar <nome> <comando> [argumentos].",
        ]
    )
    return "\n".join(lines)


def parse_plugin_mentions(prompt: str) -> list[str] | None:
    parts = prompt.strip().split()
    if parts == ["@"]:
        return []
    if not parts or not all(part.startswith("@") for part in parts):
        return None
    names = [part[1:].casefold() for part in parts]
    if any(not name or not all(char.isalnum() or char in "-_." for char in name) for name in names):
        return None
    return list(dict.fromkeys(names))


def format_native_tool_status(plugin_name: str, tools: list[dict]) -> str:
    prefixes = {
        "computer-use": ("desktop_", "desktop_android_"),
        "navegador-use": ("browser_",),
    }
    prefix_set = prefixes.get(plugin_name)
    if prefix_set is None:
        return f"@{plugin_name}: ferramenta nativa desconhecida."
    tool_names: set[str] = set()
    for tool in tools:
        function = tool.get("function")
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        if isinstance(name, str) and name.startswith(prefix_set):
            tool_names.add(name)
    names = sorted(tool_names)
    if not names:
        return f"@{plugin_name}: indisponível nesta sessão; nenhuma ferramenta correspondente foi carregada."
    return (
        f"@{plugin_name}: ativo nesta sessão; {len(names)} ferramenta(s) disponível(is): "
        + ", ".join(names)
    )


class SlashCommandCompleter(Completer):
    def __init__(self) -> None:
        self.commands = [
            Completion(command, display=command, display_meta=description)
            for command, description in _SLASH_COMMANDS
        ]
        self.commands.extend(
            Completion(
                f"/hub/{route}",
                display=f"/hub/{route}",
                display_meta=f"{name} — {description}",
            )
            for route, name, description, _ in HUB_DIRECTORY
        )
        self.commands.extend(
            (
                Completion(
                    "/hub/github/copilot/agentes",
                    display="/hub/github/copilot/agentes",
                    display_meta="Listar agentes personalizados do Copilot",
                ),
                Completion(
                    "/hub/crewai/amp/agentes",
                    display="/hub/crewai/amp/agentes",
                    display_meta="Listar crews do CrewAI AMP",
                ),
            )
        )

    def get_completions(self, document: Document, complete_event):
        word = document.text_before_cursor
        normalized_word = word.casefold()
        plugin_command = "/plugins adicionar "
        plugin_command_context = normalized_word.startswith(plugin_command)
        current_token = word.rsplit(None, 1)[-1] if word.split() else ""
        standalone_context = current_token.startswith("@") and (
            not word[: -len(current_token)].strip()
            or all(token.startswith("@") for token in word[: -len(current_token)].split())
        )
        if plugin_command_context and current_token.startswith("@") or standalone_context:
            typed_plugin = current_token[1:].casefold()
            for name, description in _MCP_PLUGIN_OPTIONS:
                if name.startswith(typed_plugin):
                    suggestion = f"{word[: -len(current_token)]}@{name}"
                    yield Completion(
                        suggestion,
                        start_position=-len(current_token),
                        display=f"@{name}",
                        display_meta=description,
                    )
            return
        if not word.startswith("/"):
            return
        for command in self.commands:
            if command.text.startswith(word):
                yield Completion(
                    command.text,
                    start_position=-len(word),
                    display=command.display,
                    display_meta=command.display_meta,
                )


def read_prompt(message: str) -> str:
    session: PromptSession[str] = PromptSession(
        completer=SlashCommandCompleter(),
        complete_while_typing=True,
    )
    with patch_stdout():
        if "Você>" not in message:
            return session.prompt(message)

        user_label = f"{getpass.getuser()}@{socket.gethostname().split('.')[0]}"
        before, after = message.split("Você>", 1)
        prompt = FormattedText(
            [
                ("", before),
                ("class:user", f"{user_label}>"),
                ("", after),
            ]
        )
        style = None
        if "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb":
            style = Style.from_dict(
                {
                    "": "ansiblue",
                    "user": "ansiblue bold",
                }
            )
        return session.prompt(prompt, style=style)


def _paint(value: str, color: str, enabled: bool) -> str:
    return f"{color}{value}{_RESET}" if enabled else value


def print_status(message: str, stream: TextIO | None = None) -> None:
    output = stream or sys.stdout
    color = output.isatty() and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb"
    print(_paint(message, _YELLOW, color), file=output)


def escape_terminal_text(value: str, limit: int = MAX_TERMINAL_MESSAGE_CHARS) -> str:
    escaped_parts = []
    for char in value[:limit]:
        category = unicodedata.category(char)
        if category.startswith("C") or category in {"Zl", "Zp"}:
            codepoint = ord(char)
            if codepoint <= 0xFF:
                escaped_parts.append(f"\\x{codepoint:02x}")
            elif codepoint <= 0xFFFF:
                escaped_parts.append(f"\\u{codepoint:04x}")
            else:
                escaped_parts.append(f"\\U{codepoint:08x}")
        else:
            escaped_parts.append(char)
    escaped = "".join(escaped_parts)
    if len(value) > limit:
        escaped += f" [texto truncado; {len(value)} caracteres no total]"
    return escaped


def _welcome_frame(width: int, height: int, progress: float) -> list[str]:
    canvas_width = max(1, width - 1)
    title = "CÉREBRO CENTRAL"[:canvas_width]
    lines = [[" "] * canvas_width for _ in range(max(1, height))]
    ring_height = max(0, height - 3)
    if ring_height:
        radius_y = min(
            (ring_height - 1) / 2,
            max(0, (canvas_width - 2) / 4.4),
        )
        radius_y *= max(0, min(1, progress))
        radius_x = radius_y * 2.2
        center_x = (canvas_width - 1) / 2
        center_y = (ring_height - 1) / 2
        thickness = max(1, int(radius_x * 0.06))

        if radius_y > 0:
            for y in range(ring_height):
                dy = (y - center_y) / radius_y
                if abs(dy) > 1:
                    continue
                half_width = radius_x * max(0, 1 - dy * dy) ** 0.5
                left = max(0, int(center_x - half_width))
                right = min(canvas_width - 1, int(center_x + half_width))
                for x in range(left, min(canvas_width, left + thickness)):
                    lines[y][x] = "█"
                for x in range(max(0, right - thickness + 1), right + 1):
                    lines[y][x] = "█"

    title_row = min(height - 1, ring_height + 1)
    title_left = max(0, (canvas_width - len(title)) // 2)
    for offset, char in enumerate(title):
        lines[title_row][title_left + offset] = char
    return ["".join(line).rstrip() for line in lines]


def show_welcome(stream: TextIO | None = None) -> None:
    output = stream or sys.stdout
    color = output.isatty() and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb"
    size = shutil.get_terminal_size((80, 24))
    width, height = max(1, size.columns), max(1, size.lines)
    lines = _welcome_frame(width, height, 1)
    lines = [_paint(line, _CYAN, color) for line in lines]
    title_row = min(height - 1, max(0, height - 3) + 1)
    lines[title_row] = _paint(lines[title_row], _TITLE_CYAN, color)
    output.write("\n".join(lines).rstrip("\n") + "\n")
    output.flush()


def print_response(speaker: str, message: str, stream: TextIO | None = None) -> None:
    output = stream or sys.stdout
    color = output.isatty() and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb"
    width = max(1, shutil.get_terminal_size((80, 24)).columns)
    box_width = min(72, max(1, width - 2))
    if box_width < len(speaker) + 2:
        speaker = "Cerebro"
    title = f" {speaker} "[: max(0, box_width - 2)]
    interior_width = max(1, box_width - 4)
    top = "┌" + title + "─" * max(0, box_width - len(title) - 2) + "┐"
    bottom = "└" + "─" * (box_width - 2) + "┘"
    lines = []
    for paragraph in message.splitlines() or [""]:
        lines.extend(textwrap.wrap(paragraph, width=interior_width) or [""])

    print(file=output)
    print(_paint(top, _CYAN, color), file=output)
    for line in lines:
        content = f"│ {line:<{interior_width}} │"
        print(_paint(content, _CYAN_TEXT, color), file=output)
    print(_paint(bottom, _CYAN, color), file=output)
