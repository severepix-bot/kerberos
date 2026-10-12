import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from assistente.__main__ import PROJECT_ROOT

mcp = FastMCP("cerebro-google-workspace")
MAX_OUTPUT_BYTES = 1_000_000


def gws_command() -> list[str]:
    bundled_cli = PROJECT_ROOT / ".plugins" / "node_modules" / "@googleworkspace" / "cli" / "run.js"
    if bundled_cli.is_file() and shutil.which("node"):
        return [shutil.which("node") or "node", str(bundled_cli)]
    installed_cli = shutil.which("gws")
    if installed_cli:
        return [installed_cli]
    raise FileNotFoundError(
        "Google Workspace CLI gws não encontrado. Instale-o pelo iniciar.sh/iniciar.bat "
        "ou com `npm install -g @googleworkspace/cli`."
    )


def gws_auth_configured() -> bool:
    config_dir = Path(
        os.environ.get("GOOGLE_WORKSPACE_CLI_CONFIG_DIR", Path.home() / ".config" / "gws")
    )
    return (config_dir / "client_secret.json").is_file()


def _run_gws_read(arguments: list[str], params: dict[str, Any]) -> str:
    command = [*gws_command(), *arguments, "--params", json.dumps(params)]
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=45,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "erro sem detalhes"
        raise RuntimeError(f"gws falhou ({result.returncode}): {detail[:2000]}")
    output = result.stdout.strip()
    if len(output.encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise ValueError("Resposta gws excede o limite de 1 MB; reduza o tamanho da consulta.")
    return output


@mcp.tool()
def search_drive_files(query: str, page_size: int = 20) -> str:
    """Pesquisa metadados de arquivos no Google Drive; não altera arquivos."""
    if not query.strip():
        raise ValueError("A pesquisa do Drive não pode estar vazia.")
    if not 1 <= page_size <= 100:
        raise ValueError("page_size deve estar entre 1 e 100.")
    return _run_gws_read(
        ["drive", "files", "list"],
        {
            "q": query,
            "pageSize": page_size,
            "fields": "files(id,name,mimeType,modifiedTime,webViewLink),nextPageToken",
        },
    )


@mcp.tool()
def get_drive_file_metadata(file_id: str) -> str:
    """Obtém metadados de um arquivo do Drive sem baixar nem modificar seu conteúdo."""
    if not file_id.strip():
        raise ValueError("file_id não pode estar vazio.")
    return _run_gws_read(
        ["drive", "files", "get"],
        {"fileId": file_id, "fields": "id,name,mimeType,modifiedTime,webViewLink"},
    )


@mcp.tool()
def search_gmail_messages(query: str, max_results: int = 20) -> str:
    """Pesquisa mensagens do Gmail; retorna identificadores e resumos, sem enviar emails."""
    if not query.strip():
        raise ValueError("A pesquisa do Gmail não pode estar vazia.")
    if not 1 <= max_results <= 100:
        raise ValueError("max_results deve estar entre 1 e 100.")
    return _run_gws_read(
        ["gmail", "users", "messages", "list"],
        {"userId": "me", "q": query, "maxResults": max_results},
    )


@mcp.tool()
def get_gmail_message(message_id: str) -> str:
    """Lê uma mensagem do Gmail pelo identificador; não envia nem altera mensagens."""
    if not message_id.strip():
        raise ValueError("message_id não pode estar vazio.")
    return _run_gws_read(
        ["gmail", "users", "messages", "get"],
        {"userId": "me", "id": message_id, "format": "full"},
    )


@mcp.tool()
def list_calendar_events(time_min: str, time_max: str, max_results: int = 50) -> str:
    """Lista eventos do calendário principal em um intervalo ISO 8601, sem modificá-los."""
    if not time_min.strip() or not time_max.strip():
        raise ValueError("time_min e time_max precisam ser datas ISO 8601.")
    if not 1 <= max_results <= 100:
        raise ValueError("max_results deve estar entre 1 e 100.")
    return _run_gws_read(
        ["calendar", "events", "list"],
        {
            "calendarId": "primary",
            "timeMin": time_min,
            "timeMax": time_max,
            "maxResults": max_results,
            "singleEvents": True,
            "orderBy": "startTime",
        },
    )


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
