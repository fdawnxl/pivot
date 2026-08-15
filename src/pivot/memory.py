"""SQLite-backed long-term memory for persistent device agents."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from .models import Message, ToolCall, normalize_content

LOGGER = logging.getLogger(__name__)

MemoryKind = Literal["fact", "preference", "episode", "procedure"]


class MemoryError(RuntimeError):
    """Raised when durable agent memory cannot be read or updated safely."""


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    """One sourced and versionable long-term memory record."""

    memory_id: str
    namespace: str
    kind: MemoryKind
    content: str
    source: str
    confidence: float
    valid_from: float
    valid_until: float | None
    supersedes: str | None
    sensitivity: str
    created_at: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "namespace": self.namespace,
            "kind": self.kind,
            "content": self.content,
            "source": self.source,
            "confidence": self.confidence,
            "valid_from": self.valid_from,
            "valid_until": self.valid_until,
            "supersedes": self.supersedes,
            "sensitivity": self.sensitivity,
            "created_at": self.created_at,
        }


class MemoryStore:
    """Own agent identity, activations, messages, tasks, and retrieved memory."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "pivot.db"
        try:
            self._connection = sqlite3.connect(self.path, check_same_thread=False)
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute("PRAGMA synchronous=FULL")
        except sqlite3.Error as exc:
            raise MemoryError(f"Cannot open memory database {self.path}: {exc}") from exc
        self._lock = threading.RLock()
        self._fts_enabled = False
        self._closed = False
        self._initialize()

    def _initialize(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS agents (
            agent_id TEXT PRIMARY KEY,
            role TEXT NOT NULL,
            name TEXT NOT NULL,
            parent_id TEXT,
            capabilities_json TEXT NOT NULL DEFAULT '[]',
            events_json TEXT NOT NULL DEFAULT '[]',
            state TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS one_main_agent ON agents(role) WHERE role = 'main';
        CREATE TABLE IF NOT EXISTS activations (
            activation_id TEXT PRIMARY KEY,
            agent_id TEXT NOT NULL REFERENCES agents(agent_id),
            source TEXT NOT NULL,
            state TEXT NOT NULL,
            input_json TEXT NOT NULL,
            response TEXT,
            error TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS messages (
            message_id INTEGER PRIMARY KEY AUTOINCREMENT,
            activation_id TEXT NOT NULL REFERENCES activations(activation_id),
            agent_id TEXT NOT NULL REFERENCES agents(agent_id),
            ordinal INTEGER NOT NULL,
            role TEXT NOT NULL,
            content_json TEXT,
            name TEXT,
            tool_calls_json TEXT NOT NULL DEFAULT '[]',
            tool_call_id TEXT,
            created_at REAL NOT NULL,
            UNIQUE(activation_id, ordinal)
        );
        CREATE INDEX IF NOT EXISTS messages_by_agent ON messages(agent_id, message_id);
        CREATE TABLE IF NOT EXISTS journal (
            entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id TEXT,
            activation_id TEXT,
            kind TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS memories (
            memory_id TEXT PRIMARY KEY,
            namespace TEXT NOT NULL,
            kind TEXT NOT NULL,
            content TEXT NOT NULL,
            source TEXT NOT NULL,
            confidence REAL NOT NULL,
            valid_from REAL NOT NULL,
            valid_until REAL,
            supersedes TEXT,
            sensitivity TEXT NOT NULL,
            deleted_at REAL,
            created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS memories_by_namespace ON memories(namespace, kind, created_at);
        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            owner_agent_id TEXT NOT NULL REFERENCES agents(agent_id),
            parent_task_id TEXT,
            description TEXT NOT NULL,
            state TEXT NOT NULL,
            result_json TEXT,
            error TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS continuations (
            continuation_id TEXT PRIMARY KEY,
            task_id TEXT,
            agent_id TEXT NOT NULL REFERENCES agents(agent_id),
            kind TEXT NOT NULL,
            state TEXT NOT NULL,
            condition_json TEXT NOT NULL,
            deadline REAL,
            result_json TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS world_state (
            source TEXT NOT NULL,
            field TEXT NOT NULL,
            value_json TEXT NOT NULL,
            observed_at REAL NOT NULL,
            valid_until REAL,
            PRIMARY KEY(source, field)
        );
        """
        try:
            with self._lock, self._connection:
                self._connection.executescript(schema)
                try:
                    self._connection.execute(
                        "CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(memory_id UNINDEXED, content)"
                    )
                except sqlite3.OperationalError:
                    LOGGER.warning("SQLite FTS5 is unavailable; memory recall will use substring matching")
                else:
                    self._fts_enabled = True
        except sqlite3.Error as exc:
            raise MemoryError(f"Cannot initialize memory database: {exc}") from exc

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._connection.close()
            self._closed = True

    def main_agent_id(self) -> str:
        """Return the durable main-agent identity, creating it once."""

        with self._lock, self._connection:
            row = self._connection.execute("SELECT agent_id FROM agents WHERE role = 'main'").fetchone()
            if row is not None:
                return str(row["agent_id"])
            agent_id = str(uuid4())
            now = time.time()
            self._connection.execute(
                "INSERT INTO agents(agent_id, role, name, state, created_at, updated_at) VALUES (?, 'main', 'main', 'ready', ?, ?)",
                (agent_id, now, now),
            )
            self.append_journal("agent.created", {"role": "main", "name": "main"}, agent_id=agent_id)
            return agent_id

    def create_worker(
        self,
        *,
        name: str,
        parent_id: str,
        capabilities: Sequence[str],
        events: Sequence[str],
    ) -> str:
        agent_id = str(uuid4())
        now = time.time()
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT INTO agents(
                    agent_id, role, name, parent_id, capabilities_json, events_json, state, created_at, updated_at
                ) VALUES (?, 'worker', ?, ?, ?, ?, 'created', ?, ?)""",
                (agent_id, name, parent_id, _json(list(capabilities)), _json(list(events)), now, now),
            )
        self.append_journal("agent.created", {"role": "worker", "name": name}, agent_id=agent_id)
        return agent_id

    def agent_rows(self) -> tuple[dict[str, Any], ...]:
        with self._lock:
            rows = self._connection.execute("SELECT * FROM agents ORDER BY created_at, agent_id").fetchall()
        return tuple(
            {
                **dict(row),
                "capabilities": tuple(json.loads(row["capabilities_json"])),
                "events": tuple(json.loads(row["events_json"])),
            }
            for row in rows
        )

    def update_agent_state(self, agent_id: str, state: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE agents SET state = ?, updated_at = ? WHERE agent_id = ?",
                (state, time.time(), agent_id),
            )

    def create_activation(self, agent_id: str, source: str, content: Any) -> str:
        activation_id = str(uuid4())
        now = time.time()
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT INTO activations(
                    activation_id, agent_id, source, state, input_json, created_at, updated_at
                ) VALUES (?, ?, ?, 'running', ?, ?, ?)""",
                (activation_id, agent_id, source, _json(_content_value(content)), now, now),
            )
        self.append_journal(
            "activation.started",
            {"source": source, "content": _content_value(content)},
            agent_id=agent_id,
            activation_id=activation_id,
        )
        return activation_id

    def finish_activation(
        self,
        activation_id: str,
        state: Literal["completed", "failed", "cancelled"],
        *,
        response: str | None = None,
        error: str | None = None,
    ) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE activations SET state = ?, response = ?, error = ?, updated_at = ? WHERE activation_id = ?",
                (state, response, error, time.time(), activation_id),
            )
            row = self._connection.execute(
                "SELECT agent_id FROM activations WHERE activation_id = ?", (activation_id,)
            ).fetchone()
        self.append_journal(
            f"activation.{state}",
            {"response": response, "error": error},
            agent_id=str(row["agent_id"]) if row else None,
            activation_id=activation_id,
        )

    def append_message(self, agent_id: str, activation_id: str, message: Message) -> None:
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT COALESCE(MAX(ordinal), -1) + 1 AS ordinal FROM messages WHERE activation_id = ?",
                (activation_id,),
            ).fetchone()
            ordinal = int(row["ordinal"])
            self._connection.execute(
                """INSERT INTO messages(
                    activation_id, agent_id, ordinal, role, content_json, name, tool_calls_json, tool_call_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    activation_id,
                    agent_id,
                    ordinal,
                    message.role,
                    _json(_content_value(message.content)) if message.content is not None else None,
                    message.name,
                    _json([call.as_dict() for call in message.tool_calls]),
                    message.tool_call_id,
                    time.time(),
                ),
            )
        self.append_journal(
            "message.appended",
            message.as_dict(),
            agent_id=agent_id,
            activation_id=activation_id,
        )

    def messages(self, agent_id: str, *, limit: int | None = None) -> tuple[Message, ...]:
        query = "SELECT * FROM messages WHERE agent_id = ? ORDER BY message_id"
        parameters: tuple[Any, ...] = (agent_id,)
        if limit is not None:
            query = "SELECT * FROM (SELECT * FROM messages WHERE agent_id = ? ORDER BY message_id DESC LIMIT ?) ORDER BY message_id"
            parameters = (agent_id, limit)
        with self._lock:
            rows = self._connection.execute(query, parameters).fetchall()
        return tuple(_row_message(row) for row in rows)

    def context_messages(
        self,
        agent_id: str,
        activation_id: str,
        *,
        max_messages: int = 48,
        max_chars: int = 32000,
    ) -> tuple[Message, ...]:
        with self._lock:
            rows = self._connection.execute(
                """SELECT m.* FROM messages AS m
                JOIN activations AS a ON a.activation_id = m.activation_id
                WHERE m.agent_id = ? AND (a.state = 'completed' OR a.activation_id = ?)
                ORDER BY m.message_id DESC LIMIT ?""",
                (agent_id, activation_id, max_messages),
            ).fetchall()
        selected: list[Message] = []
        used = 0
        for row in rows:
            message = _row_message(row)
            size = len(_json(message.as_dict()))
            if selected and used + size > max_chars:
                break
            selected.append(message)
            used += size
        selected.reverse()
        while selected and selected[0].role == "tool":
            selected.pop(0)
        return tuple(selected)

    def append_journal(
        self,
        kind: str,
        payload: Any,
        *,
        agent_id: str | None = None,
        activation_id: str | None = None,
    ) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO journal(agent_id, activation_id, kind, payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (agent_id, activation_id, kind, _json(payload), time.time()),
            )

    def remember(
        self,
        *,
        namespace: str,
        kind: MemoryKind,
        content: str,
        source: str,
        confidence: float = 1.0,
        valid_until: float | None = None,
        supersedes: str | None = None,
        sensitivity: str = "normal",
    ) -> MemoryRecord:
        if kind not in {"fact", "preference", "episode", "procedure"}:
            raise MemoryError(f"Unsupported memory kind: {kind}")
        if not namespace.strip() or not content.strip() or not source.strip():
            raise MemoryError("Memory namespace, content, and source must be non-empty")
        if not 0 <= confidence <= 1:
            raise MemoryError("Memory confidence must be between 0 and 1")
        memory_id = str(uuid4())
        now = time.time()
        with self._lock, self._connection:
            if supersedes is not None:
                exists = self._connection.execute(
                    "SELECT 1 FROM memories WHERE memory_id = ? AND deleted_at IS NULL", (supersedes,)
                ).fetchone()
                if exists is None:
                    raise MemoryError(f"Cannot supersede unknown memory: {supersedes}")
            self._connection.execute(
                """INSERT INTO memories(
                    memory_id, namespace, kind, content, source, confidence, valid_from,
                    valid_until, supersedes, sensitivity, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    memory_id,
                    namespace,
                    kind,
                    content.strip(),
                    source,
                    confidence,
                    now,
                    valid_until,
                    supersedes,
                    sensitivity,
                    now,
                ),
            )
            if self._fts_enabled:
                self._connection.execute(
                    "INSERT INTO memory_fts(memory_id, content) VALUES (?, ?)", (memory_id, content.strip())
                )
        record = MemoryRecord(
            memory_id,
            namespace,
            kind,
            content.strip(),
            source,
            confidence,
            now,
            valid_until,
            supersedes,
            sensitivity,
            now,
        )
        self.append_journal("memory.remembered", record.as_dict())
        return record

    def recall(
        self,
        query: str,
        *,
        namespaces: Sequence[str],
        limit: int = 8,
    ) -> tuple[MemoryRecord, ...]:
        if limit < 1 or not namespaces:
            return ()
        placeholders = ",".join("?" for _ in namespaces)
        now = time.time()
        parameters: list[Any] = [*namespaces, now]
        base = (
            f"m.namespace IN ({placeholders}) AND m.deleted_at IS NULL "
            "AND (m.valid_until IS NULL OR m.valid_until > ?) "
            "AND NOT EXISTS (SELECT 1 FROM memories AS newer WHERE newer.supersedes = m.memory_id)"
        )
        terms = [item for item in re.findall(r"[^\W_]+", query, flags=re.UNICODE) if item]
        with self._lock:
            if self._fts_enabled and terms:
                expression = " OR ".join(f'"{term.replace(chr(34), "")}"' for term in terms[:12])
                rows = self._connection.execute(
                    f"""SELECT m.* FROM memory_fts AS f
                    JOIN memories AS m ON m.memory_id = f.memory_id
                    WHERE {base} AND memory_fts MATCH ?
                    ORDER BY bm25(memory_fts), m.confidence DESC, m.created_at DESC LIMIT ?""",
                    (*parameters, expression, limit),
                ).fetchall()
            else:
                like = f"%{query.strip()}%"
                rows = self._connection.execute(
                    f"SELECT m.* FROM memories AS m WHERE {base} AND (? = '%%' OR m.content LIKE ?) "
                    "ORDER BY m.confidence DESC, m.created_at DESC LIMIT ?",
                    (*parameters, like, like, limit),
                ).fetchall()
        return tuple(_row_memory(row) for row in rows)

    def forget(self, memory_id: str) -> bool:
        now = time.time()
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "UPDATE memories SET deleted_at = ? WHERE memory_id = ? AND deleted_at IS NULL",
                (now, memory_id),
            )
            if cursor.rowcount and self._fts_enabled:
                self._connection.execute("DELETE FROM memory_fts WHERE memory_id = ?", (memory_id,))
        if cursor.rowcount:
            self.append_journal("memory.forgotten", {"memory_id": memory_id})
        return bool(cursor.rowcount)

    def record_episode(self, agent_id: str, activation_id: str, stimulus: str, response: str) -> MemoryRecord:
        content = f"Stimulus: {stimulus[:1000]}\nOutcome: {response[:2000]}"
        return self.remember(
            namespace=f"agent:{agent_id}",
            kind="episode",
            content=content,
            source=f"activation:{activation_id}",
            confidence=1.0,
        )

    def upsert_task(
        self,
        task_id: str,
        owner_agent_id: str,
        description: str,
        state: str,
        *,
        parent_task_id: str | None = None,
        result: Any = None,
        error: str | None = None,
    ) -> None:
        now = time.time()
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT INTO tasks(task_id, owner_agent_id, parent_task_id, description, state, result_json, error, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET state=excluded.state, result_json=excluded.result_json,
                    error=excluded.error, updated_at=excluded.updated_at""",
                (task_id, owner_agent_id, parent_task_id, description, state, _json(result) if result is not None else None, error, now, now),
            )

    def save_continuation(
        self,
        continuation_id: str,
        agent_id: str,
        kind: str,
        state: str,
        condition: Mapping[str, Any],
        *,
        task_id: str | None = None,
        deadline: float | None = None,
        result: Any = None,
    ) -> None:
        now = time.time()
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT INTO continuations(
                    continuation_id, task_id, agent_id, kind, state, condition_json, deadline, result_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(continuation_id) DO UPDATE SET state=excluded.state, result_json=excluded.result_json,
                    updated_at=excluded.updated_at""",
                (continuation_id, task_id, agent_id, kind, state, _json(condition), deadline, _json(result) if result is not None else None, now, now),
            )

    def update_world_state(
        self,
        source: str,
        values: Mapping[str, Any],
        *,
        ttl: float | None = None,
    ) -> None:
        observed = time.time()
        valid_until = observed + ttl if ttl is not None else None
        with self._lock, self._connection:
            for field, value in values.items():
                self._connection.execute(
                    """INSERT INTO world_state(source, field, value_json, observed_at, valid_until)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(source, field) DO UPDATE SET value_json=excluded.value_json,
                        observed_at=excluded.observed_at, valid_until=excluded.valid_until""",
                    (source, str(field), _json(value), observed, valid_until),
                )

    def current_world_state(self) -> tuple[dict[str, Any], ...]:
        now = time.time()
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM world_state WHERE valid_until IS NULL OR valid_until > ? ORDER BY source, field",
                (now,),
            ).fetchall()
        return tuple(
            {
                "source": row["source"],
                "field": row["field"],
                "value": json.loads(row["value_json"]),
                "observed_at": row["observed_at"],
                "valid_until": row["valid_until"],
            }
            for row in rows
        )


class ContextBuilder:
    """Build a bounded prompt view from durable memory for one activation."""

    def __init__(self, store: MemoryStore, *, max_messages: int = 48, max_chars: int = 32000) -> None:
        if max_messages < 1 or max_chars < 1024:
            raise ValueError("Context limits are invalid")
        self.store = store
        self.max_messages = max_messages
        self.max_chars = max_chars

    def build(
        self,
        *,
        agent_id: str,
        activation_id: str,
        query: str,
        runtime_context: Mapping[str, Any],
    ) -> tuple[Message, ...]:
        memories = self.store.recall(
            query,
            namespaces=("global", f"agent:{agent_id}"),
            limit=8,
        )
        context = {
            **dict(runtime_context),
            "retrieved_memory": [record.as_dict() for record in memories],
            "world_state": list(self.store.current_world_state()),
            "memory_instruction": (
                "Retrieved memory may be stale or incorrect. Respect source, confidence, validity, and current measurements."
            ),
        }
        system = Message("system", "pivot runtime context:\n" + _json(context))
        recent = self.store.context_messages(
            agent_id,
            activation_id,
            max_messages=self.max_messages,
            max_chars=self.max_chars,
        )
        return (system, *recent)


class MemoryService:
    """Validate model-facing remember, recall, and forget operations."""

    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    def prompt_context(self) -> tuple[dict[str, Any], ...]:
        return (
            {"name": "memory.remember", "arguments": ["kind", "content", "confidence?", "valid_for?", "supersedes?"]},
            {"name": "memory.recall", "arguments": ["query", "limit?"]},
            {"name": "memory.forget", "arguments": ["memory_id"]},
        )

    def execute(self, agent_id: str, operation: str, arguments: Mapping[str, Any]) -> Any:
        if operation == "memory.remember":
            kind = arguments.get("kind")
            content = arguments.get("content")
            confidence = arguments.get("confidence", 1.0)
            valid_for = arguments.get("valid_for")
            if not isinstance(kind, str) or not isinstance(content, str):
                raise MemoryError("memory.remember requires string kind and content")
            if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
                raise MemoryError("memory confidence must be a number")
            if valid_for is not None and (
                not isinstance(valid_for, (int, float)) or isinstance(valid_for, bool) or valid_for <= 0
            ):
                raise MemoryError("memory valid_for must be a positive number")
            supersedes = arguments.get("supersedes")
            if supersedes is not None and not isinstance(supersedes, str):
                raise MemoryError("memory supersedes must be a memory id")
            record = self.store.remember(
                namespace=f"agent:{agent_id}",
                kind=kind,  # type: ignore[arg-type]
                content=content,
                source=f"agent:{agent_id}",
                confidence=float(confidence),
                valid_until=time.time() + float(valid_for) if valid_for is not None else None,
                supersedes=supersedes,
            )
            return record.as_dict()
        if operation == "memory.recall":
            query = arguments.get("query")
            limit = arguments.get("limit", 8)
            if not isinstance(query, str) or not isinstance(limit, int):
                raise MemoryError("memory.recall requires a string query and integer limit")
            return [
                item.as_dict()
                for item in self.store.recall(
                    query,
                    namespaces=("global", f"agent:{agent_id}"),
                    limit=min(max(limit, 1), 32),
                )
            ]
        if operation == "memory.forget":
            memory_id = arguments.get("memory_id")
            if not isinstance(memory_id, str):
                raise MemoryError("memory.forget requires memory_id")
            return {"memory_id": memory_id, "forgotten": self.store.forget(memory_id)}
        raise MemoryError(f"Unknown memory operation: {operation}")


def _row_message(row: sqlite3.Row) -> Message:
    content_value = json.loads(row["content_json"]) if row["content_json"] is not None else None
    content = normalize_content(content_value) if content_value is not None else None
    calls: list[ToolCall] = []
    for raw in json.loads(row["tool_calls_json"]):
        function = raw.get("function", raw)
        arguments = function.get("arguments", {})
        if isinstance(arguments, str):
            arguments = json.loads(arguments)
        calls.append(ToolCall(str(function["name"]), dict(arguments), raw.get("id")))
    return Message(str(row["role"]), content, row["name"], tuple(calls), row["tool_call_id"])


def _row_memory(row: sqlite3.Row) -> MemoryRecord:
    return MemoryRecord(
        str(row["memory_id"]),
        str(row["namespace"]),
        str(row["kind"]),  # type: ignore[arg-type]
        str(row["content"]),
        str(row["source"]),
        float(row["confidence"]),
        float(row["valid_from"]),
        float(row["valid_until"]) if row["valid_until"] is not None else None,
        str(row["supersedes"]) if row["supersedes"] is not None else None,
        str(row["sensitivity"]),
        float(row["created_at"]),
    )


def _content_value(content: Any) -> Any:
    return list(content) if isinstance(content, tuple) else content


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


__all__ = [
    "ContextBuilder",
    "MemoryError",
    "MemoryKind",
    "MemoryRecord",
    "MemoryService",
    "MemoryStore",
]
