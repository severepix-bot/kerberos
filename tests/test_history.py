import json
import os
import sqlite3
import tempfile
import unittest
import zlib
from pathlib import Path

from assistente.history import (
    ConversationEvent,
    ConversationHistoryStore,
    ConversationTurn,
    discover_history_turns,
    discover_project_events,
    parse_codex_rollout,
    parse_vscode_session,
    workspace_from_metadata,
)


class ConversationHistoryTests(unittest.TestCase):
    def test_codex_parser_keeps_user_turns_and_completed_final_answers(self):
        with tempfile.TemporaryDirectory() as directory:
            rollout = Path(directory) / "rollout.jsonl"
            records = [
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "id": "user-turn-1",
                        "content": [{"type": "input_text", "text": "Corrija o código."}],
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "phase": "commentary",
                        "content": [{"type": "output_text", "text": "Comentário não final."}],
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "phase": "final_answer",
                        "content": [{"type": "output_text", "text": "Código corrigido."}],
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "user",
                        "id": "user-turn-2",
                        "content": [{"type": "input_text", "text": "Agora rode os testes."}],
                    },
                },
                {"malformed": "incomplete"},
            ]
            rollout.write_text(
                "\n".join(json.dumps(record) for record in records), encoding="utf-8"
            )

            turns = list(
                parse_codex_rollout(
                    rollout,
                    source="codex-cli",
                    session_id="session-1",
                    workspace="/workspace",
                )
            )

        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0].turn_id, "user-turn-1")
        self.assertEqual(turns[0].user_message, "Corrija o código.")
        self.assertEqual(turns[0].assistant_response, "Código corrigido.")
        self.assertNotIn("Raciocínio", turns[0].assistant_response)

    def test_vscode_parser_uses_prompt_and_non_thinking_response(self):
        with tempfile.TemporaryDirectory() as directory:
            session = Path(directory) / "session.jsonl"
            session.write_text(
                json.dumps(
                    {
                        "kind": 2,
                        "v": [
                            {
                                "requestId": "request-1",
                                "timestamp": 123,
                                "message": {"text": "Corrija o código."},
                                "response": [
                                    {"kind": "thinking", "value": "Não indexar."},
                                    {"kind": None, "value": "Código corrigido."},
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            turns = list(parse_vscode_session(session, workspace="/workspace"))

        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0].source, "vscode")
        self.assertEqual(turns[0].turn_id, "request-1")
        self.assertEqual(turns[0].assistant_response, "Código corrigido.")

    def test_workspace_metadata_accepts_file_urls_and_rejects_other_schemes(self):
        with tempfile.TemporaryDirectory() as directory:
            metadata = Path(directory) / "workspace.json"
            metadata.write_text(
                json.dumps({"workspace": "file:///tmp/project%20folder"}), encoding="utf-8"
            )

            self.assertEqual(
                Path(workspace_from_metadata(metadata)),
                Path("/tmp/project folder").resolve(),
            )
            metadata.write_text(json.dumps({"workspace": "vscode-remote://host/path"}))
            self.assertEqual(workspace_from_metadata(metadata), "")

    def test_discovery_reads_vscode_and_codex_sources_without_scanning_other_codex_paths(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            vscode_root = home / "workspaceStorage"
            vscode_workspace = vscode_root / "workspace-id"
            session_dir = vscode_workspace / "chatSessions"
            session_dir.mkdir(parents=True)
            (vscode_workspace / "workspace.json").write_text(
                json.dumps({"workspace": "file:///project/kerberos"}), encoding="utf-8"
            )
            (session_dir / "vscode-session.jsonl").write_text(
                json.dumps(
                    {
                        "kind": 2,
                        "v": [
                            {
                                "requestId": "vscode-turn",
                                "message": {"text": "Corrija o código"},
                                "response": [{"value": "Código corrigido"}],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            codex_root = home / ".codex"
            rollout = codex_root / "sessions" / "rollout.jsonl"
            rollout.parent.mkdir(parents=True)
            rollout.write_text(
                "\n".join(
                    json.dumps(record)
                    for record in (
                        {
                            "type": "response_item",
                            "payload": {
                                "type": "user",
                                "id": "codex-turn",
                                "content": [{"type": "input_text", "text": "Rode o teste"}],
                            },
                        },
                        {
                            "type": "response_item",
                            "payload": {
                                "type": "assistant",
                                "phase": "final",
                                "content": [{"type": "output_text", "text": "Teste aprovado"}],
                            },
                        },
                    )
                ),
                encoding="utf-8",
            )
            database = codex_root / "state_5.sqlite"
            with sqlite3.connect(database) as connection:
                connection.execute(
                    """
                    CREATE TABLE threads (
                        id TEXT, rollout_path TEXT, source TEXT, cwd TEXT, created_at INTEGER
                    )
                    """
                )
                connection.executemany(
                    "INSERT INTO threads VALUES (?, ?, ?, ?, ?)",
                    [
                        ("cli-1", str(rollout), "cli", "/project/kerberos", 1),
                        ("other-1", str(rollout), "remote", "/project/kerberos", 2),
                    ],
                )

            turns = list(
                discover_history_turns(home=home, vscode_root=vscode_root, codex_db=database)
            )

        self.assertEqual({turn.source for turn in turns}, {"vscode", "codex-cli"})
        self.assertEqual(len(turns), 2)

    def test_import_is_idempotent_redacts_credentials_and_search_is_project_scoped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "private" / "training.sqlite3"
            workspace = root / "kerberos"
            sibling = root / "other-project"
            store = ConversationHistoryStore(database)
            turn = ConversationTurn(
                source="vscode",
                session_id="session-1",
                turn_id="turn-1",
                workspace=str(workspace),
                user_message="Corrija a busca FTS.",
                assistant_response="Use consulta FTS5; api_key=secret-value.",
            )

            self.assertEqual(store.import_turns([turn]), 1)
            self.assertEqual(store.import_turns([turn]), 1)
            self.assertEqual(store.count(), 1)
            results = store.search("busca FTS5", workspace=workspace)
            unrelated = store.search("busca FTS5", workspace=sibling)
            cross_project = store.search("busca FTS5", workspace=None)
            with sqlite3.connect(database) as connection:
                raw = connection.execute(
                    "SELECT assistant_response FROM conversation_turns"
                ).fetchone()[0]

            self.assertEqual(len(results), 1)
            self.assertEqual(len(cross_project), 1)
            self.assertIn("[redacted]", results[0]["assistant_response"])
            self.assertNotIn("secret-value", raw)
            self.assertEqual(unrelated, [])
            self.assertEqual(store.search("%%%", workspace=workspace), [])
            updated = ConversationTurn(
                source=turn.source,
                session_id=turn.session_id,
                turn_id=turn.turn_id,
                workspace=turn.workspace,
                user_message=turn.user_message,
                assistant_response="Consulta FTS atualizada.",
            )
            store.import_turns([updated])
            updated_results = store.search("atualizada", workspace=workspace)
            self.assertEqual(len(updated_results), 1)
            self.assertEqual(updated_results[0]["assistant_response"], "Consulta FTS atualizada.")
            if os.name != "nt":
                self.assertEqual(database.stat().st_mode & 0o777, 0o600)
                self.assertEqual(database.parent.stat().st_mode & 0o777, 0o700)

    def test_search_is_bounded_and_refuses_excessive_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "kerberos"
            store = ConversationHistoryStore(Path(directory) / "training.sqlite3")
            turns = [
                ConversationTurn(
                    source="codex-cli",
                    session_id="session",
                    turn_id=str(index),
                    workspace=str(workspace),
                    user_message=f"Corrija o código {index} " + "código " * 500,
                    assistant_response="Resposta " + "teste " * 500,
                )
                for index in range(5)
            ]
            store.import_turns(turns)

            results = store.search("código teste", workspace=workspace, limit=3, max_chars=500)
            self.assertLessEqual(len(results), 3)
            self.assertLessEqual(
                sum(
                    len(item["user_message"]) + len(item["assistant_response"]) for item in results
                ),
                500,
            )
            with self.assertRaises(ValueError):
                store.search("código", workspace=workspace, limit=4)

    def test_project_rollouts_import_user_calls_results_and_final_answers(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            project = home / "Desktop" / "www-italosantos-com"
            project.mkdir(parents=True)
            codex = home / ".codex"
            rollout = codex / "sessions" / "rollout.jsonl"
            rollout.parent.mkdir(parents=True)
            records = [
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "id": "user-1",
                        "content": [{"type": "input_text", "text": "Configure deploy"}],
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "exec_command",
                        "call_id": "call-1",
                        "arguments": json.dumps(
                            {"cmd": ["npm", "run", "deploy"], "api_key": "secret"}
                        ),
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": "call-1",
                        "output": "Deploy failed\nExit code: 1",
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "phase": "final_answer",
                        "id": "answer-1",
                        "content": [{"type": "output_text", "text": "O deploy falhou."}],
                    },
                },
            ]
            rollout.write_text(
                "\n".join(json.dumps(record) for record in records), encoding="utf-8"
            )
            database = codex / "state_5.sqlite"
            with sqlite3.connect(database) as connection:
                connection.execute(
                    """
                    CREATE TABLE threads (
                        id TEXT, rollout_path TEXT, source TEXT, cwd TEXT, created_at INTEGER
                    )
                    """
                )
                connection.executemany(
                    "INSERT INTO threads VALUES (?, ?, ?, ?, ?)",
                    [
                        ("site", str(rollout), "cli", str(project), 1),
                        ("other", str(rollout), "cli", str(home / "Desktop" / "other"), 2),
                        ("outside", str(home / "outside.jsonl"), "cli", str(project), 3),
                    ],
                )

            events = list(discover_project_events("www-italosantos-com", home=home))

        self.assertEqual(
            [event.event_type for event in events],
            [
                "user_message",
                "tool_call",
                "tool_result",
                "assistant_final",
            ],
        )
        self.assertEqual(events[1].status, "pending")
        self.assertEqual(events[2].status, "error")
        self.assertEqual(events[2].tool_name, "exec_command")
        self.assertEqual(events[2].turn_id, "user-1")

    def test_project_events_are_private_idempotent_and_searchable_without_losing_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "Desktop" / "www-italosantos-com"
            store = ConversationHistoryStore(root / "private" / "training.sqlite3")
            events = [
                ConversationEvent(
                    source="codex-cli",
                    session_id="deploy-session",
                    event_id="call-1:call",
                    turn_id="user-1",
                    workspace=str(workspace),
                    event_order=1,
                    event_type="tool_call",
                    tool_name="exec_command",
                    status="pending",
                    payload={"cmd": "npm run deploy", "api_key": "secret-value"},
                ),
                ConversationEvent(
                    source="codex-cli",
                    session_id="deploy-session",
                    event_id="call-1:result",
                    turn_id="user-1",
                    workspace=str(workspace),
                    event_order=2,
                    event_type="tool_result",
                    tool_name="exec_command",
                    status="error",
                    payload="Deployment failed with Exit code: 1 and error detail.",
                ),
            ]

            self.assertEqual(store.import_events(events), 2)
            self.assertEqual(store.import_events(events), 2)
            results = store.search_events(
                "Exit code",
                project_name="www-italosantos-com",
            )
            unrelated = store.search_events(
                "Exit code",
                project_name="different-project",
            )
            with sqlite3.connect(store.path) as connection:
                event_count = connection.execute(
                    "SELECT COUNT(*) FROM conversation_events"
                ).fetchone()[0]
                stored_secret = connection.execute(
                    "SELECT payload_z FROM conversation_events WHERE event_id = ?",
                    ("call-1:call",),
                ).fetchone()[0]
                payload = zlib.decompress(stored_secret).decode()
            database_mode = store.path.stat().st_mode & 0o777
            directory_mode = store.path.parent.stat().st_mode & 0o777

        self.assertEqual(event_count, 2)
        self.assertEqual(len(results), 1)
        self.assertIn("Exit code: 1", results[0]["assistant_response"])
        self.assertNotIn("secret-value", payload)
        self.assertEqual(unrelated, [])
        if os.name != "nt":
            self.assertEqual(database_mode, 0o600)
            self.assertEqual(directory_mode, 0o700)


if __name__ == "__main__":
    unittest.main()
