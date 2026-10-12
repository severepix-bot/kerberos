import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from assistente.training import (
    MAX_TRACE_CHARS,
    MAX_TRACE_EVENTS,
    _append_trace,
    _validated_prepared_dataset,
    collect_samples,
    condition_samples_with_learnings,
    iter_codex_samples,
    iter_imported_history_samples,
    iter_project_event_samples,
    iter_vscode_samples,
    load_long_term_learnings,
    prepare_mlx_dataset,
    sanitize_training_text,
    train_huggingface,
    write_dataset,
)


class CharacterTokenizer:
    @staticmethod
    def encode(text, add_special_tokens=False):
        return list(text)

    @staticmethod
    def decode(tokens, skip_special_tokens=True):
        return "".join(tokens)

    @staticmethod
    def apply_chat_template(
        messages,
        tokenize=True,
        add_generation_prompt=False,
        return_dict=False,
    ):
        length = sum(len(message["content"]) for message in messages)
        length += 5 * len(messages)
        if add_generation_prompt:
            length += 5
        return [1] * length


class TrainingHistoryTests(unittest.TestCase):
    def test_huggingface_requires_prepared_dataset_with_all_splits(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            training_root = root / "private-training"
            dataset = training_root / "datasets" / "reviewed"
            dataset.mkdir(parents=True)
            for split in ("train", "valid", "test"):
                (dataset / f"{split}.jsonl").write_text("{}\n", encoding="utf-8")
            outside = root / "outside"
            outside.mkdir()
            for split in ("train", "valid", "test"):
                (outside / f"{split}.jsonl").write_text("{}\n", encoding="utf-8")

            with patch.dict(
                os.environ,
                {"CEREBRO_TRAINING_DIR": str(training_root)},
                clear=False,
            ):
                self.assertEqual(
                    _validated_prepared_dataset(dataset),
                    dataset.resolve(),
                )
                with self.assertRaisesRegex(ValueError, "dataset preparado"):
                    _validated_prepared_dataset(outside)

    def test_memory_guidance_conditions_only_real_matching_conversation_pairs(self):
        original_answer = "Run the focused test suite before the full suite."
        samples = [
            {
                "identity": "history:1",
                "fingerprint": "original",
                "messages": [
                    {"role": "user", "content": "Add focused pytest tests for terminal access."},
                    {"role": "assistant", "content": original_answer},
                ],
            },
            {
                "identity": "history:2",
                "fingerprint": "unrelated",
                "messages": [
                    {"role": "user", "content": "Explain the color palette."},
                    {"role": "assistant", "content": "Use a restrained palette."},
                ],
            },
        ]
        learnings = [
            {
                "topic": "pytest discipline",
                "lesson": "Run focused pytest tests before the full suite.",
            }
        ]

        conditioned, notes_used = condition_samples_with_learnings(samples, learnings)

        self.assertEqual(len(conditioned), 1)
        self.assertEqual(notes_used, 1)
        self.assertIn("Orientações de longo prazo", conditioned[0]["messages"][0]["content"])
        self.assertIn("Add focused pytest tests", conditioned[0]["messages"][0]["content"])
        self.assertEqual(conditioned[0]["messages"][1]["content"], original_answer)
        self.assertEqual(
            samples[0]["messages"][0]["content"], "Add focused pytest tests for terminal access."
        )

    def test_long_term_learnings_are_read_only_and_training_sanitized(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "runtime.sqlite3"
            with sqlite3.connect(database) as connection:
                connection.execute(
                    """
                    CREATE TABLE learnings (
                        id INTEGER PRIMARY KEY, topic TEXT, lesson TEXT,
                        fingerprint TEXT, created_at TEXT
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO learnings VALUES (1, ?, ?, ?, CURRENT_TIMESTAMP)",
                    (
                        "pytest",
                        "Email person@example.com; token=secret-value",
                        "fingerprint",
                    ),
                )

            learnings = load_long_term_learnings(database)

        self.assertEqual(len(learnings), 1)
        self.assertNotIn("person@example.com", learnings[0]["lesson"])
        self.assertNotIn("secret-value", learnings[0]["lesson"])
        self.assertIn("[REDACTED]", learnings[0]["lesson"])

    def test_long_term_learnings_ignore_symlinked_runtime_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            actual = root / "actual.sqlite3"
            with sqlite3.connect(actual) as connection:
                connection.execute(
                    "CREATE TABLE learnings (topic TEXT, lesson TEXT, created_at TEXT, id INTEGER)"
                )
                connection.execute(
                    "INSERT INTO learnings VALUES ('topic', 'lesson', CURRENT_TIMESTAMP, 1)"
                )
            link = root / "linked.sqlite3"
            link.symlink_to(actual)

            self.assertEqual(load_long_term_learnings(link), [])

    def test_long_term_learnings_follow_active_workspace_data_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            runtime_database = base / "workspaces" / "training-test" / "data" / "runtime.sqlite3"
            runtime_database.parent.mkdir(parents=True)
            with sqlite3.connect(runtime_database) as connection:
                connection.execute(
                    "CREATE TABLE learnings (topic TEXT, lesson TEXT, created_at TEXT, id INTEGER)"
                )
                connection.execute(
                    "INSERT INTO learnings VALUES ('pytest', 'Run focused tests', CURRENT_TIMESTAMP, 1)"
                )
            with patch.dict(
                os.environ,
                {
                    "CEREBRO_WORKSPACE": "training-test",
                    "CEREBRO_DATA_DIR": str(base),
                },
                clear=False,
            ):
                learnings = load_long_term_learnings()

        self.assertEqual(learnings, [{"topic": "pytest", "lesson": "Run focused tests"}])

    def test_project_trace_keeps_recent_bounded_execution_context(self):
        trace = []
        _append_trace(trace, "resultado " + ("x" * (MAX_TRACE_CHARS * 10)))
        for index in range(MAX_TRACE_EVENTS + 2):
            _append_trace(trace, f"evento {index}")

        self.assertLessEqual(len(trace), MAX_TRACE_EVENTS)
        self.assertLessEqual(sum(map(len, trace)), MAX_TRACE_CHARS)
        self.assertIn("evento", trace[-1])
        self.assertLessEqual(len(trace[0]), MAX_TRACE_CHARS // 2)

    def test_sanitizes_secrets_personal_data_and_local_paths(self):
        text = (
            "Email pessoa@example.com token=secret-value "
            "Bearer abcdefghijklmnopqrstuvwxyz0123456789 /Users/alice/project"
        )

        result = sanitize_training_text(text)

        self.assertNotIn("pessoa@example.com", result)
        self.assertNotIn("secret-value", result)
        self.assertNotIn("/Users/alice", result)
        self.assertIn("[REDACTED]", result)
        self.assertNotIn("x" * 40, sanitize_training_text("Bearer " + "x" * 40))
        self.assertIn("[USER_HOME]", sanitize_training_text(f"path {Path.home()}/project"))

    def test_codex_import_uses_only_user_text_and_final_answers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session = root / "rollout-example.jsonl"
            session.write_text(
                "\n".join(
                    json.dumps(row)
                    for row in (
                        {
                            "type": "response_item",
                            "payload": {
                                "type": "message",
                                "role": "user",
                                "content": [{"type": "input_text", "text": "Explique isto"}],
                            },
                        },
                        {
                            "type": "response_item",
                            "payload": {
                                "type": "message",
                                "role": "assistant",
                                "phase": "commentary",
                                "content": [{"type": "output_text", "text": "Progresso"}],
                            },
                        },
                        {
                            "type": "response_item",
                            "payload": {
                                "type": "message",
                                "role": "assistant",
                                "phase": "final_answer",
                                "content": [{"type": "output_text", "text": "Resposta final"}],
                            },
                        },
                    )
                ),
                encoding="utf-8",
            )

            samples = list(iter_codex_samples(root))

        self.assertEqual(len(samples), 1)
        self.assertEqual(
            samples[0]["messages"],
            [
                {"role": "user", "content": "Explique isto"},
                {"role": "assistant", "content": "Resposta final"},
            ],
        )

    def test_vscode_import_requires_complete_user_assistant_pair(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "sessions.db"
            with sqlite3.connect(database) as connection:
                connection.execute(
                    """
                    CREATE TABLE turns (
                        session_id TEXT,
                        turn_index INTEGER,
                        user_message TEXT,
                        assistant_response TEXT
                    )
                    """
                )
                connection.executemany(
                    "INSERT INTO turns VALUES (?, ?, ?, ?)",
                    [
                        ("session", 0, "Pergunta", "Resposta"),
                        ("session", 1, "Sem resposta", None),
                    ],
                )

            samples = list(iter_vscode_samples(database))

        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0]["messages"][1]["content"], "Resposta")

    def test_collection_deduplicates_matching_records_across_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            codex = root / "codex"
            codex.mkdir()
            (codex / "rollout.jsonl").write_text(
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "Pergunta única"}],
                        },
                    }
                )
                + "\n"
                + json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "phase": "final_answer",
                            "content": [{"type": "output_text", "text": "Resposta única"}],
                        },
                    }
                ),
                encoding="utf-8",
            )
            database = root / "sessions.db"
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "CREATE TABLE turns "
                    "(session_id TEXT, turn_index INTEGER, user_message TEXT, assistant_response TEXT)"
                )
                connection.execute(
                    "INSERT INTO turns VALUES (?, ?, ?, ?)",
                    ("session", 0, "Pergunta única", "Resposta única"),
                )

            samples = collect_samples(codex, database, root / "history.sqlite3")

        self.assertEqual(len(samples), 1)

    def test_imported_history_samples_use_complete_sanitized_pairs(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "training.sqlite3"
            with sqlite3.connect(database) as connection:
                connection.execute(
                    """
                    CREATE TABLE conversation_turns (
                        source TEXT, session_id TEXT, turn_id TEXT,
                        user_message TEXT, assistant_response TEXT
                    )
                    """
                )
                connection.executemany(
                    "INSERT INTO conversation_turns VALUES (?, ?, ?, ?, ?)",
                    [
                        ("codex", "session", "1", "Pergunta", "Resposta token=secret-value"),
                        ("codex", "session", "2", "Sem resposta", None),
                    ],
                )

            samples = list(iter_imported_history_samples(database))

        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0]["identity"], "imported-codex:session")
        self.assertNotIn("secret-value", samples[0]["messages"][1]["content"])

    def test_project_event_samples_include_commands_results_and_errors_only_for_project(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            codex = home / ".codex"
            rollout = codex / "sessions" / "rollout.jsonl"
            rollout.parent.mkdir(parents=True)
            rows = [
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "id": "turn",
                        "content": [{"type": "input_text", "text": "Corrija o build"}],
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "call_id": "1",
                        "name": "exec_command",
                        "arguments": {"cmd": "pytest"},
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": "1",
                        "output": {"exit_code": 1, "stderr": "falha de teste"},
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "call_id": "2",
                        "name": "exec_command",
                        "arguments": {"cmd": "pytest -q"},
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": "2",
                        "output": {"exit_code": 0, "stdout": "2 passed"},
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "phase": "final_answer",
                        "content": [{"type": "output_text", "text": "Build corrigido."}],
                    },
                },
            ]
            rollout.write_text(
                "\n".join(json.dumps(row) for row in rows),
                encoding="utf-8",
            )
            database = codex / "state_5.sqlite"
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "CREATE TABLE threads (id TEXT, rollout_path TEXT, source TEXT, "
                    "cwd TEXT, created_at TEXT)"
                )
                connection.executemany(
                    "INSERT INTO threads VALUES (?, ?, ?, ?, ?)",
                    [
                        (
                            "session",
                            str(rollout),
                            "cli",
                            str(home / "Desktop" / "www-italosantos-com"),
                            "1",
                        ),
                        (
                            "other",
                            str(rollout),
                            "cli",
                            str(home / "Desktop" / "other-project"),
                            "2",
                        ),
                    ],
                )

            samples = list(
                iter_project_event_samples(
                    "www-italosantos-com",
                    home=home,
                    codex_db=database,
                )
            )

        self.assertEqual(len(samples), 3)
        self.assertIn("Ferramenta: exec_command", samples[0]["messages"][1]["content"])
        self.assertIn("status: error", samples[1]["messages"][0]["content"])
        self.assertIn("pytest -q", samples[1]["messages"][1]["content"])
        self.assertIn("status: completed", samples[2]["messages"][0]["content"])
        self.assertEqual(samples[2]["messages"][1]["content"], "Build corrigido.")

    def test_dataset_split_is_private_and_uses_messages_format(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "dataset"
            samples = [
                {
                    "identity": f"codex:session-{index}",
                    "messages": [
                        {"role": "user", "content": "Pergunta"},
                        {"role": "assistant", "content": "Resposta"},
                    ],
                }
                for index in range(100)
            ]

            counts = write_dataset(samples, destination)

            self.assertEqual(sum(counts.values()), len(samples))
            self.assertTrue(all(count > 0 for count in counts.values()))
            self.assertEqual(
                os.stat(destination).st_mode & 0o777,
                0o700,
            )
            for split in ("train", "valid", "test"):
                first_record = json.loads(
                    (destination / f"{split}.jsonl").read_text().splitlines()[0]
                )
                self.assertEqual(
                    [message["role"] for message in first_record["messages"]],
                    ["user", "assistant"],
                )
                self.assertEqual(
                    os.stat(destination / f"{split}.jsonl").st_mode & 0o777,
                    0o600,
                )

    def test_rejects_empty_data_and_existing_destination(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "dataset"
            with self.assertRaisesRegex(ValueError, "Histórico insuficiente"):
                write_dataset([], destination)
            destination.mkdir()
            with self.assertRaises(FileExistsError):
                write_dataset([], destination)

    def test_preserves_answer_tokens_within_context_and_skips_oversized_answers(self):
        with tempfile.TemporaryDirectory() as temporary:
            dataset = Path(temporary) / "dataset"
            dataset.mkdir()
            long_record = {
                "messages": [
                    {"role": "user", "content": "p" * 500},
                    {"role": "assistant", "content": "a" * 100},
                ]
            }
            short_record = {
                "messages": [
                    {"role": "user", "content": "pergunta"},
                    {"role": "assistant", "content": "r" * 200},
                ]
            }
            for split in ("train", "valid", "test"):
                (dataset / f"{split}.jsonl").write_text(
                    json.dumps(long_record) + "\n" + json.dumps(short_record) + "\n",
                    encoding="utf-8",
                )

            output, counts = prepare_mlx_dataset(
                dataset,
                CharacterTokenizer(),
                max_seq_length=256,
            )

            self.assertEqual(counts, {"train": 1, "valid": 1, "test": 1})
            for split in ("train", "valid", "test"):
                messages = json.loads((output / f"{split}.jsonl").read_text())["messages"]
                self.assertLessEqual(
                    len(CharacterTokenizer.apply_chat_template(messages)),
                    256,
                )
                self.assertEqual(messages[1]["content"], "a" * 100)

    def test_huggingface_training_uses_completion_loss_and_holds_out_test_split(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = root / "dataset"
            dataset.mkdir()
            for split in ("train", "valid", "test"):
                (dataset / f"{split}.jsonl").write_text(
                    json.dumps(
                        {
                            "messages": [
                                {"role": "user", "content": f"Pedido {split}"},
                                {"role": "assistant", "content": f"Resposta {split}"},
                            ]
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )

            captured: dict[str, object] = {}

            class FakeDataset:
                @staticmethod
                def from_list(records):
                    return records

            class FakeTrainer:
                def __init__(self, **kwargs):
                    captured.update(kwargs)
                    captured["trainer_args"] = kwargs["args"]
                    self.model = self
                    self.processing_class = self

                def train(self):
                    captured["trained"] = True

                def evaluate(self, **kwargs):
                    captured["evaluation"] = kwargs

                def save_pretrained(self, destination):
                    path = Path(destination)
                    path.mkdir(parents=True, exist_ok=True)
                    (path / "saved.txt").write_text("saved", encoding="utf-8")

            class FakeTrainingArgs:
                def __init__(self, **kwargs):
                    self.__dict__.update(kwargs)

            fake_torch = type(
                "FakeTorch",
                (),
                {
                    "float16": "float16",
                    "float32": "float32",
                    "bfloat16": "bfloat16",
                    "cuda": type(
                        "Cuda",
                        (),
                        {
                            "is_available": staticmethod(lambda: False),
                            "is_bf16_supported": staticmethod(lambda: False),
                        },
                    )(),
                    "backends": type(
                        "Backends",
                        (),
                        {
                            "mps": type(
                                "Mps",
                                (),
                                {"is_available": staticmethod(lambda: False)},
                            )()
                        },
                    )(),
                },
            )()
            fake_modules = {
                "torch": fake_torch,
                "datasets": type("Datasets", (), {"Dataset": FakeDataset})(),
                "peft": type(
                    "Peft",
                    (),
                    {"LoraConfig": staticmethod(lambda **kwargs: kwargs)},
                )(),
                "trl": type(
                    "Trl",
                    (),
                    {
                        "SFTConfig": FakeTrainingArgs,
                        "SFTTrainer": FakeTrainer,
                    },
                )(),
            }
            adapter = root / "adapters" / "test-adapter"
            with patch.dict("sys.modules", fake_modules):
                result = train_huggingface(dataset, adapter)

            self.assertEqual(result, 0)
            self.assertEqual(
                captured["train_dataset"],
                [
                    {
                        "prompt": [{"role": "user", "content": "Pedido train"}],
                        "completion": [
                            {"role": "assistant", "content": "Resposta train"}
                        ],
                    }
                ],
            )
            self.assertTrue(captured["trainer_args"].completion_only_loss)
            self.assertEqual(
                captured["eval_dataset"],
                [
                    {
                        "prompt": [{"role": "user", "content": "Pedido valid"}],
                        "completion": [
                            {"role": "assistant", "content": "Resposta valid"}
                        ],
                    }
                ],
            )
            self.assertEqual(
                captured["evaluation"]["metric_key_prefix"],
                "test",
            )
            self.assertEqual(
                captured["evaluation"]["eval_dataset"],
                [
                    {
                        "prompt": [{"role": "user", "content": "Pedido test"}],
                        "completion": [
                            {"role": "assistant", "content": "Resposta test"}
                        ],
                    }
                ],
            )
            self.assertTrue(captured["trained"])
            self.assertTrue((adapter / "saved.txt").is_file())
            self.assertEqual(os.stat(adapter).st_mode & 0o777, 0o700)


if __name__ == "__main__":
    unittest.main()
