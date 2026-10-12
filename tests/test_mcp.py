import asyncio
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import AsyncExitStack, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from assistente import __main__ as app


class McpConfigurationTests(unittest.TestCase):
    def test_bundled_account_servers_use_browser_oauth_and_github_cli(self):
        config_path = Path(app.BUNDLED_MCP_CONFIG)
        servers = json.loads(config_path.read_text(encoding="utf-8"))["mcpServers"]

        self.assertEqual(
            servers["stripe"],
            {"url": "https://mcp.stripe.com", "auth": "oauth"},
        )
        self.assertEqual(
            servers["cloudflare"],
            {"url": "https://mcp.cloudflare.com/mcp", "auth": "oauth"},
        )
        self.assertEqual(
            servers["github"]["envFromCommand"],
            {"GITHUB_PERSONAL_ACCESS_TOKEN": ["gh", "auth", "token"]},
        )
        self.assertNotIn("requiresEnv", servers["github"])
        self.assertNotIn("requiresEnv", servers["stripe"])
        self.assertNotIn("requiresEnv", servers["cloudflare"])
        self.assertEqual(
            servers["google-workspace"]["args"],
            ["-m", "assistente.mcp_google_workspace"],
        )
        self.assertTrue(servers["google-workspace"]["requiresExplicitLogin"])
        self.assertNotIn("requiresEnv", servers["google-workspace"])
        self.assertEqual(
            servers["notebooklm"],
            {
                "command": "npx",
                "args": ["--yes", "@roomi-fields/notebooklm-mcp@3.2.0"],
                "env": {
                    "NOTEBOOKLM_PROFILE": "standard",
                    "NOTEBOOKLM_DISABLED_TOOLS": "add_notebook,update_notebook",
                },
                "requiresCommands": ["npx"],
                "requiresExplicitLogin": True,
                "setupMessage": servers["notebooklm"]["setupMessage"],
            },
        )
        self.assertIn("não oficial", servers["notebooklm"]["setupMessage"])
        self.assertIn("notebooklm-mcp-setup-auth", servers["notebooklm"]["setupMessage"])
        self.assertIn("notebooklm_login", servers["notebooklm"]["setupMessage"])
        self.assertEqual(
            servers["microsoft"]["args"][-1],
            "mcp",
        )
        self.assertTrue(servers["microsoft"]["requiresExplicitLogin"])
        self.assertNotIn("requiresEnv", servers["microsoft"])
        self.assertTrue(servers["apple-photos"]["requiresExplicitLogin"])
        self.assertNotIn("requiresEnvValues", servers["apple-photos"])

    def test_explicit_login_servers_do_not_start_until_requested(self):
        output = StringIO()

        async def connect() -> tuple[dict[str, object], dict[str, object]]:
            async with AsyncExitStack() as stack:
                return await app._connect_mcp_server(
                    stack,
                    "microsoft",
                    {
                        "command": "must-not-run",
                        "requiresExplicitLogin": True,
                        "setupMessage": "Use /mcp login microsoft.",
                    },
                )

        with redirect_stdout(output):
            tools, routes = asyncio.run(connect())

        self.assertEqual(tools, {})
        self.assertEqual(routes, {})
        self.assertIn("/mcp login microsoft", output.getvalue())

    def test_mcp_oauth_servers_wait_for_explicit_browser_login(self):
        output = StringIO()

        async def connect() -> tuple[dict[str, object], dict[str, object]]:
            async with AsyncExitStack() as stack:
                return await app.connect_mcp(stack)

        with (
            patch.object(
                app,
                "load_mcp_config",
                return_value={
                    "stripe": {"url": "https://mcp.stripe.com", "auth": "oauth"},
                    "cloudflare": {
                        "url": "https://mcp.cloudflare.com/mcp",
                        "auth": "oauth",
                    },
                },
            ),
            redirect_stdout(output),
        ):
            tools, routes = asyncio.run(connect())

        self.assertEqual(tools, {})
        self.assertEqual(routes, {})
        self.assertIn("/mcp login stripe", output.getvalue())
        self.assertIn("/mcp login cloudflare", output.getvalue())

    def test_quiet_mcp_startup_hides_routine_login_notices(self):
        output = StringIO()

        async def connect() -> tuple[dict[str, object], dict[str, object]]:
            async with AsyncExitStack() as stack:
                return await app._connect_mcp_server(
                    stack,
                    "stripe",
                    {"url": "https://mcp.stripe.com", "auth": "oauth"},
                    quiet=True,
                )

        with redirect_stdout(output):
            tools, routes = asyncio.run(connect())

        self.assertEqual(tools, {})
        self.assertEqual(routes, {})
        self.assertEqual(output.getvalue(), "")

    def test_mcp_oauth_rejects_non_https_urls(self):
        async def connect() -> None:
            async with AsyncExitStack() as stack:
                await app.connect_remote_mcp(stack, "provider", "http://provider.test/mcp")

        with self.assertRaisesRegex(ValueError, "HTTPS"):
            asyncio.run(connect())

    def test_github_cli_token_is_injected_without_printing_credential(self):
        output = StringIO()
        with (
            patch.object(app.shutil, "which", return_value="/usr/bin/gh"),
            patch.dict(
                os.environ,
                {"GH_TOKEN": "legacy-env-token"},
                clear=False,
            ),
            patch.object(
                app.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(
                    args=["gh", "auth", "token"],
                    returncode=0,
                    stdout="secret-token\n",
                    stderr="",
                ),
            ) as run,
            redirect_stdout(output),
        ):
            environment = app._resolve_mcp_server_env(
                "github",
                {
                    "requiresCommands": ["gh"],
                    "env": {
                        "GITHUB_PERSONAL_ACCESS_TOKEN": "legacy-admin-token",
                        "GITHUB_TOKEN": "legacy-admin-token",
                    },
                    "envFromCommand": {"GITHUB_PERSONAL_ACCESS_TOKEN": ["legacy-token-command"]},
                },
            )

        self.assertEqual(
            environment["GITHUB_PERSONAL_ACCESS_TOKEN"],
            "secret-token",
        )
        self.assertEqual(environment["GITHUB_TOKEN"], "")
        self.assertEqual(environment["GH_TOKEN"], "")
        self.assertNotIn("legacy-admin-token", str(environment))
        self.assertNotIn("secret-token", output.getvalue())
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], ["gh", "auth", "token"])
        self.assertNotIn("GH_TOKEN", run.call_args.kwargs["env"])

    def test_github_rate_limit_quarantines_closes_and_removes_routes(self):
        class Session:
            async def call_tool(self, *_args, **_kwargs):
                return SimpleNamespace(
                    isError=True,
                    content=[SimpleNamespace(text="API rate limit exceeded (403)")],
                )

        async def invoke() -> tuple[
            str,
            AsyncMock,
            dict[str, object],
            dict[str, object],
            list[dict[str, object]],
        ]:
            closed = AsyncExitStack()
            await closed.__aenter__()
            close_callback = AsyncMock()
            closed.push_async_callback(close_callback)
            github_tool = {"function": {"name": "mcp_github_search"}}
            tool_registry = {"mcp_github_search": github_tool}
            tools = [github_tool]
            routes: dict[str, object] = {
                "mcp_github_search": (
                    Session(),
                    "search",
                    "github",
                    closed,
                    tool_registry,
                    tools,
                ),
                "mcp_other_lookup": (Session(), "lookup", "other"),
            }
            output = await app.invoke_tool(
                "mcp_github_search",
                {},
                routes,
                approve=AsyncMock(return_value=True),
            )
            return output, close_callback, routes, tool_registry, tools

        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            quarantine = data_dir / "github-mcp-quarantine.json"
            with (
                patch.object(app, "USER_DATA", data_dir),
                patch.object(app, "GITHUB_MCP_QUARANTINE", quarantine),
                patch.object(app, "_logout_github_cli", return_value=None) as logout,
            ):
                output, close_callback, routes, tool_registry, tools = asyncio.run(invoke())
                quarantine_written = quarantine.exists()

        self.assertIn("Limite de requisições do GitHub atingido", output)
        self.assertTrue(quarantine_written)
        close_callback.assert_awaited_once()
        self.assertEqual(set(routes), {"mcp_other_lookup"})
        self.assertEqual(tool_registry, {})
        self.assertEqual(tools, [])
        logout.assert_called_once_with()

    def test_github_rate_limit_detection_includes_temporary_and_secondary_limits(self):
        self.assertTrue(app._is_github_rate_limit_error("HTTP 429 Too Many Requests"))
        self.assertTrue(
            app._is_github_rate_limit_error("You have exceeded a secondary rate limit.")
        )
        self.assertFalse(app._is_github_rate_limit_error("HTTP 403 Resource forbidden"))

    def test_quarantined_github_is_not_reconnected_at_startup(self):
        async def connect() -> tuple[dict[str, object], dict[str, object]]:
            async with AsyncExitStack() as stack:
                return await app._connect_mcp_server(
                    stack,
                    "github",
                    {"command": "must-not-run"},
                )

        with tempfile.TemporaryDirectory() as directory:
            quarantine = Path(directory) / "github-mcp-quarantine.json"
            quarantine.touch()
            output = StringIO()
            with (
                patch.object(app, "GITHUB_MCP_QUARANTINE", quarantine),
                redirect_stdout(output),
            ):
                tools, routes = asyncio.run(connect())

        self.assertEqual(tools, {})
        self.assertEqual(routes, {})
        self.assertIn("bloqueado após limite", output.getvalue())

    def test_github_cli_login_uses_browser_auth_without_capturing_cli_output(self):
        with (
            patch.object(app.shutil, "which", return_value="/usr/bin/gh"),
            patch.object(
                app.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(
                    args=["gh", "auth", "login"],
                    returncode=0,
                ),
            ) as run,
        ):
            result = app.login_mcp_cli("github")

        self.assertEqual(result, "Login do GitHub concluído no CLI.")
        run.assert_called_once_with(
            ["/usr/bin/gh", "auth", "login", "--web", "--git-protocol", "https"],
            check=False,
            env=app._github_cli_environment(),
        )

    def test_github_cli_login_removes_quarantined_local_auth_before_relogin(self):
        with tempfile.TemporaryDirectory() as directory:
            quarantine = Path(directory) / "github-mcp-quarantine.json"
            quarantine.touch()
            with (
                patch.object(app, "GITHUB_MCP_QUARANTINE", quarantine),
                patch.object(app.shutil, "which", return_value="/usr/bin/gh"),
                patch.object(app, "_logout_github_cli", return_value=None) as logout,
                patch.object(
                    app.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess(
                        args=["gh", "auth", "login"],
                        returncode=0,
                    ),
                ) as run,
            ):
                result = app.login_mcp_cli("github")

        self.assertEqual(result, "Login do GitHub concluído no CLI.")
        logout.assert_called_once_with()
        run.assert_called_once()

    def test_google_workspace_cli_login_uses_setup_only_before_oauth_config(self):
        with (
            patch(
                "assistente.mcp_google_workspace.gws_auth_configured",
                return_value=False,
            ),
            patch(
                "assistente.mcp_google_workspace.gws_command",
                return_value=["/usr/bin/gws"],
            ),
            patch.object(
                app.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(
                    args=["/usr/bin/gws", "auth", "setup"],
                    returncode=0,
                ),
            ) as run,
        ):
            result = app.login_mcp_cli("google-workspace")

        self.assertEqual(result, "Login Google Workspace concluído pelo gws.")
        run.assert_called_once_with(["/usr/bin/gws", "auth", "setup"], check=False)

    def test_google_workspace_cli_login_uses_login_after_initial_setup(self):
        with (
            patch(
                "assistente.mcp_google_workspace.gws_auth_configured",
                return_value=True,
            ),
            patch(
                "assistente.mcp_google_workspace.gws_command",
                return_value=["/usr/bin/gws"],
            ),
            patch.object(
                app.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(
                    args=["/usr/bin/gws", "auth", "login"],
                    returncode=0,
                ),
            ) as run,
        ):
            result = app.login_mcp_cli("google-workspace")

        self.assertEqual(result, "Login Google Workspace concluído pelo gws.")
        run.assert_called_once_with(["/usr/bin/gws", "auth", "login"], check=False)

    def test_oauth_tokens_are_saved_in_system_keyring_only(self):
        class SecureKeyringBackend:
            __module__ = "keyring.backends.macOS"

        stored: dict[tuple[str, str], str] = {}
        keyring = types.ModuleType("keyring")
        keyring.get_keyring = lambda: SecureKeyringBackend()
        keyring.get_password = lambda service, username: stored.get((service, username))
        keyring.set_password = lambda service, username, password: stored.__setitem__(
            (service, username), password
        )
        with patch.dict(sys.modules, {"keyring": keyring}):
            storage = app.McpOAuthTokenStorage("stripe", "https://mcp.stripe.com")

            async def store_and_read_token():
                from mcp.shared.auth import OAuthToken

                await storage.set_tokens(
                    OAuthToken(access_token="private-token", token_type="Bearer")
                )
                return await storage.get_tokens()

            token = asyncio.run(store_and_read_token())

        self.assertEqual(token.access_token, "private-token")
        self.assertTrue(stored)
        self.assertTrue(all(service == "Cerebro MCP OAuth" for service, _ in stored))

    def test_oauth_storage_refuses_insecure_or_missing_keyring_backend(self):
        class InsecureKeyringBackend:
            __module__ = "keyring.backends.fail"

        keyring = types.ModuleType("keyring")
        keyring.get_keyring = lambda: InsecureKeyringBackend()
        with patch.dict(sys.modules, {"keyring": keyring}):
            storage = app.McpOAuthTokenStorage("stripe", "https://mcp.stripe.com")

            async def read_token():
                return await storage.get_tokens()

            with self.assertRaisesRegex(RuntimeError, "cofre seguro"):
                asyncio.run(read_token())

    def test_add_mcp_server_preserves_user_config_and_writes_restricted_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            bundled_config = root / "bundled.json"
            bundled_config.write_text(
                json.dumps({"mcpServers": {"builtin": {"command": "node", "args": []}}}),
                encoding="utf-8",
            )
            user_config = root / "user" / "mcp.json"
            user_config.parent.mkdir()
            user_config.write_text(
                json.dumps(
                    {
                        "mcpServers": {"existing": {"command": "existing", "args": []}},
                        "custom": "preserved",
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch.object(app, "BUNDLED_MCP_CONFIG", bundled_config),
                patch.object(app, "MCP_CONFIG", user_config),
                patch.object(app, "WORKSPACE_PATHS"),
                patch.object(app, "prepare_workspace"),
                patch.object(app, "USER_DATA", root / "data"),
            ):
                result = app.add_mcp_server("react-docs", "npx", ["-y", "react-docs-mcp"])
            saved = json.loads(user_config.read_text(encoding="utf-8"))
            self.assertIn("reinicie", result.casefold())
            self.assertEqual(saved["custom"], "preserved")
            self.assertEqual(
                saved["mcpServers"]["react-docs"],
                {"command": "npx", "args": ["-y", "react-docs-mcp"]},
            )
            if os.name != "nt":
                self.assertEqual(user_config.stat().st_mode & 0o777, 0o600)

    def test_add_mcp_server_rejects_duplicate_names_and_invalid_names(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            bundled_config = root / "bundled.json"
            bundled_config.write_text(
                json.dumps({"mcpServers": {"builtin": {"command": "node", "args": []}}}),
                encoding="utf-8",
            )
            with (
                patch.object(app, "BUNDLED_MCP_CONFIG", bundled_config),
                patch.object(app, "MCP_CONFIG", root / "user.json"),
                patch.object(app, "WORKSPACE_PATHS"),
                patch.object(app, "prepare_workspace"),
                patch.object(app, "USER_DATA", root / "data"),
            ):
                with self.assertRaisesRegex(ValueError, "Já existe"):
                    app.add_mcp_server("builtin", "npx", ["-y", "pkg"])
                with self.assertRaisesRegex(ValueError, "Nome inválido"):
                    app.add_mcp_server("../outside", "npx", ["-y", "pkg"])
            self.assertFalse((root / "user.json").exists())

    def test_load_mcp_config_expands_environment_placeholders(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            bundled_config = Path(temp_dir) / "mcp.json"
            bundled_config.write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "provider": {
                                "command": "node",
                                "env": {"TOKEN": "${MCP_TEST_TOKEN}"},
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch.object(app, "BUNDLED_MCP_CONFIG", bundled_config),
                patch.object(app, "MCP_CONFIG", Path(temp_dir) / "missing.json"),
                patch.object(app, "WORKSPACE_PATHS"),
                patch.object(app, "prepare_workspace"),
                patch.object(app, "USER_DATA") as user_data,
                patch.dict(os.environ, {"MCP_TEST_TOKEN": "test-value"}),
            ):
                servers = app.load_mcp_config()

        self.assertEqual(servers["provider"]["env"]["TOKEN"], "test-value")
        user_data.mkdir.assert_called_once_with(parents=True, exist_ok=True)

    def test_connect_mcp_skips_provider_without_required_environment(self):
        output = StringIO()
        with (
            patch.object(
                app,
                "load_mcp_config",
                return_value={
                    "provider": {
                        "command": "not-launched",
                        "requiresEnv": ["MCP_REQUIRED_TOKEN"],
                    }
                },
            ),
            patch.dict(os.environ, {}, clear=True),
            redirect_stdout(output),
        ):
            tools, routes = app.asyncio.run(app.connect_mcp(AsyncExitStack()))

        self.assertEqual(tools, {})
        self.assertEqual(routes, {})
        self.assertIn("MCP_REQUIRED_TOKEN", output.getvalue())
        self.assertNotIn("not-launched", output.getvalue())

    def test_missing_provider_environment_uses_browser_setup_guidance(self):
        output = StringIO()
        guidance = "Registre um app OAuth para este servidor."
        with (
            patch.object(
                app,
                "load_mcp_config",
                return_value={
                    "google-workspace": {
                        "command": "not-launched",
                        "requiresEnv": ["GOOGLE_CLIENT_ID"],
                        "setupMessage": guidance,
                    }
                },
            ),
            patch.dict(os.environ, {}, clear=True),
            redirect_stdout(output),
        ):
            tools, routes = app.asyncio.run(app.connect_mcp(AsyncExitStack()))

        self.assertEqual(tools, {})
        self.assertEqual(routes, {})
        self.assertIn(guidance, output.getvalue())
        self.assertNotIn("GOOGLE_CLIENT_ID", output.getvalue())

    def test_connect_mcp_skips_provider_unless_environment_value_matches(self):
        output = StringIO()
        with (
            patch.object(
                app,
                "load_mcp_config",
                return_value={
                    "provider": {
                        "command": "not-launched",
                        "requiresEnvValues": {"MCP_OPT_IN": "1"},
                    }
                },
            ),
            patch.dict(os.environ, {"MCP_OPT_IN": "0"}, clear=True),
            redirect_stdout(output),
        ):
            tools, routes = app.asyncio.run(app.connect_mcp(AsyncExitStack()))

        self.assertEqual(tools, {})
        self.assertEqual(routes, {})
        self.assertIn("MCP_OPT_IN", output.getvalue())
        self.assertNotIn("not-launched", output.getvalue())

    def test_connect_mcp_skips_provider_on_unsupported_platform(self):
        output = StringIO()
        with (
            patch.object(
                app,
                "load_mcp_config",
                return_value={
                    "provider": {
                        "command": "not-launched",
                        "requiresPlatform": "not-the-current-platform",
                    }
                },
            ),
            redirect_stdout(output),
        ):
            tools, routes = app.asyncio.run(app.connect_mcp(AsyncExitStack()))

        self.assertEqual(tools, {})
        self.assertEqual(routes, {})
        self.assertIn("not-the-current-platform", output.getvalue())
        self.assertNotIn("not-launched", output.getvalue())


if __name__ == "__main__":
    unittest.main()
