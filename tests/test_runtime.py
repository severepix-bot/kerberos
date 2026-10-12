import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from assistente.runtime import (
    MAX_LEARNING_TEXT_CHARS,
    MAX_LEARNINGS,
    RuntimeStore,
    sanitize_learning_text,
    trim_context,
)
from assistente.workspace import prepare_workspace, resolve_workspace_paths


class ContextManagementTests(unittest.TestCase):
    def test_trims_old_complete_turns_and_keeps_system_message(self):
        messages = [
            {"role": "system", "content": "Instruções."},
            {"role": "user", "content": "Pedido antigo"},
            {"role": "assistant", "content": "Resposta antiga"},
            {"role": "user", "content": "Pedido atual"},
            {"role": "assistant", "content": "Resposta atual"},
        ]

        retained = trim_context(messages, max_chars=100)

        self.assertEqual(retained[0], messages[0])
        self.assertEqual(
            [message["content"] for message in retained[1:]],
            ["Pedido atual", "Resposta atual"],
        )

    def test_keeps_the_entire_current_turn_even_when_it_exceeds_budget(self):
        current_turn = [
            {"role": "user", "content": "Faça a ação"},
            {"role": "assistant", "tool_calls": [{"id": "call-1"}]},
            {"role": "tool", "content": "Resultado"},
        ]
        messages = [{"role": "system", "content": "Instruções."}] + current_turn

        self.assertEqual(trim_context(messages, max_chars=1), messages)


