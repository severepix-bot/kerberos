import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from assistente import hubs


class HubConfigTests(unittest.TestCase):
    def test_hub_directory_lists_platform_routes(self):
        result = hubs.hub_directory()
        self.assertIn("/hub/apple/intelligence", result)
        self.assertIn("/hub/github/copilot", result)
        self.assertIn("/hub/google/gemini", result)
        self.assertIn("/hub/google/antigravity", result)
        self.assertIn("/hub/openai", result)
        self.assertIn("/hub/anthropic/claude", result)
        for route in (
            "agno",
            "langgraph",
            "azure",
            "aws/bedrock",
            "dify",
            "n8n",
            "mastra",
            "microsoft/agent-framework",
            "metagpt",
        ):
            self.assertIn(f"/hub/{route}", result)

    def test_hub_detail_shows_disabled_config_without_exposing_secrets(self):
        config = {
            "hubs": [
                {
                    "name": "openai-primary",
                    "type": "openai_compatible",
                    "enabled": False,
                    "base_url": "https://example.test/v1",
                    "model": "model-a",
                    "api_key_env": "TEST_OPENAI_KEY",
                    "headers": {"X-Custom-Secret": "secret-value"},
                }
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hubs.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            with patch.object(hubs, "HUBS_CONFIG", path):
                result = hubs.hub_directory("openai")
        self.assertIn("desabilitado", result)
        self.assertIn("model-a", result)
        self.assertNotIn("secret-value", result)
        self.assertNotIn("TEST_OPENAI_KEY", result)

    def test_copilot_hub_lists_agent_names_only(self):
        config = {
            "hubs": [
                {
                    "name": "github-copilot",
                    "type": "copilot_cli",
                    "enabled": False,
                }
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agents = root / ".github" / "agents"
            agents.mkdir(parents=True)
            (agents / "reviewer.agent.md").write_text("private agent instructions")
            config_path = root / "hubs.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            with (
                patch.object(hubs, "HUBS_CONFIG", config_path),
                patch.object(hubs.Path, "cwd", return_value=root),
            ):
                result = hubs.hub_directory("github/copilot/agentes")
        self.assertIn("reviewer", result)
        self.assertNotIn("private agent instructions", result)
        self.assertIn("desabilitado", result)

    def test_claude_hub_reports_no_direct_adapter(self):
        result = hubs.hub_directory("anthropic/claude")
        self.assertIn("integração direta ainda não está disponível", result)

    def test_apple_intelligence_directory_describes_local_model(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(hubs, "HUBS_CONFIG", Path(directory) / "hubs.json"):
                result = hubs.hub_directory("apple/intelligence")
        self.assertIn("Apple Intelligence", result)
        self.assertIn("executado localmente", result)
        self.assertIn("ainda não configurado", result)

    def test_crewia_alias_opens_crewai_amp_platform(self):
        result = hubs.hub_directory("crewia")
        self.assertIn("CrewAI AMP", result)
        self.assertEqual(hubs.hub_browser_url("crewia"), "https://app.crewai.com/")
        self.assertEqual(hubs.hub_browser_url("crewai/amp"), "https://app.crewai.com/")

    def test_antigravity_alias_opens_official_platform(self):
        result = hubs.hub_directory("antigravity")
        self.assertIn("Google Antigravity", result)
        self.assertIn("Integração direta com o Cerebro não está disponível", result)
        self.assertEqual(
            hubs.hub_browser_url("antigravity"),
            "https://antigravity.google/",
        )

    def test_open_hub_platform_reports_browser_result(self):
        with patch.object(hubs.webbrowser, "open", return_value=True) as open_browser:
            result = hubs.open_hub_platform("crewai/amp")
        open_browser.assert_called_once_with("https://app.crewai.com/")
        self.assertIn("Navegador aberto", result)

    def test_antigravity_route_is_opened_in_browser(self):
        with patch.object(hubs.webbrowser, "open", return_value=True) as open_browser:
            result = hubs.open_hub_platform("google/antigravity")
        open_browser.assert_called_once_with("https://antigravity.google/")
        self.assertIn("Navegador aberto", result)

    def test_new_agent_platforms_open_their_portals_or_documentation(self):
        expected_urls = {
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
        for route, url in expected_urls.items():
            with self.subTest(route=route):
                self.assertEqual(hubs.hub_browser_url(route), url)
                self.assertIn(
                    "integração direta",
                    hubs.hub_directory(route).lower(),
                )

    def test_lists_supported_agent_platforms(self):
        names = [name for name, _ in hubs.agent_platforms()]
        self.assertEqual(
            names,
            [
                "GitHub Copilot CLI",
                "Apple Intelligence",
                "Google Antigravity",
                "OpenHands",
                "OpenRouter",
                "CrewAI AMP",
                "AutoGen Studio",
                "Agno",
                "LangGraph",
                "Microsoft Azure AI Foundry",
                "Amazon Bedrock",
                "Dify",
                "n8n",
                "Mastra",
                "Microsoft Agent Framework",
                "MetaGPT",
            ],
        )

    def test_loads_only_enabled_hubs(self):
        config = {
            "hubs": [
                {"name": "active", "type": "openai_compatible"},
                {"name": "inactive", "type": "copilot_cli", "enabled": False},
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hubs.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            with patch.object(hubs, "HUBS_CONFIG", path):
                self.assertEqual([hub["name"] for hub in hubs.load_hubs()], ["active"])

    def test_rejects_duplicate_names(self):
        config = {
            "hubs": [
                {"name": "same", "type": "copilot_cli"},
                {"name": "same", "type": "copilot_cli"},
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hubs.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            with patch.object(hubs, "HUBS_CONFIG", path):
                with self.assertRaisesRegex(ValueError, "duplicado"):
                    hubs.load_hubs()

    def test_rejects_duplicate_account_names(self):
        config = {
            "hubs": [
                {
                    "name": "openrouter",
                    "type": "openai_compatible",
                    "accounts": [
                        {"name": "same", "api_key_env": "FIRST_KEY"},
                        {"name": "same", "api_key_env": "SECOND_KEY"},
                    ],
                }
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hubs.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            with patch.object(hubs, "HUBS_CONFIG", path):
                with self.assertRaisesRegex(ValueError, "duplicado"):
                    hubs.load_hubs()

    def test_account_rotation_is_rejected_for_native_copilot_cli(self):
        config = {
            "hubs": [
                {
                    "name": "copilot",
                    "type": "copilot_cli",
                    "accounts": [{"name": "account", "api_key_env": "COPILOT_TOKEN"}],
                }
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hubs.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            with patch.object(hubs, "HUBS_CONFIG", path):
                with self.assertRaisesRegex(ValueError, "accounts não é suportado"):
                    hubs.load_hubs()

    def test_hub_status_counts_accounts_independently_without_exposing_tokens(self):
        config = {
            "hubs": [
                {
                    "name": "provider-a",
                    "type": "openai_compatible",
                    "base_url": "https://a.example/v1",
                    "model": "model-a",
                    "accounts": [
                        {"name": "one", "api_key_env": "PROVIDER_A_ONE"},
                        {"name": "two", "api_key_env": "PROVIDER_A_TWO"},
                    ],
                },
                {
                    "name": "provider-b",
                    "type": "crewai_amp",
                    "base_url": "https://b.example",
                    "accounts": [
                        {"name": "one", "api_key_env": "PROVIDER_B_ONE"},
                    ],
                },
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hubs.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            with (
                patch.object(hubs, "HUBS_CONFIG", path),
                patch.dict(
                    os.environ,
                    {
                        "PROVIDER_A_ONE": "private-a-token",
                        "PROVIDER_B_ONE": "private-b-token",
                    },
                ),
            ):
                statuses = hubs.hub_statuses()
        self.assertIn(("provider-a", "1/2 credenciais configuradas"), statuses)
        self.assertIn(
            ("provider-b", "configurado; autenticação será verificada ao chamar"), statuses
        )
        self.assertNotIn("private-a-token", str(statuses))
        self.assertNotIn("private-b-token", str(statuses))


class HubInvocationTests(unittest.TestCase):
    def test_remote_http_hubs_are_rejected_but_loopback_http_is_allowed(self):
        self.assertEqual(
            hubs._validate_endpoint_url("http://127.0.0.1:8000/v1", "local"),
            "http://127.0.0.1:8000/v1",
        )
        self.assertEqual(
            hubs._validate_endpoint_url("http://[::1]:8000/v1", "local"),
            "http://[::1]:8000/v1",
        )
        for url in (
            "http://example.test/v1",
            "ftp://example.test/v1",
            "https://user:password@example.test/v1",
        ):
            with self.subTest(url=url), self.assertRaisesRegex(ValueError, "HTTPS"):
                hubs._validate_endpoint_url(url, "remote")

    def test_authenticated_http_requests_do_not_follow_redirects(self):
        handler = hubs.NoRedirectHandler()
        request = hubs.urllib.request.Request("https://example.test")
        result = handler.redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "https://other.example/collect",
        )
        self.assertIsNone(result)

    def test_copilot_login_uses_native_browser_oauth_flow(self):
        with (
            patch.object(hubs.shutil, "which", return_value="/usr/bin/copilot"),
            patch.object(hubs.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run,
        ):
            result = hubs.login_copilot_cli()
        self.assertEqual(result, "Login do GitHub Copilot concluído.")
        run.assert_called_once_with(["/usr/bin/copilot", "login", "--web-flow"], check=False)

    def test_copilot_login_reports_missing_cli(self):
        with patch.object(hubs.shutil, "which", return_value=None):
            result = hubs.login_copilot_cli()
        self.assertIn("não encontrado", result)

    def test_copilot_usage_opens_native_cli_with_quota_guidance(self):
        with (
            patch.object(hubs.shutil, "which", return_value="/usr/bin/copilot"),
            patch.object(hubs.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run,
        ):
            with patch("builtins.print") as output:
                result = hubs.show_copilot_usage()
        self.assertEqual(result, "Consulta do uso encerrada.")
        run.assert_called_once_with(["/usr/bin/copilot"], check=False)
        self.assertIn("/statusline quota", output.call_args.args[0])
        self.assertIn("/usage", output.call_args.args[0])

    def test_openai_compatible_request_uses_configured_key_and_model(self):
        response = json.dumps(
            {
                "choices": [{"message": {"content": "  resposta do hub  "}}],
            }
        ).encode()
        hub = {
            "name": "test",
            "type": "openai_compatible",
            "base_url": "https://example.test/v1",
            "model": "example-model",
            "api_key_env": "TEST_HUB_API_KEY",
        }
        with (
            patch.dict(os.environ, {"TEST_HUB_API_KEY": "test-secret"}),
            patch.object(hubs.urllib.request, "build_opener") as build_opener,
        ):
            build_opener.return_value.open.return_value.__enter__.return_value.read.return_value = (
                response
            )
            self.assertEqual(hubs.invoke_hub(hub, "pergunta")["content"], "resposta do hub")
            request = build_opener.return_value.open.call_args.args[0]
            self.assertIsInstance(
                build_opener.call_args.args[0],
                hubs.NoRedirectHandler,
            )
            self.assertEqual(request.full_url, "https://example.test/v1/chat/completions")
            self.assertEqual(request.get_header("Authorization"), "Bearer test-secret")
            self.assertEqual(
                json.loads(request.data),
                {
                    "model": "example-model",
                    "messages": [{"role": "user", "content": "pergunta"}],
                    "stream": False,
                },
            )

    def test_missing_credentials_are_reported(self):
        hub = {
            "name": "test",
            "type": "openai_compatible",
            "base_url": "https://example.test/v1",
            "model": "example-model",
            "api_key_env": "MISSING_TEST_HUB_API_KEY",
        }
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MISSING_TEST_HUB_API_KEY", None)
            result = hubs.invoke_hub(hub, "pergunta")
        self.assertEqual(result["status"], "error")
        self.assertIn("MISSING_TEST_HUB_API_KEY", result["content"])

    def test_apple_foundation_models_receives_prompt_over_stdin(self):
        hub = {
            "name": "apple-intelligence",
            "type": "apple_foundation_models",
        }
        completed = SimpleNamespace(returncode=0, stdout=" resposta local ", stderr="")
        with (
            patch.object(hubs.sys, "platform", "darwin"),
            patch.object(hubs.shutil, "which", return_value="/usr/bin/swift"),
            patch.object(hubs.subprocess, "run", return_value=completed) as run,
        ):
            result = hubs.invoke_hub(hub, "pergunta local")

        self.assertEqual(result, {"name": "apple-intelligence", "status": "ok", "content": "resposta local"})
        self.assertEqual(run.call_args.args[0][0], "/usr/bin/swift")
        self.assertTrue(run.call_args.args[0][1].endswith("apple_foundation_models.swift"))
        self.assertEqual(run.call_args.kwargs["input"], "pergunta local")
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_apple_foundation_models_is_rejected_outside_macos(self):
        with patch.object(hubs.sys, "platform", "linux"):
            result = hubs.invoke_hub(
                {"name": "apple-intelligence", "type": "apple_foundation_models"},
                "pergunta",
            )

        self.assertEqual(result["status"], "error")
        self.assertIn("macOS", result["content"])

    def test_apple_hub_status_explains_platform_requirement(self):
        config = {
            "hubs": [
                {
                    "name": "apple-intelligence",
                    "enabled": True,
                    "type": "apple_foundation_models",
                }
            ]
        }
        with (
            tempfile.TemporaryDirectory() as directory,
        ):
            config_path = Path(directory) / "hubs.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            with (
                patch.object(hubs, "HUBS_CONFIG", config_path),
                patch.object(hubs.sys, "platform", "linux"),
            ):
                self.assertEqual(
                    hubs.hub_statuses(),
                    [("apple-intelligence", "disponível somente quando o Cérebro roda no macOS")],
                )

    def test_openai_fails_over_on_unauthorized_account(self):
        hub = {
            "name": "test",
            "type": "openai_compatible",
            "base_url": "https://example.test/v1",
            "model": "example-model",
            "accounts": [
                {"name": "primary", "api_key_env": "PRIMARY_TEST_KEY"},
                {"name": "backup", "api_key_env": "BACKUP_TEST_KEY"},
            ],
        }
        success = {"choices": [{"message": {"content": "response"}}]}
        with (
            patch.dict(
                os.environ,
                {
                    "PRIMARY_TEST_KEY": "primary-token",
                    "BACKUP_TEST_KEY": "backup-token",
                },
            ),
            patch.object(
                hubs,
                "_read_json_response",
                side_effect=[hubs.HubHTTPError(401), success],
            ) as request,
        ):
            result = hubs.invoke_hub(hub, "question")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(request.call_count, 2)

    def test_openai_does_not_fail_over_on_quota_or_forbidden(self):
        hub = {
            "name": "test",
            "type": "openai_compatible",
            "base_url": "https://example.test/v1",
            "model": "example-model",
            "accounts": [
                {"name": "primary", "api_key_env": "PRIMARY_TEST_KEY"},
                {"name": "backup", "api_key_env": "BACKUP_TEST_KEY"},
            ],
        }
        for status_code in (403, 429):
            with (
                self.subTest(status_code=status_code),
                patch.dict(
                    os.environ,
                    {
                        "PRIMARY_TEST_KEY": "primary-token",
                        "BACKUP_TEST_KEY": "backup-token",
                    },
                ),
                patch.object(
                    hubs,
                    "_read_json_response",
                    side_effect=hubs.HubHTTPError(status_code),
                ) as request,
            ):
                result = hubs.invoke_hub(hub, "question")
            self.assertEqual(result["status"], "error")
            self.assertIn(f"HTTP {status_code}", result["content"])
            request.assert_called_once()

    def test_openai_rejects_authorization_header_override(self):
        hub = {
            "name": "test",
            "type": "openai_compatible",
            "base_url": "https://example.test/v1",
            "model": "example-model",
            "api_key_env": "TEST_HUB_API_KEY",
            "headers": {"authorization": "override"},
        }
        with patch.dict(os.environ, {"TEST_HUB_API_KEY": "test-secret"}):
            result = hubs.invoke_hub(hub, "question")
        self.assertEqual(result["status"], "error")
        self.assertIn("Authorization não pode ser substituído", result["content"])

    def test_redacts_common_secrets_before_sending(self):
        hub = {
            "name": "test",
            "type": "openai_compatible",
            "base_url": "https://example.test/v1",
            "model": "example-model",
            "api_key_env": "TEST_HUB_API_KEY",
        }
        with (
            patch.dict(os.environ, {"TEST_HUB_API_KEY": "test-secret"}),
            patch.object(hubs, "_openai_compatible", return_value="ok") as invoke,
        ):
            hubs.invoke_hub(hub, "api_key=sk-abcdefghijklmnopqrstuvwxyz012345")
        sent_prompt = invoke.call_args.args[1]
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz012345", sent_prompt)
        self.assertIn("[REDACTED]", sent_prompt)

    def test_copilot_cli_runs_without_tool_permissions(self):
        hub = {
            "name": "copilot",
            "type": "copilot_cli",
            "command": "copilot",
            "model": "auto",
        }
        with (
            patch.object(hubs.shutil, "which", return_value="/usr/bin/copilot"),
            patch.object(
                hubs.subprocess,
                "run",
                return_value=SimpleNamespace(returncode=0, stdout="Copilot result", stderr=""),
            ) as run,
        ):
            result = hubs.invoke_hub(hub, "pergunta")
        self.assertEqual(result["content"], "Copilot result")
        args = run.call_args.args[0]
        self.assertIn("--deny-tool", args)
        self.assertIn("*", args)
        self.assertNotIn("shell=True", run.call_args.kwargs)

    def test_copilot_cli_uses_configured_agent_and_agent_directory(self):
        hub = {
            "name": "copilot-agent",
            "type": "copilot_cli",
            "command": "copilot",
            "agent": "reviewer",
        }
        with tempfile.TemporaryDirectory() as directory:
            hub["agent_dir"] = directory
            with (
                patch.object(hubs.shutil, "which", return_value="/usr/bin/copilot"),
                patch.object(
                    hubs.subprocess,
                    "run",
                    return_value=SimpleNamespace(returncode=0, stdout="Agent result", stderr=""),
                ) as run,
            ):
                result = hubs.invoke_hub(hub, "pergunta")
        self.assertEqual(result["status"], "ok")
        args = run.call_args.args[0]
        self.assertEqual(args[args.index("--agent") + 1], "reviewer")
        self.assertEqual(args[args.index("--add-dir") + 1], directory)
        self.assertIn("--deny-tool", args)
        self.assertIn("*", args)

    def test_copilot_cli_rejects_missing_agent_directory(self):
        hub = {
            "name": "copilot-agent",
            "type": "copilot_cli",
            "command": "copilot",
            "agent_dir": "/not/a/real/agent/directory",
        }
        with patch.object(hubs.shutil, "which", return_value="/usr/bin/copilot"):
            result = hubs.invoke_hub(hub, "pergunta")
        self.assertEqual(result["status"], "error")
        self.assertIn("diretório de agentes existente", result["content"])

    def test_crewai_amp_kickoff_polls_for_final_result(self):
        hub = {
            "name": "crew",
            "type": "crewai_amp",
            "base_url": "https://crew.example",
            "api_key_env": "CREW_TOKEN",
            "inputs": {"topic": "{prompt}"},
        }
        responses = iter(
            (
                {"kickoff_id": "run-1"},
                {"state": "SUCCESS", "result": "Crew result"},
            )
        )
        with (
            patch.dict(os.environ, {"CREW_TOKEN": "test-token"}),
            patch.object(
                hubs,
                "_available_account_keys",
                return_value=[("crew", "CREW_TOKEN", "test-token")],
            ),
            patch.object(hubs, "_read_json_response", side_effect=lambda *_: next(responses)),
        ):
            result = hubs.invoke_hub(hub, "research")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["content"], "Crew result")

    def test_crewai_only_fails_over_before_kickoff_and_keeps_selected_account(self):
        hub = {
            "name": "crew",
            "type": "crewai_amp",
            "base_url": "https://crew.example",
            "accounts": [
                {"name": "primary", "api_key_env": "CREW_PRIMARY"},
                {"name": "backup", "api_key_env": "CREW_BACKUP"},
            ],
            "inputs": {"task": "{prompt}"},
        }
        with (
            patch.dict(
                os.environ,
                {
                    "CREW_PRIMARY": "primary-token",
                    "CREW_BACKUP": "backup-token",
                },
            ),
            patch.object(
                hubs,
                "_read_json_response",
                side_effect=[
                    hubs.HubHTTPError(401),
                    {"kickoff_id": "run-2"},
                    {"state": "SUCCESS", "result": "Crew result"},
                ],
            ) as read,
        ):
            result = hubs.invoke_hub(hub, "research")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(read.call_count, 3)
        self.assertEqual(
            read.call_args_list[0].args[0].get_header("Authorization"),
            "Bearer primary-token",
        )
        self.assertEqual(
            read.call_args_list[1].args[0].get_header("Authorization"),
            "Bearer backup-token",
        )
        self.assertEqual(
            read.call_args_list[2].args[0].get_header("Authorization"),
            "Bearer backup-token",
        )

    def test_crewai_does_not_retry_quota_response(self):
        hub = {
            "name": "crew",
            "type": "crewai_amp",
            "base_url": "https://crew.example",
            "accounts": [
                {"name": "primary", "api_key_env": "CREW_PRIMARY"},
                {"name": "backup", "api_key_env": "CREW_BACKUP"},
            ],
        }
        with (
            patch.dict(
                os.environ,
                {
                    "CREW_PRIMARY": "primary-token",
                    "CREW_BACKUP": "backup-token",
                },
            ),
            patch.object(
                hubs,
                "_read_json_response",
                side_effect=hubs.HubHTTPError(429),
            ) as read,
        ):
            result = hubs.invoke_hub(hub, "research")
        self.assertEqual(result["status"], "error")
        self.assertIn("HTTP 429", result["content"])
        read.assert_called_once()


if __name__ == "__main__":
    unittest.main()
