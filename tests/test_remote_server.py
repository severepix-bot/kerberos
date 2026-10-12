import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
import uuid
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

from assistente import server
from assistente.__main__ import upload_startup_improvement_report
from assistente.remote import remote_chat


class ServerAuthTests(unittest.TestCase):
    def setUp(self):
        self.data_directory = tempfile.TemporaryDirectory()
        self.data_path = patch.object(server, "USER_DATA", Path(self.data_directory.name))
        self.data_path.start()

    def tearDown(self):
        self.data_path.stop()
        self.data_directory.cleanup()

    def test_remote_client_defaults_to_configured_cerebro_domain(self):
        output = StringIO()
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("assistente.remote.show_welcome"),
            patch("assistente.remote.read_prompt", return_value="/sair"),
            patch(
                "assistente.remote.request_json",
                return_value={"model": "test"},
            ) as request,
            patch.dict(os.environ, {"CEREBRO_API_TOKEN": "test-token"}),
            redirect_stdout(output),
        ):
            remote_chat()

        self.assertIn("https://cerebrocentra.com", output.getvalue())
        request.assert_called_once_with(
            "https://cerebrocentra.com",
            "test-token",
            "/health",
        )

    def test_local_audits_stay_on_client_in_remote_mode(self):
        output = StringIO()
        with (
            patch.dict(
                os.environ,
                {
                    "CEREBRO_SERVER_URL": "https://cerebro.example",
                    "CEREBRO_API_TOKEN": "test-token",
                },
            ),
            patch("assistente.remote.show_welcome"),
            patch(
                "assistente.remote.read_prompt",
                side_effect=[
                    "/auditoria-elevacao",
                    "/auditoria-seguranca",
                    "/sair",
                ],
            ),
            patch(
                "assistente.remote.request_json",
                return_value={"model": "test"},
            ) as request,
            redirect_stdout(output),
        ):
            remote_chat()

        self.assertIn("não está disponível no modo remoto", output.getvalue())
        self.assertIn("somente os arquivos do projeto deste computador", output.getvalue())
        request.assert_called_once_with(
            "https://cerebro.example",
            "test-token",
            "/health",
        )

    def test_startup_upload_sends_only_sanitized_syntax_summary_over_https(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=None)
        response.read.return_value = b'{"status":"accepted"}'
        opener = Mock()
        opener.open.return_value = response
        with (
            patch.dict(
                os.environ,
                {
                    "CEREBRO_IMPROVEMENTS_UPLOAD_TOKEN": "u" * 40,
                    "CEREBRO_SERVER_URL": "https://cerebrocentra.com",
                    "CEREBRO_CF_ACCESS_CLIENT_ID": "access-client",
                    "CEREBRO_CF_ACCESS_CLIENT_SECRET": "access-secret",
                },
                clear=True,
            ),
            patch(
                "assistente.__main__.check_project_syntax",
                return_value="Problemas de sintaxe:\nsrc/private.py:2:5: invalid syntax\n"
                "[cerebro-report files=12 diagnostics=1]",
            ),
            patch(
                "assistente.__main__.urllib.request.build_opener",
                return_value=opener,
            ),
            patch("assistente.__main__.print_status"),
        ):
            asyncio.run(upload_startup_improvement_report())

        request = opener.open.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(request.full_url, "https://cerebrocentra.com/v1/improvements")
        self.assertEqual(payload["status"], "issues")
        self.assertEqual(payload["files_checked"], 12)
        self.assertEqual(payload["diagnostics_count"], 1)
        self.assertNotIn("src/private.py", request.data.decode())
        self.assertNotIn("invalid syntax", request.data.decode())
        self.assertEqual(
            request.get_header("Authorization"),
            "Bearer " + "u" * 40,
        )
        self.assertEqual(request.get_header("Cf-access-client-id"), "access-client")
        self.assertEqual(request.get_header("Cf-access-client-secret"), "access-secret")

    def test_bearer_token_is_required_and_checked(self):
        token = "t" * 40
        with patch.object(server, "API_TOKEN", token):
            with self.assertRaises(server.HTTPException) as missing:
                server.require_auth(None)
            self.assertEqual(missing.exception.status_code, 401)
            with self.assertRaises(server.HTTPException) as invalid:
                server.require_auth("Bearer wrong")
            self.assertEqual(invalid.exception.status_code, 401)
            server.require_auth(f"Bearer {token}")

    def test_health_and_chat_require_service_lifespan_and_token(self):
        token = "a" * 64
        with (
            patch.object(server, "API_TOKEN", token),
            patch.object(server, "ensure_model"),
            patch.object(server, "connect_mcp", new=AsyncMock(return_value=({}, {}))),
        ):
            with (
                patch.object(server, "API_TOKEN", token),
                patch.object(server, "ensure_model"),
                patch.object(server, "connect_mcp", new=AsyncMock(return_value=({}, {}))),
                TestClient(server.app) as client,
            ):
                self.assertEqual(client.get("/health").json()["status"], "ok")
                denied = client.post("/v1/chat", json={"session_id": "s", "message": "oi"})
                self.assertEqual(denied.status_code, 401)
                malformed = client.post(
                    "/v1/chat",
                    headers={"Authorization": f"Bearer {token}"},
                    json=[],
                )
                self.assertEqual(malformed.status_code, 400)

    def test_remote_auto_improve_command_is_converted_before_agent_invocation(self):
        token = "b" * 64
        graph = Mock()
        graph.ainvoke = AsyncMock(
            return_value={
                "messages": [{"role": "assistant", "content": "Relatório de melhorias."}],
                "agent": "geral",
            }
        )
        with (
            patch.object(server, "API_TOKEN", token),
            patch.object(server, "ensure_model"),
            patch.object(server, "connect_mcp", new=AsyncMock(return_value=({}, {}))),
            patch.object(server, "build_chat_graph", return_value=graph),
            TestClient(server.app) as client,
        ):
            response = client.post(
                "/v1/chat",
                headers={"Authorization": "Bearer " + token},
                json={"session_id": "auto-improve", "message": "/autoaprimorar"},
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["response"], "Relatório de melhorias.")
        initial_state = graph.ainvoke.await_args.args[0]
        self.assertTrue(
            initial_state["messages"][-1]["content"].startswith("[AUTOAPRIMORAR]")
        )

    def test_remote_hub_status_requires_auth(self):
        token = "c" * 64
        with (
            patch.object(server, "API_TOKEN", token),
            patch.object(server, "ensure_model"),
            patch.object(server, "connect_mcp", new=AsyncMock(return_value=({}, {}))),
            patch.object(server, "hub_statuses", return_value=[("openrouter", "configured")]),
        ):
            with TestClient(server.app) as client:
                denied = client.get("/v1/hubs")
                self.assertEqual(denied.status_code, 401)
                response = client.get(
                    "/v1/hubs",
                    headers={"Authorization": "Bearer " + token},
                )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"hubs": [{"name": "openrouter", "status": "configured"}]},
        )

    def test_structural_report_upload_uses_separate_token_and_stores_only_aggregate_fields(self):
        api_token = "a" * 40
        upload_token = "u" * 40
        report = {
            "schema_version": 1,
            "report_id": str(uuid.uuid4()),
            "check": "python_syntax",
            "status": "issues",
            "files_checked": 17,
            "diagnostics_count": 2,
        }
        with (
            patch.object(server, "API_TOKEN", api_token),
            patch.object(server, "IMPROVEMENTS_UPLOAD_TOKEN", upload_token),
            patch.object(server, "ensure_model"),
            patch.object(server, "connect_mcp", new=AsyncMock(return_value=({}, {}))),
            TestClient(server.app) as client,
        ):
            denied = client.post("/v1/improvements", json=report)
            wrong_token = client.post(
                "/v1/improvements",
                headers={"Authorization": "Bearer " + api_token},
                json=report,
            )
            accepted = client.post(
                "/v1/improvements",
                headers={"Authorization": "Bearer " + upload_token},
                json=report,
            )
            duplicate = client.post(
                "/v1/improvements",
                headers={"Authorization": "Bearer " + upload_token},
                json=report,
            )

        self.assertEqual(denied.status_code, 401)
        self.assertEqual(wrong_token.status_code, 401)
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertEqual(accepted.json()["status"], "accepted")
        self.assertEqual(duplicate.status_code, 200)
        self.assertEqual(duplicate.json()["status"], "duplicate")
        database = Path(self.data_directory.name) / "improvements.sqlite3"
        with sqlite3.connect(database) as connection:
            rows = connection.execute(
                "SELECT check_name, status, files_checked, diagnostics_count "
                "FROM improvement_reports"
            ).fetchall()
        self.assertEqual(rows, [("python_syntax", "issues", 17, 2)])

    def test_structural_report_upload_validates_payload_and_limits_frequency(self):
        token = "u" * 40
        report = {
            "schema_version": 1,
            "report_id": str(uuid.uuid4()),
            "check": "python_syntax",
            "status": "clean",
            "files_checked": 3,
            "diagnostics_count": 0,
        }
        with (
            patch.object(server, "API_TOKEN", "a" * 40),
            patch.object(server, "IMPROVEMENTS_UPLOAD_TOKEN", token),
            patch.object(server, "ensure_model"),
            patch.object(server, "connect_mcp", new=AsyncMock(return_value=({}, {}))),
            TestClient(server.app) as client,
        ):
            invalid = client.post(
                "/v1/improvements",
                headers={"Authorization": "Bearer " + token},
                json={**report, "unexpected": "field"},
            )
            oversized = client.post(
                "/v1/improvements",
                headers={"Authorization": "Bearer " + token},
                content=b"x" * (server.MAX_IMPROVEMENT_REPORT_BYTES + 1),
            )
            first = client.post(
                "/v1/improvements",
                headers={"Authorization": "Bearer " + token},
                json=report,
            )
            second = client.post(
                "/v1/improvements",
                headers={"Authorization": "Bearer " + token},
                json={**report, "report_id": str(uuid.uuid4())},
            )

        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(oversized.status_code, 413)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 429)

    def test_remote_hub_directory_requires_auth(self):
        token = "d" * 64
        with (
            patch.object(server, "API_TOKEN", token),
            patch.object(server, "ensure_model"),
            patch.object(server, "connect_mcp", new=AsyncMock(return_value=({}, {}))),
            patch.object(server, "hub_directory", return_value="Plataformas disponíveis"),
            patch.object(server, "hub_browser_url", return_value="https://app.crewai.com/"),
        ):
            with TestClient(server.app) as client:
                denied = client.get("/v1/hub-directory?path=google%2Fgemini")
                self.assertEqual(denied.status_code, 401)
                response = client.get(
                    "/v1/hub-directory?path=google%2Fgemini",
                    headers={"Authorization": "Bearer " + token},
                )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "content": "Plataformas disponíveis",
                "browser_url": "https://app.crewai.com/",
            },
        )

    def test_remote_chat_uses_graph_and_returns_selected_agent(self):
        token = "b" * 64
        response_iter = iter(
            (
                {"message": {"content": json.dumps({"agent": "geral"})}},
                {"message": {"content": "Olá do servidor."}},
            )
        )
        with (
            patch.object(server, "API_TOKEN", token),
            patch.object(server, "ensure_model"),
            patch.object(server, "connect_mcp", new=AsyncMock(return_value=({}, {}))),
            patch(
                "assistente.__main__.http_json",
                side_effect=lambda *args: next(response_iter),
            ) as model,
        ):
            with TestClient(server.app) as client:
                result = client.post(
                    "/v1/chat",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"session_id": "test-session", "message": "Olá"},
                )
                run_id = result.json().get("run_id")
                denied = client.get("/v1/runs")
                runs = client.get(
                    "/v1/runs",
                    headers={"Authorization": "Bearer " + token},
                )
                events = client.get(
                    f"/v1/runs/{run_id}/events",
                    headers={"Authorization": "Bearer " + token},
                )
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["agent"], "geral")
        self.assertEqual(result.json()["response"], "Olá do servidor.")
        self.assertEqual(len(run_id), 32)
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(runs.status_code, 200)
        self.assertEqual(runs.json()["runs"][0]["id"], run_id)
        self.assertEqual(events.status_code, 200)
        self.assertEqual(events.json()["events"][-1]["event"], "run.finished")
        remote_system = model.call_args_list[1].args[1]["messages"][0]["content"]
        self.assertIn("Você é o Cérebro Central", remote_system)
        self.assertIn("plataforma principal de orquestração", remote_system)
        self.assertIn("agentes especialistas são capacidades internas", remote_system)

    def test_remote_chat_intercepts_training_commands_when_android_is_disabled(self):
        token = "e" * 64
        with (
            patch.object(server, "API_TOKEN", token),
            patch.object(server, "ENABLE_DESKTOP", False),
            patch.object(server, "ensure_model"),
            patch.object(server, "connect_mcp", new=AsyncMock(return_value=({}, {}))),
            patch("assistente.__main__.http_json") as model,
            TestClient(server.app) as client,
        ):
            activation = client.post(
                "/v1/chat",
                headers={"Authorization": "Bearer " + token},
                json={
                    "session_id": "test-session",
                    "message": "ativar modo de treinamento",
                },
            )
            status = client.post(
                "/v1/chat",
                headers={"Authorization": "Bearer " + token},
                json={"session_id": "test-session", "message": "/grindr status"},
            )

        self.assertEqual(activation.status_code, 200, activation.text)
        self.assertIn("Treinamento não ativado", activation.json()["response"])
        self.assertIn("CEREBRO_ENABLE_DESKTOP=1", activation.json()["response"])
        self.assertIn("treinamento: desativado", status.json()["response"])
        model.assert_not_called()

    def test_remote_chat_activates_training_without_enabling_drafts(self):
        token = "f" * 64
        with (
            patch.object(server, "API_TOKEN", token),
            patch.object(server, "ENABLE_DESKTOP", True),
            patch.object(server, "_grindr_background_monitor", new=AsyncMock()),
            patch.object(server, "ensure_model"),
            patch.object(server, "connect_mcp", new=AsyncMock(return_value=({}, {}))),
            patch("assistente.__main__.http_json") as model,
            TestClient(server.app) as client,
        ):
            activation = client.post(
                "/v1/chat",
                headers={"Authorization": "Bearer " + token},
                json={"session_id": "test-session", "message": "ativar modo de treinamento"},
            )
            status = client.post(
                "/v1/chat",
                headers={"Authorization": "Bearer " + token},
                json={"session_id": "test-session", "message": "/grindr status"},
            )

        self.assertEqual(activation.status_code, 200, activation.text)
        self.assertIn("Treinamento ativado", activation.json()["response"])
        self.assertIn("treinamento: ativado", status.json()["response"])
        self.assertIn("Rascunhos automáticos: desativados", status.json()["response"])
        model.assert_not_called()

    def test_tools_list_and_direct_tool_execution(self):
        token = "d" * 64
        with (
            patch.object(server, "API_TOKEN", token),
            patch.object(server, "ensure_model"),
            patch.object(server, "connect_mcp", new=AsyncMock(return_value=({}, {}))),
            TestClient(server.app) as client,
        ):
            # Teste de listagem de ferramentas
            list_res = client.get(
                "/v1/tools",
                headers={"Authorization": f"Bearer {token}"},
            )
            self.assertEqual(list_res.status_code, 200)
            tools = list_res.json().get("tools", [])
            tool_names = [t["function"]["name"] for t in tools]
            self.assertIn("terminal_execute", tool_names)
            self.assertIn("project_read_file", tool_names)

            with patch.object(server.approval_broker, "request", new=AsyncMock(return_value=True)):
                exec_res = client.post(
                    "/v1/tools/execute",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "name": "terminal_execute",
                        "arguments": {"command": ["python3", "-c", "print('ORCHESTRATOR_OK')"]},
                    },
                )
                self.assertEqual(exec_res.status_code, 200)
                self.assertIn("ORCHESTRATOR_OK", exec_res.json().get("output", ""))


class ApprovalBrokerTests(unittest.IsolatedAsyncioTestCase):
    async def test_action_waits_for_remote_approval(self):
        broker = server.ApprovalBroker()
        waiting = asyncio.create_task(broker.request("session-1", "browser_click(#submit)"))
        await asyncio.sleep(0)
        action_id = next(iter(broker.pending))
        self.assertEqual(broker.pending[action_id]["session_id"], "session-1")
        broker.pending[action_id]["future"].set_result(True)
        self.assertTrue(await waiting)
        self.assertNotIn(action_id, broker.pending)


if __name__ == "__main__":
    unittest.main()
