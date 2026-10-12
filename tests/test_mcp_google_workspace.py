import asyncio
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import AsyncExitStack
from pathlib import Path
from unittest.mock import patch

from assistente import __main__ as app
from assistente import mcp_google_workspace as google


class GoogleWorkspaceMcpTests(unittest.TestCase):
    def test_gws_prefers_the_project_bundle_without_shell(self):
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(google, "PROJECT_ROOT", Path(temp_dir)),
            patch.object(google.shutil, "which", return_value="/usr/bin/node"),
        ):
            bundled = (
                Path(temp_dir)
                / ".plugins"
                / "node_modules"
                / "@googleworkspace"
                / "cli"
                / "run.js"
            )
            bundled.parent.mkdir(parents=True)
            bundled.touch()

            self.assertEqual(google.gws_command(), ["/usr/bin/node", str(bundled)])

    def test_stdio_server_publishes_only_read_tools_after_explicit_login(self):
        async def connect() -> dict[str, object]:
            async with AsyncExitStack() as stack:
                tools, _routes = await app.connect_mcp_server(
                    stack,
                    "google-workspace",
                    {
                        "command": sys.executable,
                        "args": ["-m", "assistente.mcp_google_workspace"],
                        "requiresExplicitLogin": True,
                    },
                    authenticate=True,
                )
                return tools

        tools = asyncio.run(connect())

        self.assertEqual(
            set(tools),
            {
                "mcp_google_workspace_search_drive_files",
                "mcp_google_workspace_get_drive_file_metadata",
                "mcp_google_workspace_search_gmail_messages",
                "mcp_google_workspace_get_gmail_message",
                "mcp_google_workspace_list_calendar_events",
            },
        )

    def test_drive_search_uses_allowlisted_gws_args_and_structured_params(self):
        response = '{"files":[{"id":"file-1","name":"report"}]}'
        with (
            patch.object(google, "_run_gws_read", return_value=response) as run,
            patch.object(google, "gws_command", return_value=["gws"]),
        ):
            result = google.search_drive_files("name contains 'report'", page_size=5)

        self.assertEqual(result, response)
        run.assert_called_once_with(
            ["drive", "files", "list"],
            {
                "q": "name contains 'report'",
                "pageSize": 5,
                "fields": "files(id,name,mimeType,modifiedTime,webViewLink),nextPageToken",
            },
        )

    def test_gmail_read_uses_current_user_and_never_accepts_cli_arguments(self):
        with patch.object(google, "_run_gws_read", return_value="{}") as run:
            google.get_gmail_message("message-1")

        run.assert_called_once_with(
            ["gmail", "users", "messages", "get"],
            {"userId": "me", "id": "message-1", "format": "full"},
        )

    def test_search_validation_rejects_empty_and_unbounded_queries(self):
        with self.assertRaises(ValueError):
            google.search_drive_files(" ")
        with self.assertRaises(ValueError):
            google.search_gmail_messages("in:anywhere", max_results=1000)

    def test_gws_invocation_reports_errors_and_bounds_output(self):
        with (
            patch.object(google, "gws_command", return_value=["gws"]),
            patch.object(
                google.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(
                    args=["gws"],
                    returncode=2,
                    stdout="",
                    stderr="authentication failed",
                ),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "authentication failed"):
                google._run_gws_read(["drive", "files", "list"], {"pageSize": 5})

        with (
            patch.object(google, "MAX_OUTPUT_BYTES", 2),
            patch.object(google, "gws_command", return_value=["gws"]),
            patch.object(
                google.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(
                    args=["gws"],
                    returncode=0,
                    stdout=json.dumps({"files": []}),
                    stderr="",
                ),
            ),
        ):
            with self.assertRaisesRegex(ValueError, "1 MB"):
                google._run_gws_read(["drive", "files", "list"], {"pageSize": 5})


if __name__ == "__main__":
    unittest.main()
