import hashlib
import json
import os
import re
import sqlite3
import tempfile
import unicodedata
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

MAX_CONTEXT_CHARS = 60000
MAX_LEARNINGS = 100
MAX_LEARNING_CHARS = 400
MAX_LEARNING_TEXT_CHARS = 32_000
_LEARNING_SECRET_PATTERNS = (
    re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(r"\b[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    re.compile(r"(?i)\b(api[_-]?key|token|password|secret)\s*[:=]\s*\S+"),
    re.compile(r"\b[A-Za-z0-9_-]{32,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\b"),
    re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?-----END "
        r"(?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
        re.DOTALL,
    ),
)


def sanitize_learning_text(value: str, limit: int = MAX_LEARNING_CHARS) -> str:
    result = " ".join(value.split())[:limit]
    for pattern in _LEARNING_SECRET_PATTERNS:
        result = pattern.sub("[redacted]", result)
    return result.strip()[:limit]


def _message_size(message: dict[str, Any]) -> int:
    return len(json.dumps(message, ensure_ascii=False, default=str))


def trim_context(
    messages: list[dict[str, Any]],
    max_chars: int = MAX_CONTEXT_CHARS,
) -> list[dict[str, Any]]:
    if not messages:
        return []
    system = messages[:1] if messages[0].get("role") == "system" else []
    conversation = messages[len(system) :]
    turns: list[list[dict[str, Any]]] = []
    for message in conversation:
        if message.get("role") == "user" or not turns:
            turns.append([])
        turns[-1].append(message)

    system_size = sum(_message_size(message) for message in system)
    budget = max(0, max_chars - system_size)
    retained: list[list[dict[str, Any]]] = []
    used = 0
    for turn in reversed(turns):
        turn_size = sum(_message_size(message) for message in turn)
        if retained and used + turn_size > budget:
            break
        retained.append(turn)
        used += turn_size
    retained.reverse()
    return system + [message for turn in retained for message in turn]


class RuntimeStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.backup_path = self.path.with_name(f"{self.path.name}.bak")
        self._recover_database()
        with self._connection(backup=True) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    status TEXT NOT NULL,
                    agent TEXT,
                    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    finished_at TEXT
                );
                CREATE TABLE IF NOT EXISTS run_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    event TEXT NOT NULL,
                    component TEXT NOT NULL,
                    status TEXT NOT NULL,
                    agent TEXT,
                    tool TEXT
                );
                CREATE INDEX IF NOT EXISTS run_events_by_run
                    ON run_events(run_id, id);
                CREATE INDEX IF NOT EXISTS runs_by_started_at
                    ON runs(started_at DESC);
                CREATE TABLE IF NOT EXISTS learnings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    topic TEXT NOT NULL,
                    lesson TEXT NOT NULL,
                    fingerprint TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS agent_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO agent_state (key, value)
                VALUES ('agent_id', ?)
                """,
                (uuid.uuid4().hex,),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO agent_state (key, value)
                VALUES ('name', 'Kerberos')
                """
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO agent_state (key, value)
                VALUES ('created_at', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                """
            )
        if os.name != "nt":
            self.path.chmod(0o600)

    @staticmethod
    def _database_is_valid(path: Path) -> bool:
        if not path.is_file():
            return False
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
            return connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
        except (OSError, sqlite3.DatabaseError):
            return False
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    @contextmanager
    def _backup_lock(destination: Path) -> Iterator[None]:
        lock_path = destination.with_name(f"{destination.name}.lock")
        flags = os.O_CREAT | os.O_RDWR
        if os.name != "nt":
            flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(lock_path, flags, 0o600)
        try:
            if os.name != "nt":
                os.fchmod(descriptor, 0o600)
            if os.name == "nt":
                import msvcrt

                if os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, b"\0")
                os.lseek(descriptor, 0, os.SEEK_SET)
                lock_file = getattr(msvcrt, "locking")
                lock_file(descriptor, getattr(msvcrt, "LK_LOCK"), 1)
                try:
                    yield
                finally:
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    lock_file(descriptor, getattr(msvcrt, "LK_UNLCK"), 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    @staticmethod
    def _atomic_backup(source: Path | sqlite3.Connection, destination: Path) -> None:
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
        )
        os.close(file_descriptor)
        temporary_path = Path(temporary_name)
        source_connection: sqlite3.Connection | None = None
        target_connection: sqlite3.Connection | None = None
        owns_source = False
        try:
            if isinstance(source, Path):
                source_connection = sqlite3.connect(str(source), timeout=10)
                owns_source = True
            else:
                source_connection = source
            target_connection = sqlite3.connect(temporary_path, timeout=10)
            source_connection.backup(target_connection)
            target_connection.close()
            target_connection = None
            if owns_source:
                source_connection.close()
                source_connection = None
            if not RuntimeStore._database_is_valid(temporary_path):
                raise sqlite3.DatabaseError("O snapshot SQLite criado não passou no quick_check.")
            if os.name != "nt":
                temporary_path.chmod(0o600)
            os.replace(temporary_path, destination)
        finally:
            if target_connection is not None:
                target_connection.close()
            if source_connection is not None and owns_source:
                source_connection.close()
            temporary_path.unlink(missing_ok=True)

    def _recover_database(self) -> None:
        with self._backup_lock(self.path):
            main_exists = self.path.exists()
            if main_exists and self._database_is_valid(self.path):
                return
            if not main_exists and not self.backup_path.exists():
                return
            if not self._database_is_valid(self.backup_path):
                raise sqlite3.DatabaseError(
                    f"O banco {self.path} está ausente ou corrompido e não há snapshot válido "
                    f"em {self.backup_path}; os arquivos foram preservados."
                )
            sidecars = [
                Path(f"{self.path}{suffix}")
                for suffix in ("-journal", "-wal", "-shm")
                if Path(f"{self.path}{suffix}").exists()
            ]
            if sidecars:
                names = ", ".join(str(path) for path in sidecars)
                raise sqlite3.DatabaseError(
                    f"Não é seguro restaurar {self.path} enquanto houver arquivos SQLite "
                    f"ativos ou pendentes ({names}). Feche todas as instâncias do Kerberos "
                    "e tente novamente."
                )
            self._atomic_backup(self.backup_path, self.path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def _connection(self, backup: bool = False) -> Iterator[sqlite3.Connection]:
        with self._backup_lock(self.path):
            connection = self._connect()
            try:
                with connection:
                    yield connection
                if backup:
                    self._atomic_backup(connection, self.backup_path)
            finally:
                connection.close()

    def _record(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        event: str,
        component: str,
        status: str,
        agent: str | None = None,
        tool: str | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO run_events (run_id, event, component, status, agent, tool)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                event[:80],
                component[:80],
                status[:40],
                agent[:80] if agent else None,
                tool[:120] if tool else None,
            ),
        )

    def start_run(self, source: str) -> str:
        run_id = uuid.uuid4().hex
        with self._connection(backup=True) as connection:
            connection.execute(
                "INSERT INTO runs (id, source, status) VALUES (?, ?, 'running')",
                (run_id, source[:40]),
            )
            self._record(connection, run_id, "run.started", "runtime", "running")
        return run_id

    def record_event(
        self,
        run_id: str,
        event: str,
        component: str,
        status: str,
        agent: str | None = None,
        tool: str | None = None,
    ) -> None:
        with self._connection(backup=True) as connection:
            self._record(connection, run_id, event, component, status, agent, tool)
            if agent:
                connection.execute(
                    "UPDATE runs SET agent = ? WHERE id = ?",
                    (agent[:80], run_id),
                )

    def finish_run(self, run_id: str, status: str) -> None:
        if status not in {"completed", "failed"}:
            raise ValueError("O status final deve ser completed ou failed.")
        with self._connection(backup=True) as connection:
            updated = connection.execute(
                """
                UPDATE runs
                SET status = ?, finished_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status = 'running'
                """,
                (status, run_id),
            ).rowcount
            if updated:
                self._record(connection, run_id, "run.finished", "runtime", status)
                if status == "completed":
                    connection.execute(
                        """
                        INSERT INTO agent_state (key, value)
                        VALUES ('last_checkpoint', ?)
                        ON CONFLICT(key) DO UPDATE SET
                            value = excluded.value,
                            updated_at = CURRENT_TIMESTAMP
                        """,
                        (run_id,),
                    )

    def recent_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        if not 1 <= limit <= 100:
            raise ValueError("O limite de execuções deve estar entre 1 e 100.")
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id, source, status, agent, started_at, finished_at
                FROM runs ORDER BY started_at DESC, rowid DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def run_events(self, run_id: str, limit: int = 100) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500:
            raise ValueError("O limite de eventos deve estar entre 1 e 500.")
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id, created_at, event, component, status, agent, tool
                FROM run_events WHERE run_id = ? ORDER BY id LIMIT ?
                """,
                (run_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _sanitize_learning(value: str, limit: int) -> str:
        return sanitize_learning_text(value, limit)

    def save_learning(self, topic: str, lesson: str) -> bool:
        safe_topic = self._sanitize_learning(topic, 80)
        safe_lesson = self._sanitize_learning(lesson, MAX_LEARNING_CHARS)
        if not safe_topic or not safe_lesson or safe_lesson == "[redacted]":
            return False
        normalized = unicodedata.normalize("NFKC", safe_lesson).casefold()
        fingerprint = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        with self._connection(backup=True) as connection:
            connection.execute(
                """
                INSERT INTO learnings (topic, lesson, fingerprint)
                VALUES (?, ?, ?)
                ON CONFLICT(fingerprint) DO UPDATE SET
                    topic = excluded.topic,
                    created_at = CURRENT_TIMESTAMP
                """,
                (safe_topic, safe_lesson, fingerprint),
            )
            connection.execute(
                """
                DELETE FROM learnings
                WHERE id NOT IN (
                    SELECT id FROM learnings
                    ORDER BY created_at DESC, id DESC
                    LIMIT ?
                )
                """,
                (MAX_LEARNINGS,),
            )
            rows = connection.execute(
                "SELECT id, topic, lesson FROM learnings ORDER BY created_at DESC, id DESC"
            ).fetchall()
            total = 0
            keep_ids = []
            for row in rows:
                size = len(row["topic"]) + len(row["lesson"])
                if total + size > MAX_LEARNING_TEXT_CHARS:
                    continue
                total += size
                keep_ids.append(row["id"])
            if keep_ids:
                placeholders = ",".join("?" for _ in keep_ids)
                connection.execute(
                    f"DELETE FROM learnings WHERE id NOT IN ({placeholders})",
                    keep_ids,
                )
            else:
                connection.execute("DELETE FROM learnings")
        return True

    def relevant_learnings(self, query: str, limit: int = 3) -> list[dict[str, str]]:
        if not 1 <= limit <= 10:
            raise ValueError("O limite de aprendizados deve estar entre 1 e 10.")
        words = {word.casefold() for word in re.findall(r"[a-zA-Z0-9_-]{3,}", query)}
        if not words:
            return []
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT topic, lesson FROM learnings ORDER BY created_at DESC, id DESC"
            ).fetchall()
        ranked: list[tuple[int, dict[str, str]]] = []
        for row in rows:
            item = {"topic": row["topic"], "lesson": row["lesson"]}
            tokens = set(
                re.findall(r"[a-zA-Z0-9_-]{3,}", f"{item['topic']} {item['lesson']}".casefold())
            )
            score = len(words & tokens)
            if score:
                ranked.append((score, item))
        ranked.sort(key=lambda item: (-item[0], item[1]["topic"].casefold()))
        return [item for _, item in ranked[:limit]]

    def list_learnings(self, limit: int = MAX_LEARNINGS) -> list[dict[str, str]]:
        if not 1 <= limit <= MAX_LEARNINGS:
            raise ValueError(f"O limite deve estar entre 1 e {MAX_LEARNINGS}.")
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT topic, lesson, created_at FROM learnings "
                "ORDER BY created_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def learning_usage(self) -> tuple[int, int]:
        with self._connection() as connection:
            count, chars = connection.execute(
                "SELECT COUNT(*), COALESCE(SUM(length(topic) + length(lesson)), 0) FROM learnings"
            ).fetchone()
        return int(count), int(chars)

    def delete_learnings(self) -> None:
        with self._connection(backup=True) as connection:
            connection.execute("DELETE FROM learnings")

    def identity(self) -> dict[str, str | None]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT key, value FROM agent_state "
                "WHERE key IN ('agent_id', 'name', 'created_at', 'last_checkpoint')"
            ).fetchall()
        state = {row["key"]: row["value"] for row in rows}
        return {
            "agent_id": state["agent_id"],
            "name": state["name"],
            "created_at": state["created_at"],
            "last_checkpoint": state.get("last_checkpoint"),
        }

    def set_checkpoint(self, label: str) -> None:
        safe_label = sanitize_learning_text(label, 120)
        if not safe_label or safe_label == "[redacted]":
            raise ValueError("O checkpoint precisa ter um rótulo válido e sem dados sensíveis.")
        with self._connection(backup=True) as connection:
            connection.execute(
                """
                INSERT INTO agent_state (key, value)
                VALUES ('last_checkpoint', ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (safe_label,),
            )
