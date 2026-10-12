import json
import os
import re
import sqlite3
import zlib
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator
from urllib.parse import unquote, urlparse

from assistente.runtime import sanitize_learning_text

MAX_MESSAGE_CHARS = 20_000
MAX_SEARCH_RESULTS = 3
MAX_SEARCH_CHARS = 5_000
MAX_EVENT_SEARCH_CHARS = 3_000
_SEARCH_TOKEN = re.compile(r"[\w-]{2,}", re.UNICODE)
_SECRET_FIELD = re.compile(
    r"(?:api[_-]?key|access[_-]?token|auth(?:orization)?|cookie|password|passwd|private[_-]?key|secret|token)",
    re.I,
)
_EXIT_CODE = re.compile(r"(?:^|\n)\s*(?:exit code|exit status)\s*:\s*(-?\d+)(?:\s|$)", re.I)


@dataclass(frozen=True)
class ConversationTurn:
    source: str
    session_id: str
    turn_id: str
    workspace: str
    user_message: str
    assistant_response: str
    created_at: str = ""


@dataclass(frozen=True)
class ConversationEvent:
    source: str
    session_id: str
    event_id: str
    turn_id: str
    workspace: str
    event_order: int
    event_type: str
    tool_name: str
    status: str
    payload: Any


def _safe_text(value: Any, limit: int = MAX_MESSAGE_CHARS) -> str:
    if not isinstance(value, str):
        return ""
    return sanitize_learning_text(value, limit).strip()


def _safe_event_value(value: Any, field_name: str = "") -> Any:
    if _SECRET_FIELD.search(field_name):
        return "[redacted]"
    if isinstance(value, dict):
        return {str(key): _safe_event_value(item, str(key)) for key, item in value.items()}
    if isinstance(value, list):
        return [_safe_event_value(item) for item in value]
    if isinstance(value, str):
        return sanitize_learning_text(value, max(400, len(value))).strip()
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return "[unsupported value redacted]"


def _event_json(value: Any) -> str:
    return json.dumps(_safe_event_value(value), ensure_ascii=False, separators=(",", ":"))


def _event_search_text(value: str) -> str:
    if len(value) <= MAX_EVENT_SEARCH_CHARS:
        return value
    head_chars = MAX_EVENT_SEARCH_CHARS // 2
    return (
        value[:head_chars]
        + "\n[... conteúdo completo mantido localmente ...]\n"
        + value[-(MAX_EVENT_SEARCH_CHARS - head_chars) :]
    )


def _tool_output_status(tool_name: str, output: Any) -> str:
    if isinstance(output, dict):
        if output.get("is_error") is True or output.get("ok") is False or bool(output.get("error")):
            return "error"
        for key in ("exit_code", "exitCode", "returncode"):
            status = output.get(key)
            if isinstance(status, int) and not isinstance(status, bool):
                return "error" if status else "completed"
        status = output.get("status")
        if isinstance(status, str) and status.casefold() in {
            "error",
            "failed",
            "failure",
        }:
            return "error"
    if isinstance(output, str) and tool_name in {"exec_command", "write_stdin"}:
        exit_code = _EXIT_CODE.search(output)
        if exit_code:
            return "error" if int(exit_code.group(1)) else "completed"
    return "unknown"


def _codex_text(content: Any, *, output: bool = False) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    allowed = {"output_text", "text"} if output else {"input_text", "text"}
    return "\n".join(
        item["text"]
        for item in content
        if isinstance(item, dict)
        and item.get("type") in allowed
        and isinstance(item.get("text"), str)
    )


