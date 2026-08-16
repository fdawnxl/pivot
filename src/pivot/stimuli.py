"""Durable stimuli and the persistent main-agent reaction loop."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from .activation import ActivationProgress, AgentCancelled, CancellationToken, PersistentAgent, ProgressCallback
from .agents import AgentControl, AgentRecord
from .models import normalize_content

LOGGER = logging.getLogger(__name__)


class StimulusError(RuntimeError):
    """Raised when a stimulus is invalid or cannot be processed."""


class StimulusKind(StrEnum):
    """Framework-level reasons for activating the persistent main agent."""

    COMMAND = "command"
    OBSERVATION = "observation"
    WORKER_REPORT = "worker_report"
    TIMER = "timer"
    SYSTEM = "system"


class StimulusState(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


_DEFAULT_PRIORITIES = {
    StimulusKind.COMMAND: 50,
    StimulusKind.WORKER_REPORT: 40,
    StimulusKind.SYSTEM: 30,
    StimulusKind.OBSERVATION: 20,
    StimulusKind.TIMER: 10,
}
_TERMINAL_STATES = {StimulusState.COMPLETED, StimulusState.FAILED, StimulusState.CANCELLED}
_MAX_ENVELOPE_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class StimulusEnvelope:
    """One validated input addressed to the instance's sole main agent."""

    stimulus_id: str
    target_agent_id: str
    kind: StimulusKind
    source: str
    payload: dict[str, Any]
    priority: int
    created_at: float
    correlation_id: str | None = None
    causation_id: str | None = None
    dedupe_key: str | None = None
    state: StimulusState = StimulusState.QUEUED
    attempts: int = 0
    response: str | None = None
    error: str | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any], *, target_agent_id: str) -> "StimulusEnvelope":
        """Validate an untrusted adapter envelope and bind it to the main agent."""

        allowed = {
            "stimulus_id",
            "kind",
            "source",
            "payload",
            "priority",
            "correlation_id",
            "causation_id",
            "dedupe_key",
        }
        unknown = set(value) - allowed
        if unknown:
            raise StimulusError(f"Unknown stimulus fields: {', '.join(sorted(unknown))}")
        try:
            kind = StimulusKind(value.get("kind"))
        except (TypeError, ValueError) as exc:
            raise StimulusError("Stimulus kind is invalid") from exc
        source = value.get("source")
        if not isinstance(source, str) or not source.strip() or len(source) > 256 or "\x00" in source:
            raise StimulusError("Stimulus source must be a non-empty string of at most 256 characters")
        payload = value.get("payload")
        if not isinstance(payload, Mapping):
            raise StimulusError("Stimulus payload must be a JSON object")
        payload_value = dict(payload)
        if kind == StimulusKind.COMMAND:
            if "content" not in payload_value:
                raise StimulusError("Command stimuli require payload.content")
            try:
                normalized = normalize_content(payload_value["content"])
            except (TypeError, ValueError) as exc:
                raise StimulusError(f"Command content is invalid: {exc}") from exc
            payload_value["content"] = list(normalized) if isinstance(normalized, tuple) else normalized
        priority = value.get("priority", _DEFAULT_PRIORITIES[kind])
        if not isinstance(priority, int) or isinstance(priority, bool) or not -100 <= priority <= 100:
            raise StimulusError("Stimulus priority must be an integer between -100 and 100")
        stimulus_id = _optional_uuid(value.get("stimulus_id"), "stimulus_id") or str(uuid4())
        correlation_id = _optional_identifier(value.get("correlation_id"), "correlation_id")
        causation_id = _optional_identifier(value.get("causation_id"), "causation_id")
        dedupe_key = _optional_identifier(value.get("dedupe_key"), "dedupe_key")
        envelope = cls(
            stimulus_id,
            target_agent_id,
            kind,
            source.strip(),
            payload_value,
            priority,
            time.time(),
            correlation_id,
            causation_id,
            dedupe_key,
        )
        if len(_json(envelope.as_dict()).encode("utf-8")) > _MAX_ENVELOPE_BYTES:
            raise StimulusError(f"Stimulus envelope exceeds {_MAX_ENVELOPE_BYTES} bytes")
        return envelope

    def activation_content(self) -> Any:
        """Return provider-neutral content for one finite activation."""

        if self.kind == StimulusKind.COMMAND:
            return self.payload["content"]
        return "pivot stimulus:\n" + _json(
            {
                "stimulus_id": self.stimulus_id,
                "kind": self.kind,
                "source": self.source,
                "payload": self.payload,
                "correlation_id": self.correlation_id,
                "causation_id": self.causation_id,
            }
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "stimulus_id": self.stimulus_id,
            "target_agent_id": self.target_agent_id,
            "kind": self.kind,
            "source": self.source,
            "payload": self.payload,
            "priority": self.priority,
            "created_at": self.created_at,
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
            "dedupe_key": self.dedupe_key,
            "state": self.state,
            "attempts": self.attempts,
            "response": self.response,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class OutputEnvelope:
    """A durable, transport-neutral result emitted by the main agent."""

    output_id: str
    stimulus_id: str
    agent_id: str
    kind: str
    payload: dict[str, Any]
    correlation_id: str | None
    created_at: float = field(default_factory=time.time)

    def as_dict(self) -> dict[str, Any]:
        return {
            "output_id": self.output_id,
            "stimulus_id": self.stimulus_id,
            "agent_id": self.agent_id,
            "kind": self.kind,
            "payload": self.payload,
            "correlation_id": self.correlation_id,
            "created_at": self.created_at,
        }


class StimulusInbox:
    """SQLite-backed priority inbox shared by every instance ingress adapter."""

    def __init__(self, database: str | Path) -> None:
        self.path = Path(database).expanduser().resolve()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._closed = False
        self._initialize()

    def _initialize(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS stimuli (
            stimulus_id TEXT PRIMARY KEY,
            target_agent_id TEXT NOT NULL REFERENCES agents(agent_id),
            kind TEXT NOT NULL,
            source TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            priority INTEGER NOT NULL,
            correlation_id TEXT,
            causation_id TEXT,
            dedupe_key TEXT,
            state TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            response TEXT,
            error TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS stimuli_queue
            ON stimuli(state, priority DESC, created_at, stimulus_id);
        CREATE UNIQUE INDEX IF NOT EXISTS stimuli_dedupe
            ON stimuli(source, dedupe_key) WHERE dedupe_key IS NOT NULL;
        CREATE TABLE IF NOT EXISTS outputs (
            output_id TEXT PRIMARY KEY,
            stimulus_id TEXT NOT NULL REFERENCES stimuli(stimulus_id),
            agent_id TEXT NOT NULL REFERENCES agents(agent_id),
            kind TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            correlation_id TEXT,
            created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS outputs_by_time ON outputs(created_at, output_id);
        """
        with self._lock, self._connection:
            self._connection.executescript(schema)
            self._connection.execute(
                "UPDATE stimuli SET state = 'queued', updated_at = ? WHERE state = 'processing'",
                (time.time(),),
            )

    def enqueue(self, envelope: StimulusEnvelope) -> tuple[StimulusEnvelope, bool]:
        """Append one envelope and report whether a new row was created."""

        now = time.time()
        with self._condition, self._connection:
            if self._closed:
                raise StimulusError("Stimulus inbox is closed")
            try:
                self._connection.execute(
                    """INSERT INTO stimuli(
                        stimulus_id, target_agent_id, kind, source, payload_json, priority,
                        correlation_id, causation_id, dedupe_key, state, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)""",
                    (
                        envelope.stimulus_id,
                        envelope.target_agent_id,
                        envelope.kind,
                        envelope.source,
                        _json(envelope.payload),
                        envelope.priority,
                        envelope.correlation_id,
                        envelope.causation_id,
                        envelope.dedupe_key,
                        envelope.created_at,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                existing = self._find_existing(envelope)
                if existing is None:
                    raise StimulusError("Stimulus id conflicts with an existing envelope") from exc
                return existing, False
            self._condition.notify_all()
        return envelope, True

    def claim_next(self, *, timeout: float = 0.5) -> StimulusEnvelope | None:
        """Claim the highest-priority queued item, preserving FIFO within a priority."""

        deadline = time.monotonic() + timeout
        with self._condition:
            while not self._closed:
                row = self._connection.execute(
                    """SELECT * FROM stimuli WHERE state = 'queued'
                    ORDER BY priority DESC, created_at, stimulus_id LIMIT 1"""
                ).fetchone()
                if row is not None:
                    with self._connection:
                        cursor = self._connection.execute(
                            """UPDATE stimuli SET state = 'processing', attempts = attempts + 1, updated_at = ?
                            WHERE stimulus_id = ? AND state = 'queued'""",
                            (time.time(), row["stimulus_id"]),
                        )
                    if cursor.rowcount:
                        return self.get(str(row["stimulus_id"]))
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)
        return None

    def finish(
        self,
        stimulus_id: str,
        state: StimulusState,
        *,
        response: str | None = None,
        error: str | None = None,
    ) -> StimulusEnvelope:
        if state not in _TERMINAL_STATES:
            raise ValueError("Stimulus can only finish in a terminal state")
        with self._condition, self._connection:
            cursor = self._connection.execute(
                "UPDATE stimuli SET state = ?, response = ?, error = ?, updated_at = ? WHERE stimulus_id = ?",
                (state, response, error, time.time(), stimulus_id),
            )
            if not cursor.rowcount:
                raise StimulusError(f"Unknown stimulus: {stimulus_id}")
            self._condition.notify_all()
        return self.get(stimulus_id)

    def cancel_queued(self, stimulus_id: str) -> bool:
        with self._condition, self._connection:
            cursor = self._connection.execute(
                "UPDATE stimuli SET state = 'cancelled', updated_at = ? WHERE stimulus_id = ? AND state = 'queued'",
                (time.time(), stimulus_id),
            )
            self._condition.notify_all()
        return bool(cursor.rowcount)

    def get(self, stimulus_id: str) -> StimulusEnvelope:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM stimuli WHERE stimulus_id = ?", (stimulus_id,)
            ).fetchone()
        if row is None:
            raise StimulusError(f"Unknown stimulus: {stimulus_id}")
        return _row_stimulus(row)

    def list(self, *, limit: int = 100) -> tuple[StimulusEnvelope, ...]:
        limit = min(max(limit, 1), 1000)
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM stimuli ORDER BY created_at DESC, stimulus_id DESC LIMIT ?", (limit,)
            ).fetchall()
        return tuple(_row_stimulus(row) for row in rows)

    def wait(self, stimulus_id: str, *, timeout: float | None = None) -> StimulusEnvelope:
        deadline = time.monotonic() + timeout if timeout is not None else None
        with self._condition:
            while True:
                envelope = self.get(stimulus_id)
                if envelope.state in _TERMINAL_STATES:
                    return envelope
                if self._closed:
                    raise StimulusError("Stimulus inbox closed before processing completed")
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise TimeoutError(f"Timed out waiting for stimulus {stimulus_id}")
                self._condition.wait(remaining)

    def append_output(self, output: OutputEnvelope) -> None:
        with self._condition, self._connection:
            self._connection.execute(
                """INSERT INTO outputs(
                    output_id, stimulus_id, agent_id, kind, payload_json, correlation_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    output.output_id,
                    output.stimulus_id,
                    output.agent_id,
                    output.kind,
                    _json(output.payload),
                    output.correlation_id,
                    output.created_at,
                ),
            )
            self._condition.notify_all()

    def outputs(self, *, limit: int = 100) -> tuple[OutputEnvelope, ...]:
        limit = min(max(limit, 1), 1000)
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM outputs ORDER BY created_at DESC, output_id DESC LIMIT ?", (limit,)
            ).fetchall()
        return tuple(_row_output(row) for row in rows)

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._condition.notify_all()
            self._connection.close()

    def wake(self) -> None:
        with self._condition:
            self._condition.notify_all()

    def _find_existing(self, envelope: StimulusEnvelope) -> StimulusEnvelope | None:
        row = self._connection.execute(
            "SELECT * FROM stimuli WHERE stimulus_id = ?", (envelope.stimulus_id,)
        ).fetchone()
        if row is not None:
            existing = _row_stimulus(row)
            if (
                existing.kind != envelope.kind
                or existing.source != envelope.source
                or existing.payload != envelope.payload
                or existing.priority != envelope.priority
                or existing.correlation_id != envelope.correlation_id
                or existing.causation_id != envelope.causation_id
                or existing.dedupe_key != envelope.dedupe_key
            ):
                raise StimulusError("Stimulus id conflicts with different envelope content")
            return existing
        if envelope.dedupe_key is not None:
            row = self._connection.execute(
                "SELECT * FROM stimuli WHERE source = ? AND dedupe_key = ?",
                (envelope.source, envelope.dedupe_key),
            ).fetchone()
        return _row_stimulus(row) if row is not None else None


ReactorListener = Callable[[str, Mapping[str, Any]], None]


class MainAgentReactor:
    """Continuously activate the main agent from one durable, unified inbox."""

    def __init__(
        self,
        main_agent: PersistentAgent,
        agents: AgentControl,
        inbox: StimulusInbox,
    ) -> None:
        self.main_agent = main_agent
        self.agents = agents
        self.inbox = inbox
        self._listeners: set[ReactorListener] = set()
        self._progress: dict[str, ProgressCallback] = {}
        self._tokens: dict[str, CancellationToken] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self.agents.set_completion_handler(self._on_worker_completion)
        self._thread = threading.Thread(target=self._run, name="pivot-main-reactor", daemon=True)
        self._thread.start()
        LOGGER.info("Main-agent reactor started agent_id=%s", self.main_agent.agent_id)

    def subscribe(self, listener: ReactorListener) -> Callable[[], None]:
        with self._lock:
            self._listeners.add(listener)

        def unsubscribe() -> None:
            with self._lock:
                self._listeners.discard(listener)

        return unsubscribe

    def inject(
        self,
        value: Mapping[str, Any],
        *,
        progress: ProgressCallback | None = None,
        cancellation: CancellationToken | None = None,
    ) -> str:
        envelope = StimulusEnvelope.from_mapping(value, target_agent_id=self.main_agent.agent_id)
        with self._lock:
            if progress is not None:
                self._progress[envelope.stimulus_id] = progress
            if cancellation is not None:
                self._tokens[envelope.stimulus_id] = cancellation
        accepted, created = self.inbox.enqueue(envelope)
        if not created:
            with self._lock:
                self._progress.pop(envelope.stimulus_id, None)
                self._tokens.pop(envelope.stimulus_id, None)
        else:
            self._emit("stimulus_changed", accepted.as_dict())
        return accepted.stimulus_id

    def wait(self, stimulus_id: str, *, timeout: float | None = None) -> StimulusEnvelope:
        return self.inbox.wait(stimulus_id, timeout=timeout)

    def cancel(self, stimulus_id: str) -> bool:
        if self.inbox.cancel_queued(stimulus_id):
            with self._lock:
                self._progress.pop(stimulus_id, None)
                self._tokens.pop(stimulus_id, None)
            self._emit("stimulus_changed", self.inbox.get(stimulus_id).as_dict())
            return True
        with self._lock:
            token = self._tokens.get(stimulus_id)
        if token is None:
            return False
        token.cancel()
        return True

    def interrupt(self) -> bool:
        with self._lock:
            tokens = tuple(self._tokens.values())
        for token in tokens:
            token.cancel()
        return self.agents.cancel_workers() or bool(tokens)

    def emit_runtime(self, event: str, payload: Mapping[str, Any]) -> None:
        self._emit(event, payload)

    def close(self) -> None:
        self.agents.set_completion_handler(None)
        self._stop.set()
        self.interrupt()
        self.inbox.wake()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join()
        self._thread = None
        LOGGER.info("Main-agent reactor stopped agent_id=%s", self.main_agent.agent_id)

    def _run(self) -> None:
        while not self._stop.is_set():
            envelope = self.inbox.claim_next(timeout=0.25)
            if envelope is None:
                continue
            self._emit("stimulus_changed", envelope.as_dict())
            self._process(envelope)

    def _process(self, envelope: StimulusEnvelope) -> None:
        with self._lock:
            token = self._tokens.setdefault(envelope.stimulus_id, CancellationToken())
            progress = self._progress.get(envelope.stimulus_id)

        def emit_progress(update: ActivationProgress) -> None:
            if progress is not None:
                progress(update)
            self._emit(
                "activation_progress",
                {
                    "stimulus_id": envelope.stimulus_id,
                    "kind": update.kind,
                    "agent_id": update.agent_id,
                    "activation_id": update.activation_id,
                    "message": update.message,
                    "round_number": update.round_number,
                    "name": update.name,
                    "result": update.result,
                },
            )

        try:
            response = self.main_agent.activate(
                envelope.activation_content(),
                source="user" if envelope.kind == StimulusKind.COMMAND else envelope.kind.value,
                progress=emit_progress,
                cancellation=token,
            )
        except AgentCancelled as exc:
            finished = self.inbox.finish(envelope.stimulus_id, StimulusState.CANCELLED, error=str(exc))
        except Exception as exc:
            LOGGER.error(
                "Stimulus processing failed stimulus_id=%s kind=%s error_type=%s",
                envelope.stimulus_id,
                envelope.kind,
                type(exc).__name__,
            )
            finished = self.inbox.finish(
                envelope.stimulus_id,
                StimulusState.FAILED,
                error=f"{type(exc).__name__}: {exc}",
            )
        else:
            output = OutputEnvelope(
                str(uuid4()),
                envelope.stimulus_id,
                self.main_agent.agent_id,
                "response",
                {"content": response, "stimulus_kind": envelope.kind.value, "source": envelope.source},
                envelope.correlation_id,
            )
            self.inbox.append_output(output)
            self._emit("output_available", output.as_dict())
            finished = self.inbox.finish(envelope.stimulus_id, StimulusState.COMPLETED, response=response)
        finally:
            with self._lock:
                self._progress.pop(envelope.stimulus_id, None)
                self._tokens.pop(envelope.stimulus_id, None)
        self._emit("stimulus_changed", finished.as_dict())

    def _on_worker_completion(self, record: AgentRecord) -> None:
        snapshot = record.as_dict()
        self.inject(
            {
                "kind": StimulusKind.WORKER_REPORT,
                "source": f"agent:{record.agent_id}",
                "payload": snapshot,
                "causation_id": record.task_id,
                "dedupe_key": record.task_id or record.agent_id,
            }
        )

    def _emit(self, event: str, payload: Mapping[str, Any]) -> None:
        with self._lock:
            listeners = tuple(self._listeners)
        for listener in listeners:
            try:
                listener(event, payload)
            except Exception as exc:
                LOGGER.warning("Reactor listener failed event=%s error_type=%s", event, type(exc).__name__)


def _row_stimulus(row: sqlite3.Row) -> StimulusEnvelope:
    return StimulusEnvelope(
        str(row["stimulus_id"]),
        str(row["target_agent_id"]),
        StimulusKind(row["kind"]),
        str(row["source"]),
        dict(json.loads(row["payload_json"])),
        int(row["priority"]),
        float(row["created_at"]),
        str(row["correlation_id"]) if row["correlation_id"] is not None else None,
        str(row["causation_id"]) if row["causation_id"] is not None else None,
        str(row["dedupe_key"]) if row["dedupe_key"] is not None else None,
        StimulusState(row["state"]),
        int(row["attempts"]),
        str(row["response"]) if row["response"] is not None else None,
        str(row["error"]) if row["error"] is not None else None,
    )


def _row_output(row: sqlite3.Row) -> OutputEnvelope:
    return OutputEnvelope(
        str(row["output_id"]),
        str(row["stimulus_id"]),
        str(row["agent_id"]),
        str(row["kind"]),
        dict(json.loads(row["payload_json"])),
        str(row["correlation_id"]) if row["correlation_id"] is not None else None,
        float(row["created_at"]),
    )


def _optional_uuid(value: Any, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise StimulusError(f"Stimulus {name} must be a UUID")
    try:
        return str(UUID(value))
    except ValueError as exc:
        raise StimulusError(f"Stimulus {name} must be a UUID") from exc


def _optional_identifier(value: Any, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 256 or "\x00" in value:
        raise StimulusError(f"Stimulus {name} must be a non-empty string of at most 256 characters")
    return value.strip()


def _json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise StimulusError("Stimulus data must be JSON serializable") from exc


__all__ = [
    "MainAgentReactor",
    "OutputEnvelope",
    "StimulusEnvelope",
    "StimulusError",
    "StimulusInbox",
    "StimulusKind",
    "StimulusState",
]
