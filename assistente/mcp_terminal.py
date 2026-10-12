from mcp.server.fastmcp import FastMCP

from assistente.__main__ import _is_safe_project_check, run_project_command

mcp = FastMCP("cerebro-terminal")


@mcp.tool()
def execute_command(command: list[str], cwd: str = ".", timeout: int = 180) -> str:
    """Executa verificações seguras do projeto, sem shell, dentro da pasta do projeto."""
    if not _is_safe_project_check(command):
        raise PermissionError(
            "O MCP terminal aceita apenas testes e verificações reconhecidos. "
            "Use terminal_execute no Cérebro para outros comandos; eles exigem aprovação."
        )
    return run_project_command(command=command, cwd=cwd, timeout=timeout)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