def parse_codex_rollout(
    path: Path,
    *,
    source: str,
    session_id: str,
    workspace: str,
) -> Iterator[ConversationTurn]:
    pending_user = ""
    pending_turn_id = ""
    assistant_parts: list[str] = []
    ordinal = 0

    def completed_turn() -> ConversationTurn | None:
        nonlocal pending_user, pending_turn_id, assistant_parts, ordinal
        answer = _safe_text("\n".join(assistant_parts))
        question = _safe_text(pending_user)
        turn_id = pending_turn_id
        pending_user = ""
        pending_turn_id = ""
        assistant_parts = []
        if not question or not answer:
            return None
        ordinal += 1
        return ConversationTurn(
            source=source,
            session_id=session_id,
            turn_id=turn_id or str(ordinal),
            workspace=workspace,
            user_message=question,
            assistant_response=answer,
        )

    with path.open("r", encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            is_user_message = payload.get("type") == "user" or (
                payload.get("type") == "message" and payload.get("role") == "user"
            )
            if record.get("type") == "response_item" and is_user_message:
                previous = completed_turn()
                if previous:
                    yield previous
                pending_user = _codex_text(payload.get("content"))
                pending_turn_id = str(payload.get("id", ""))
            elif (
                record.get("type") == "response_item"
                and payload.get("type") == "message"
                and payload.get("role") == "assistant"
                and payload.get("phase") == "final_answer"
            ):
                text = _codex_text(payload.get("content"), output=True)
                if text:
                    assistant_parts.append(text)
            elif (
                pending_user
                and record.get("type") == "response_item"
                and (
                    (payload.get("type") == "assistant" and payload.get("phase") == "final")
                    or (
                        payload.get("type") == "message"
                        and payload.get("role") == "assistant"
                        and payload.get("phase") == "final_answer"
                    )
                )
            ):
                text = _codex_text(payload.get("content"), output=True)
                if text:
                    assistant_parts.append(text)
    previous = completed_turn()
    if previous:
        yield previous


def _workspace_path(metadata_path: Path) -> str:
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        workspace = metadata.get("workspace")
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(workspace, str):
        return ""
    parsed = urlparse(workspace)
    if parsed.scheme == "file":
        return str(Path(unquote(parsed.path)).resolve())
    if parsed.scheme:
        return ""
    return str(Path(workspace).expanduser().resolve())


def parse_vscode_session(path: Path, *, workspace: str) -> Iterator[ConversationTurn]:
    ordinal = 0
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("kind") != 2 or not isinstance(record.get("v"), list):
                continue
            for item in record["v"]:
                if not isinstance(item, dict):
                    continue
                message = item.get("message")
                if not isinstance(message, dict):
                    continue
                question = _safe_text(message.get("text"))
                if not question:
                    continue
                response_parts = []
                for part in item.get("response", []):
                    if (
                        isinstance(part, dict)
                        and part.get("kind") != "thinking"
                        and isinstance(part.get("value"), str)
                    ):
                        response_parts.append(part["value"])
                answer = _safe_text("\n".join(response_parts))
                if not answer:
                    continue
                ordinal += 1
                turn_id = str(item.get("requestId") or item.get("responseId") or ordinal)
                timestamp = item.get("timestamp")
                created_at = str(timestamp) if isinstance(timestamp, (int, str)) else ""
                yield ConversationTurn(
                    source="vscode",
                    session_id=path.stem,
                    turn_id=turn_id,
                    workspace=workspace,
                    user_message=question,
                    assistant_response=answer,
                    created_at=created_at,
                )


def discover_history_turns(
    *,
    home: Path | None = None,
    vscode_root: Path | None = None,
    codex_db: Path | None = None,
) -> Iterator[ConversationTurn]:
    user_home = Path.home() if home is None else home
    vscode_base = (
        user_home / "Library/Application Support/Code/User/workspaceStorage"
        if vscode_root is None
        else vscode_root
    )
    for session_file in sorted(vscode_base.glob("*/chatSessions/*.jsonl")):
        workspace = _workspace_path(session_file.parents[1] / "workspace.json")
        yield from parse_vscode_session(session_file, workspace=workspace)

    database_path = user_home / ".codex/state_5.sqlite" if codex_db is None else codex_db
    if not database_path.is_file() or database_path.is_symlink():
        return
    connection = sqlite3.connect(f"{database_path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            """
            SELECT id, rollout_path, source, cwd
            FROM threads
            WHERE source IN ('cli', 'vscode')
            ORDER BY created_at, id
            """
        ).fetchall()
    finally:
        connection.close()
    codex_root = (user_home / ".codex").resolve()
    for session_id, rollout, source, cwd in rows:
        if not isinstance(rollout, str):
            continue
        rollout_path = Path(rollout)
        if rollout_path.is_symlink():
            continue
        try:
            resolved_rollout = rollout_path.resolve(strict=True)
            resolved_rollout.relative_to(codex_root)
        except (OSError, ValueError):
            continue
        if not resolved_rollout.is_file() or resolved_rollout.is_symlink():
            continue
        workspace = ""
        if isinstance(cwd, str) and cwd:
            try:
                workspace = str(Path(cwd).expanduser().resolve())
            except OSError:
                pass
        yield from parse_codex_rollout(
            resolved_rollout,
            source=f"codex-{source}",
            session_id=str(session_id),
            workspace=workspace,
        )


def _project_workspace(cwd: Any, *, home: Path, project_name: str) -> str | None:
    if not isinstance(cwd, str) or not cwd:
        return None
    candidate = Path(cwd).expanduser()
    if candidate.name != project_name:
        return None
    try:
        normalized = candidate.resolve(strict=False)
        desktop_root = (home / "Desktop").resolve()
        codex_worktrees = (home / ".codex" / "worktrees").resolve()
        normalized.relative_to(desktop_root)
        allowed = True
    except ValueError:
        try:
            normalized.relative_to(codex_worktrees)
            allowed = True
        except ValueError:
            allowed = False
    except OSError:
        return None
    return str(normalized) if allowed and normalized.name == project_name else None


def discover_project_events(
    project_name: str,
    *,
    home: Path | None = None,
    codex_db: Path | None = None,
) -> Iterator[ConversationEvent]:
    if not re.fullmatch(r"[a-zA-Z0-9._-]{1,100}", project_name):
        raise ValueError("O nome do projeto contém caracteres inválidos.")
    user_home = Path.home() if home is None else home
    database_path = user_home / ".codex/state_5.sqlite" if codex_db is None else codex_db
    if not database_path.is_file() or database_path.is_symlink():
        return
    connection = sqlite3.connect(f"{database_path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            """
            SELECT id, rollout_path, source, cwd
            FROM threads
            WHERE substr(cwd, -length(?)) = ?
            ORDER BY created_at, id
            """,
            (f"/{project_name}", f"/{project_name}"),
        ).fetchall()
    finally:
        connection.close()
    codex_root = (user_home / ".codex").resolve()
    project_rollouts: list[tuple[str, str, str, str]] = []
    for session_id, rollout, source, cwd in rows:
        workspace = _project_workspace(cwd, home=user_home, project_name=project_name)
        if workspace is None or not isinstance(rollout, str):
            continue
        project_rollouts.append((str(session_id), rollout, str(source or ""), workspace))

    for session_id, rollout, source, workspace in project_rollouts:
        if not rollout:
            continue
        rollout_path = Path(rollout)
        if rollout_path.is_symlink():
            continue
        try:
            resolved_rollout = rollout_path.resolve(strict=True)
            resolved_rollout.relative_to(codex_root)
        except (OSError, ValueError):
            continue
        if not resolved_rollout.is_file() or resolved_rollout.is_symlink():
            continue
        source_name = (
            f"codex-{source}" if source in {"cli", "vscode"} else "codex-agent"
        )
        call_names: dict[str, str] = {}
        turn_id = ""
        with resolved_rollout.open("r", encoding="utf-8", errors="replace") as stream:
            for event_order, line in enumerate(stream):
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = record.get("payload")
                if record.get("type") != "response_item" or not isinstance(payload, dict):
                    continue
                kind = payload.get("type")
                role = payload.get("role")
                if kind == "user" or (kind == "message" and role == "user"):
                    turn_id = str(payload.get("id") or event_order)
                    content = payload.get("content")
                    yield ConversationEvent(
                        source_name,
                        str(session_id),
                        str(payload.get("id") or f"user-{event_order}"),
                        turn_id,
                        workspace,
                        event_order,
                        "user_message",
                        "",
                        "recorded",
                        content,
                    )
                elif (
                    kind == "message"
                    and role == "assistant"
                    and payload.get("phase") == "final_answer"
                ):
                    yield ConversationEvent(
                        source_name,
                        str(session_id),
                        str(payload.get("id") or f"assistant-final-{event_order}"),
                        turn_id,
                        workspace,
                        event_order,
                        "assistant_final",
                        "",
                        "recorded",
                        payload.get("content"),
                    )
                elif kind == "function_call":
                    call_id = str(payload.get("call_id") or f"call-{event_order}")
                    tool_name = str(payload.get("name") or "")
                    call_names[call_id] = tool_name
                    yield ConversationEvent(
                        source_name,
                        str(session_id),
                        f"{call_id}:call",
                        turn_id,
                        workspace,
                        event_order,
                        "tool_call",
                        tool_name,
                        "pending",
                        payload.get("arguments"),
                    )
                elif kind == "function_call_output":
                    call_id = str(payload.get("call_id") or f"result-{event_order}")
                    tool_name = call_names.get(call_id, "")
                    result = payload.get("output")
                    yield ConversationEvent(
                        source_name,
                        str(session_id),
                        f"{call_id}:result",
                        turn_id,
                        workspace,
                        event_order,
                        "tool_result",
                        tool_name,
                        _tool_output_status(tool_name, result),
                        result,
                    )


class ConversationHistoryStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if self.path.parent.is_symlink():
            raise ValueError("A pasta do banco de histórico não pode ser um link simbólico.")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.is_symlink():
            raise ValueError("O banco de histórico não pode ser um link simbólico.")
        self.path.parent.chmod(0o700)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_turns (
                    source TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    workspace TEXT NOT NULL,
                    user_message TEXT NOT NULL,
                    assistant_response TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (source, session_id, turn_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_events (
                    source TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    workspace TEXT NOT NULL,
                    event_order INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_z BLOB NOT NULL,
                    PRIMARY KEY (source, session_id, event_id)
                )
                """
            )
            connection.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS conversation_turns_fts USING fts5(
                    source UNINDEXED,
                    session_id UNINDEXED,
                    turn_id UNINDEXED,
                    workspace UNINDEXED,
                    user_message,
                    assistant_response,
                    tokenize = 'unicode61'
                )
                """
            )
            connection.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS conversation_events_fts USING fts5(
                    source UNINDEXED,
                    session_id UNINDEXED,
                    event_id UNINDEXED,
                    turn_id UNINDEXED,
                    workspace UNINDEXED,
                    event_type UNINDEXED,
                    tool_name UNINDEXED,
                    status UNINDEXED,
                    search_text,
                    tokenize = 'unicode61'
                )
                """
            )
            if os.name != "nt":
                self.path.chmod(0o600)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def import_turns(self, turns: Iterable[ConversationTurn]) -> int:
        imported = 0
        with self._connect() as connection:
            for turn in turns:
                question = _safe_text(turn.user_message)
                answer = _safe_text(turn.assistant_response)
                if not question or not answer:
                    continue
                workspace = ""
                if turn.workspace:
                    workspace = str(Path(turn.workspace).expanduser().resolve())
                connection.execute(
                    """
                    DELETE FROM conversation_turns_fts
                    WHERE source = ? AND session_id = ? AND turn_id = ?
                    """,
                    (turn.source, turn.session_id, turn.turn_id),
                )
                connection.execute(
                    """
                    INSERT INTO conversation_turns
                        (source, session_id, turn_id, workspace, user_message,
                         assistant_response, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source, session_id, turn_id) DO UPDATE SET
                        workspace = excluded.workspace,
                        user_message = excluded.user_message,
                        assistant_response = excluded.assistant_response,
                        created_at = excluded.created_at
                    """,
                    (
                        turn.source,
                        turn.session_id,
                        turn.turn_id,
                        workspace,
                        question,
                        answer,
                        turn.created_at,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO conversation_turns_fts
                        (source, session_id, turn_id, workspace, user_message,
                         assistant_response)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        turn.source,
                        turn.session_id,
                        turn.turn_id,
                        workspace,
                        question,
                        answer,
                    ),
                )
                imported += 1
        return imported

    def import_events(self, events: Iterable[ConversationEvent]) -> int:
        imported = 0
        with self._connect() as connection:
            for event in events:
                safe_payload = _safe_event_value(event.payload)
                payload_json = json.dumps(
                    safe_payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                if len(payload_json) < 3 or payload_json in {"null", '""', "[]", "{}"}:
                    continue
                event_text = f"{event.tool_name}\n{payload_json}".strip()
                compressed_payload = zlib.compress(payload_json.encode("utf-8"), level=6)
                connection.execute(
                    """
                    DELETE FROM conversation_events_fts
                    WHERE source = ? AND session_id = ? AND event_id = ?
                    """,
                    (event.source, event.session_id, event.event_id),
                )
                connection.execute(
                    """
                    INSERT INTO conversation_events
                        (source, session_id, event_id, turn_id, workspace,
                         event_order, event_type, tool_name, status, payload_z)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source, session_id, event_id) DO UPDATE SET
                        turn_id = excluded.turn_id,
                        workspace = excluded.workspace,
                        event_order = excluded.event_order,
                        event_type = excluded.event_type,
                        tool_name = excluded.tool_name,
                        status = excluded.status,
                        payload_z = excluded.payload_z
                    """,
                    (
                        event.source,
                        event.session_id,
                        event.event_id,
                        event.turn_id,
                        event.workspace,
                        event.event_order,
                        event.event_type,
                        event.tool_name,
                        event.status,
                        compressed_payload,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO conversation_events_fts
                        (source, session_id, event_id, turn_id, workspace,
                         event_type, tool_name, status, search_text)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.source,
                        event.session_id,
                        event.event_id,
                        event.turn_id,
                        event.workspace,
                        event.event_type,
                        event.tool_name,
                        event.status,
                        _event_search_text(event_text),
                    ),
                )
                imported += 1
        return imported

    def search_events(
        self,
        query: str,
        *,
        project_name: str,
        limit: int = 5,
        max_chars: int = MAX_SEARCH_CHARS,
    ) -> list[dict[str, str]]:
        if not isinstance(query, str) or len(query) > 1000:
            raise ValueError("A consulta de eventos deve ter até 1000 caracteres.")
        if not 1 <= limit <= 5 or not 1 <= max_chars <= MAX_SEARCH_CHARS:
            raise ValueError("O limite da busca de eventos excede o máximo permitido.")
        if not re.fullmatch(r"[a-zA-Z0-9._-]{1,100}", project_name):
            raise ValueError("O nome do projeto contém caracteres inválidos.")
        terms = list(dict.fromkeys(_SEARCH_TOKEN.findall(query.casefold())))[:20]
        if not terms:
            return []
        match_query = " OR ".join(f'"{term}"' for term in terms)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT e.source, e.session_id, e.event_id, e.event_type,
                       e.tool_name, e.status, e.payload_z
                FROM conversation_events_fts AS f
                JOIN conversation_events AS e
                  ON e.source = f.source
                 AND e.session_id = f.session_id
                 AND e.event_id = f.event_id
                WHERE conversation_events_fts MATCH ?
                  AND substr(f.workspace, -length(?)) = ?
                ORDER BY bm25(conversation_events_fts), e.source,
                         e.session_id, e.event_order
                LIMIT ?
                """,
                (match_query, f"/{project_name}", f"/{project_name}", limit * 4),
            ).fetchall()
        results: list[dict[str, str]] = []
        used_chars = 0
        for source, session_id, event_id, event_type, tool_name, status, payload_z in rows:
            payload_json = zlib.decompress(payload_z).decode("utf-8")
            event_data = json.loads(payload_json)
            if isinstance(event_data, str):
                event_text = event_data
            elif isinstance(event_data, (dict, list)):
                event_text = json.dumps(event_data, ensure_ascii=False)
            else:
                event_text = str(event_data)
            prefix = f"[{event_type} | {tool_name or source} | {status}] "
            result = {
                "source": source,
                "user_message": prefix + (event_text if event_type == "tool_call" else ""),
                "assistant_response": (prefix + event_text if event_type != "tool_call" else ""),
            }
            size = len(result["user_message"]) + len(result["assistant_response"])
            if used_chars + size > max_chars:
                remaining = max_chars - used_chars
                if remaining < 200:
                    break
                user_budget = min(len(result["user_message"]), remaining // 2)
                result["user_message"] = result["user_message"][:user_budget]
                result["assistant_response"] = result["assistant_response"][
                    : remaining - user_budget
                ]
                size = len(result["user_message"]) + len(result["assistant_response"])
            results.append(result)
            used_chars += size
            if len(results) >= limit or used_chars >= max_chars:
                break
        return results

    def search(
        self,
        query: str,
        *,
        workspace: str | Path | None,
        project_name: str | None = None,
        limit: int = MAX_SEARCH_RESULTS,
        max_chars: int = MAX_SEARCH_CHARS,
    ) -> list[dict[str, str]]:
        if not isinstance(query, str) or len(query) > 1000:
            raise ValueError("A consulta de histórico deve ter até 1000 caracteres.")
        if not 1 <= limit <= MAX_SEARCH_RESULTS or not 1 <= max_chars <= MAX_SEARCH_CHARS:
            raise ValueError("O limite da busca de histórico excede o máximo permitido.")
        terms = list(dict.fromkeys(_SEARCH_TOKEN.findall(query.casefold())))[:20]
        if not terms:
            return []
        match_query = " OR ".join(f'"{term}"' for term in terms)
        scope_filter = ""
        parameters: list[Any] = [match_query]
        if workspace is not None:
            root = str(Path(workspace).expanduser().resolve())
            scope_filter = " AND (workspace = ? OR substr(workspace, 1, length(?) + 1) = ? || '/')"
            parameters.extend((root, root, root))
        elif project_name is not None:
            if not re.fullmatch(r"[a-zA-Z0-9._-]{1,100}", project_name):
                raise ValueError("O nome do projeto contém caracteres inválidos.")
            scope_filter = " AND workspace LIKE ?"
            parameters.append(f"%/{project_name}")
        parameters.append(limit * 3)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT source, session_id, turn_id, user_message, assistant_response
                FROM conversation_turns_fts
                WHERE conversation_turns_fts MATCH ?
                {scope_filter}
                ORDER BY bm25(conversation_turns_fts), source, session_id, turn_id
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        results: list[dict[str, str]] = []
        used_chars = 0
        seen: set[tuple[str, str, str]] = set()
        for source, session_id, turn_id, question, answer in rows:
            identity = (source, session_id, turn_id)
            if identity in seen:
                continue
            seen.add(identity)
            result = {
                "source": source,
                "user_message": question,
                "assistant_response": answer,
            }
            size = len(question) + len(answer)
            if used_chars + size > max_chars:
                remaining = max_chars - used_chars
                if remaining < 200:
                    break
                question_budget = min(len(question), remaining // 2)
                answer_budget = remaining - question_budget
                result["user_message"] = question[:question_budget]
                result["assistant_response"] = answer[:answer_budget]
                size = question_budget + answer_budget
            results.append(result)
            used_chars += size
            if len(results) >= limit or used_chars >= max_chars:
                break
        return results

    def count(self) -> int:
        with self._connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM conversation_turns").fetchone()[0])


def history_database_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / "history" / "training.sqlite3"


def workspace_from_metadata(metadata_path: Path) -> str:
    return _workspace_path(metadata_path)
