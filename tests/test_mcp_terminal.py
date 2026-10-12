import asyncio
import json
import sys
import unittest
from contextlib import AsyncExitStack, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import AsyncMock, patch

from assistente import __main__ as app
from assistente import mcp_terminal


class McpTerminalTests(unittest.TestCase):
    def test_terminal_mcp_is_bundled_and_uses_the_project_python(self):
        config_path = Path(__file__).resolve().parent.parent / "config" / "mcp.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))

        self.assertEqual(
            config["mcpServers"]["terminal"],
            {
                "command": "${PROJECT_PYTHON}",
                "args": ["-m", "assistente.mcp_terminal"],
            },
        )

    def test_terminal_mcp_rejects_unapproved_command_classes(self):
        with patch.object(mcp_terminal, "run_project_command") as run_command:
            with self.assertRaises(PermissionError):
                mcp_terminal.execute_command(["sh", "-c", "echo unsafe"])

        run_command.assert_not_called()

    def test_terminal_mcp_delegates_safe_checks_to_existing_project_runner(self):
        with (
            patch.object(mcp_terminal, "_is_safe_project_check", return_value=True),
            patch.object(
                mcp_terminal, "run_project_command", return_value="Exit code: 0"
            ) as run_command,
        ):
            output = mcp_terminal.execute_command(
                [sys.executable, "-m", "pytest", "-q", "tests"],
                cwd="tests",
                timeout=90,
            )

        self.assertEqual(output, "Exit code: 0")
        run_command.assert_called_once_with(
            command=[sys.executable, "-m", "pytest", "-q", "tests"],
            cwd="tests",
            timeout=90,
        )

    def test_terminal_mcp_cannot_run_checks_outside_project_root(self):
        with self.assertRaises(ValueError):
            mcp_terminal.execute_command(["git", "status"], cwd="..")

    def test_stdio_server_publishes_the_terminal_tool_schema(self):
        async def connect_server() -> dict[str, object]:
            async with AsyncExitStack() as stack:
                tools, _routes = await app.connect_mcp(stack, quiet=True)
                return tools

        output = StringIO()
        with (
            patch.object(
                app,
                "load_mcp_config",
                return_value={
                    "terminal": {
                        "command": sys.executable,
                        "args": ["-m", "assistente.mcp_terminal"],
                    }
                },
            ),
            redirect_stdout(output),
        ):
            tools = asyncio.run(connect_server())

        terminal_tool = tools["mcp_terminal_execute_command"]["function"]
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(terminal_tool["parameters"]["required"], ["command"])
        self.assertEqual(
            set(terminal_tool["parameters"]["properties"]),
            {"command", "cwd", "timeout"},
        )

    def test_cerebro_approval_gate_blocks_terminal_mcp_invocation(self):
        class Session:
            def __init__(self) -> None:
                self.called = False

            async def call_tool(self, _name: str, arguments: dict[str, object]) -> object:
                self.called = True
                return arguments

        async def invoke() -> tuple[str, Session]:
            session = Session()
            output = await app.invoke_tool(
                "mcp_terminal_execute_command",
                {"command": ["pytest", "-q"]},
                {
                    "mcp_terminal_execute_command": (
                        session,
                        "execute_command",
                        "terminal",
                    )
                },
                approve=AsyncMock(return_value=False),
            )
            return output, session

        output, session = asyncio.run(invoke())

        self.assertEqual(output, "Ação cancelada pelo usuário.")
        self.assertFalse(session.called)


if __name__ == "__main__":
    unittest.main()