class RuntimeStoreTests(unittest.TestCase):
    def test_persists_identity_and_checkpoint_with_private_recovery_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "runtime.sqlite3"
            store = RuntimeStore(database)
            identity = store.identity()
            store.set_checkpoint("suite local concluída")

            reopened = RuntimeStore(database)

            self.assertEqual(reopened.identity()["agent_id"], identity["agent_id"])
            self.assertEqual(reopened.identity()["name"], "Kerberos")
            self.assertEqual(reopened.identity()["last_checkpoint"], "suite local concluída")
            self.assertTrue(Path(f"{database}.bak").is_file())
            if os.name != "nt":
                self.assertEqual(Path(f"{database}.bak").stat().st_mode & 0o777, 0o600)

    def test_restores_corrupted_or_missing_database_from_valid_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "runtime.sqlite3"
            store = RuntimeStore(database)
            store.set_checkpoint("estado preservado")
            agent_id = store.identity()["agent_id"]
            database.write_bytes(b"database corrupted")

            recovered = RuntimeStore(database)

            self.assertEqual(recovered.identity()["agent_id"], agent_id)
            self.assertEqual(recovered.identity()["last_checkpoint"], "estado preservado")

            database.unlink()
            recovered_from_missing = RuntimeStore(database)
            self.assertEqual(recovered_from_missing.identity()["agent_id"], agent_id)

    def test_does_not_overwrite_database_when_no_valid_snapshot_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "runtime.sqlite3"
            snapshot = Path(f"{database}.bak")
            database.write_bytes(b"damaged database")
            snapshot.write_bytes(b"damaged snapshot")

            with self.assertRaises(sqlite3.DatabaseError):
                RuntimeStore(database)

            self.assertEqual(database.read_bytes(), b"damaged database")
            self.assertEqual(snapshot.read_bytes(), b"damaged snapshot")

    def test_refuses_recovery_while_sqlite_sidecars_exist(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "runtime.sqlite3"
            store = RuntimeStore(database)
            store.set_checkpoint("estado preservado")
            database.write_bytes(b"damaged database")
            journal = Path(f"{database}-wal")
            journal.touch()

            with self.assertRaisesRegex(sqlite3.DatabaseError, "Feche todas as instâncias"):
                RuntimeStore(database)

            self.assertEqual(database.read_bytes(), b"damaged database")

    def test_concurrent_writes_keep_snapshot_at_least_as_current_as_database(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "runtime.sqlite3"
            first_store = RuntimeStore(database)
            second_store = RuntimeStore(database)

            with ThreadPoolExecutor(max_workers=2) as executor:
                list(
                    executor.map(
                        lambda store: store.start_run("concurrent"),
                        (first_store, second_store),
                    )
                )

            with sqlite3.connect(database) as main:
                main_count = main.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
            with sqlite3.connect(f"{database}.bak") as snapshot:
                snapshot_count = snapshot.execute("SELECT COUNT(*) FROM runs").fetchone()[0]

        self.assertEqual(main_count, 2)
        self.assertEqual(snapshot_count, main_count)

    def test_learning_store_deduplicates_redacts_retrieves_and_stays_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RuntimeStore(Path(directory) / "runtime.sqlite3")

            self.assertTrue(
                store.save_learning(
                    "pytest",
                    "Use focused pytest tests before the full suite; api_key=secret-value",
                )
            )
            self.assertTrue(
                store.save_learning(
                    "test workflow",
                    "Use focused pytest tests before the full suite; api_key=another-value",
                )
            )
            notes = store.list_learnings()
            relevant = store.relevant_learnings("pytest focused tests")
            self.assertEqual(len(notes), 1)
            self.assertIn("[redacted]", notes[0]["lesson"])
            self.assertTrue(relevant)
            self.assertIn("focused pytest", relevant[0]["lesson"])

            for index in range(MAX_LEARNINGS + 10):
                store.save_learning(f"topic-{index}", f"Lesson {index}: " + "x" * 390)

            notes = store.list_learnings()
            count, chars = store.learning_usage()

        self.assertLessEqual(count, MAX_LEARNINGS)
        self.assertLessEqual(chars, MAX_LEARNING_TEXT_CHARS)
        self.assertTrue(all(len(item["lesson"]) <= 400 for item in notes))

    def test_learning_sanitizer_redacts_common_credentials_and_limits_expansion(self):
        text = (
            "AWS AKIA1234567890ABCDEF; Google AIza"
            + "A" * 35
            + "; token=super-secret-value; user@example.com; Bearer "
            + "X" * 30
        )

        sanitized = sanitize_learning_text(text, 500)

        self.assertNotIn("AKIA1234567890ABCDEF", sanitized)
        self.assertNotIn("AIza", sanitized)
        self.assertNotIn("super-secret-value", sanitized)
        self.assertNotIn("user@example.com", sanitized)
        self.assertNotIn("X" * 30, sanitized)
        self.assertLessEqual(len(sanitize_learning_text("x" * 100, 10)), 10)

    def test_learning_notes_can_be_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RuntimeStore(Path(directory) / "runtime.sqlite3")
            store.save_learning("pytest", "Run focused tests before full suite.")
            store.delete_learnings()

            self.assertEqual(store.list_learnings(), [])
            self.assertEqual(store.learning_usage(), (0, 0))

    def test_records_lifecycle_and_structured_events_without_message_content(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RuntimeStore(Path(directory) / "runtime.sqlite3")
            run_id = store.start_run("local")
            store.record_event(
                run_id,
                "agent.selected",
                "orchestrator",
                "completed",
                agent="geral",
            )
            store.record_event(
                run_id,
                "tool.finished",
                "tool",
                "completed",
                agent="geral",
                tool="browser_snapshot",
            )
            store.finish_run(run_id, "completed")

            (run,) = store.recent_runs()
            events = store.run_events(run_id)
            identity = store.identity()

        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["agent"], "geral")
        self.assertEqual(
            [event["event"] for event in events],
            [
                "run.started",
                "agent.selected",
                "tool.finished",
                "run.finished",
            ],
        )
        self.assertEqual(events[2]["tool"], "browser_snapshot")
        self.assertNotIn("content", events[2])
        self.assertNotIn("prompt", run)
        self.assertEqual(identity["last_checkpoint"], run_id)

    def test_rejects_invalid_final_status_and_query_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RuntimeStore(Path(directory) / "runtime.sqlite3")
            run_id = store.start_run("local")

            with self.assertRaises(ValueError):
                store.finish_run(run_id, "running")
            with self.assertRaises(ValueError):
                store.recent_runs(0)
            with self.assertRaises(ValueError):
                store.run_events(run_id, 501)

    def test_workspace_profiles_keep_runtime_databases_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            first = resolve_workspace_paths(
                {"CEREBRO_WORKSPACE": "test-one"},
                home=home,
            )
            second = resolve_workspace_paths(
                {"CEREBRO_WORKSPACE": "test-two"},
                home=home,
            )
            prepare_workspace(first)
            prepare_workspace(second)
            first_store = RuntimeStore(first.data_dir / "runtime.sqlite3")
            second_store = RuntimeStore(second.data_dir / "runtime.sqlite3")

            first_run = first_store.start_run("local")
            first_runs = first_store.recent_runs()
            second_runs = second_store.recent_runs()

        self.assertNotEqual(first.root, second.root)
        self.assertNotEqual(first.mcp_config, second.mcp_config)
        self.assertNotEqual(first.hubs_config, second.hubs_config)
        self.assertEqual(first_run, first_runs[0]["id"])
        self.assertEqual(second_runs, [])

    def test_workspace_name_is_validated_and_legacy_paths_are_preserved(self):
        home = Path("/tmp/cerebro-test-home")
        legacy = resolve_workspace_paths({}, home=home)
        self.assertEqual(legacy.data_dir, home / ".local/share/cerebro")
        self.assertEqual(legacy.mcp_config, home / ".config/assistente-local/mcp.json")
        self.assertEqual(legacy.hubs_config, home / ".config/cerebro/hubs.json")

        for invalid_name in ("../other", "two words", "Upper", "a/b", "-test"):
            with self.subTest(name=invalid_name), self.assertRaises(ValueError):
                resolve_workspace_paths(
                    {"CEREBRO_WORKSPACE": invalid_name},
                    home=home,
                )

    def test_workspace_directories_are_private_on_posix(self):
        if os.name == "nt":
            self.skipTest("Permissões POSIX não estão disponíveis.")
        with tempfile.TemporaryDirectory() as directory:
            paths = resolve_workspace_paths(
                {"CEREBRO_WORKSPACE": "private-test"},
                home=Path(directory),
            )
            prepare_workspace(paths)

            self.assertEqual(paths.root.stat().st_mode & 0o777, 0o700)
            self.assertEqual(paths.data_dir.stat().st_mode & 0o777, 0o700)

    def test_workspace_rejects_symlinked_profile_directory(self):
        if os.name == "nt":
            self.skipTest("Este teste requer links simbólicos POSIX.")
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            paths = resolve_workspace_paths(
                {"CEREBRO_WORKSPACE": "linked"},
                home=home,
            )
            external = home / "outside"
            external.mkdir()
            profile_parent = paths.root.parent
            profile_parent.mkdir(parents=True)
            paths.root.symlink_to(external, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "links simbólicos"):
                prepare_workspace(paths)

            self.assertFalse((external / "data").exists())


if __name__ == "__main__":
    unittest.main()
