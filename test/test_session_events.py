from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from pivot.capabilities import CapabilityRegistry
from pivot.events import EVENT_WAIT_TOOL, EventError, EventPool, EventScriptRunner, EventService, EventSupervisor, load_event_scripts_isolated
from pivot.memory import TextMemory
from pivot.models import CapabilityDescriptor, EventDescriptor
from pivot.session import CancellationToken, ConversationSession, SessionCancelled


class FakeLLM:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, messages, *, tools=()):
        self.calls += 1
        if self.calls == 1:
            return {"choices": [{"message": {"content": "", "tool_calls": [{"id": "c1", "function": {"name": "echo", "arguments": '{"value":"ok"}'}}]}}]}
        return {"choices": [{"message": {"content": "finished"}}]}


def test_session_executes_tools_and_persists(tmp_path: Path) -> None:
    registry = CapabilityRegistry()
    registry.register(CapabilityDescriptor("echo", "work", "Echo value", {"type": "object"}), lambda value: value)
    session_id = str(uuid4())
    session = ConversationSession(session_id, llm=FakeLLM(), capabilities=registry, memory=TextMemory(tmp_path), max_rounds=3)
    assert session.run("hello") == "finished"
    assert "finished" in (tmp_path / session_id / "history.jsonl").read_text(encoding="utf-8")
    restored = ConversationSession(session_id, llm=FakeLLM(), capabilities=registry, memory=TextMemory(tmp_path), max_rounds=3)
    assert len(restored.history) == len(session.history)


def test_session_emits_user_safe_progress_updates(tmp_path: Path) -> None:
    registry = CapabilityRegistry()
    registry.register(CapabilityDescriptor("echo", "work", "Echo value", {"type": "object"}), lambda value: value)
    updates = []
    session = ConversationSession(str(uuid4()), llm=FakeLLM(), capabilities=registry, memory=TextMemory(tmp_path), max_rounds=3)

    assert session.run("hello", progress=updates.append) == "finished"

    kinds = [item.kind for item in updates]
    assert kinds[:2] == ["turn_started", "llm_waiting"]
    assert "capability_started" in kinds
    assert "capability_completed" in kinds
    assert kinds[-1] == "turn_completed"
    assert all("private planning" not in item.message for item in updates)


def test_session_cooperatively_cancels_after_model_returns(tmp_path: Path) -> None:
    token = CancellationToken()

    class CancellingLLM:
        def complete(self, messages, *, tools=()):
            token.cancel()
            return {"choices": [{"message": {"content": "must not be committed"}}]}

    session = ConversationSession(
        str(uuid4()),
        llm=CancellingLLM(),
        capabilities=CapabilityRegistry(),
        memory=TextMemory(tmp_path),
    )
    updates = []

    with pytest.raises(SessionCancelled, match="interrupted"):
        session.run("cancel me", progress=updates.append, cancellation=token)

    assert [message.role for message in session.history] == ["system", "user"]
    assert updates[-1].kind == "turn_cancelled"


def _temperature_event(source: str = "/event.py") -> EventDescriptor:
    return EventDescriptor(
        "monitor_temperature",
        "Monitor a temperature sensor with a condition chosen at wait time.",
        "temperature",
        (">", ">=", "<", "<=", "==", "!="),
        templates={">": "The sensor temperature reached {condition}; current value is {value}."},
        timeout_template="Temperature condition {condition} did not occur within {timeout:g} seconds.",
        error_template="Temperature monitoring for {condition} failed: {error}.",
        source=source,
    )


def test_event_pool_supports_dynamic_conditions_and_fifo() -> None:
    pool = EventPool()
    pool.register(_temperature_event())
    first = pool.create_wait("monitor_temperature", "session-a", ">", 40, 10)
    second = pool.create_wait("monitor_temperature", "session-b", ">", 50, 10)

    notified = pool.report_source("/event.py", {"temperature": 45})

    assert [item.wait_id for item in notified] == [first.wait_id]
    assert notified[0].status == "matched"
    assert "temperature > 40" in notified[0].message
    assert "current value is 45" in notified[0].message
    assert pool.take_completion(second.wait_id) is None


def test_event_pool_injects_timeout_and_error_messages() -> None:
    now = [0.0]
    pool = EventPool(clock=lambda: now[0])
    pool.register(_temperature_event())
    timed = pool.create_wait("monitor_temperature", "session", "<=", 5, 3)
    now[0] = 3.0
    timeout = pool.expire()[0]
    assert timeout.wait_id == timed.wait_id
    assert timeout.status == "timeout"
    assert "within 3 seconds" in timeout.message

    failed = pool.create_wait("monitor_temperature", "session", ">", 40, 5)
    error = pool.fail_source("/event.py", "sensor offline")[0]
    assert error.wait_id == failed.wait_id
    assert error.status == "error"
    assert "sensor offline" in error.message


def test_isolated_event_runner_discovers_generic_source(tmp_path: Path) -> None:
    event_root = tmp_path / "events"
    event_root.mkdir()
    environment = tmp_path / "event-env"
    environment.mkdir()
    (environment / "pyproject.toml").write_text(
        '[project]\nname="test-event-env"\nversion="0.1.0"\nrequires-python=">=3.11"\ndependencies=[]\n',
        encoding="utf-8",
    )
    script = event_root / "sample.py"
    script.write_text(
        "import json,sys\n"
        "D={'name':'monitor_temperature','description':'temperature','field':'temperature','operators':['>','<']}\n"
        "print(json.dumps([D] if '-l' in sys.argv else {'temperature':42}))\n",
        encoding="utf-8",
    )
    runner = EventScriptRunner(environment)
    events = load_event_scripts_isolated(event_root, runner)
    assert events[0].name == "monitor_temperature"
    assert events[0].operators == (">", "<")


def test_event_runner_rejects_missing_source(tmp_path: Path) -> None:
    runner = EventScriptRunner(tmp_path / "environment", workspace=tmp_path)
    try:
        runner.poll(tmp_path / "missing.py")
    except EventError as exc:
        assert "does not exist" in str(exc)
    else:
        raise AssertionError("missing event source was accepted")


class ImmediateSupervisor:
    def __init__(self, pool: EventPool) -> None:
        self.pool = pool

    def poll_once(self):
        return self.pool.report_source("/event.py", {"temperature": 42})


class EventLLM:
    def __init__(self) -> None:
        self.calls = 0
        self.wake_message = ""

    def complete(self, messages, *, tools=()):
        self.calls += 1
        if self.calls == 1:
            assert any(tool["function"]["name"] == EVENT_WAIT_TOOL for tool in tools)
            return {
                "choices": [{"message": {"tool_calls": [{"id": "wait-1", "function": {"name": EVENT_WAIT_TOOL, "arguments": '{"event":"monitor_temperature","operator":">","expected":40,"timeout":5}'}}]}}]
            }
        tool_message = messages[-1]
        self.wake_message = json.loads(tool_message.content)["message"]
        return {"choices": [{"message": {"content": "Temperature reached the requested threshold."}}]}


def test_event_completion_is_injected_and_resumes_llm() -> None:
    pool = EventPool()
    pool.register(_temperature_event())
    service = EventService(pool, ImmediateSupervisor(pool), poll_interval=0.01, sleeper=lambda _: None)  # type: ignore[arg-type]
    llm = EventLLM()
    session = ConversationSession(str(uuid4()), llm=llm, capabilities=CapabilityRegistry(), events=pool, event_service=service)

    response = session.run("Wait until the temperature is above 40")

    assert response == "Temperature reached the requested threshold."
    assert "current value is 42" in llm.wake_message


def test_event_service_returns_timeout_notification() -> None:
    now = [0.0]
    pool = EventPool(clock=lambda: now[0])
    pool.register(_temperature_event())

    class NonMatchingSupervisor:
        def poll_once(self):
            pool.report_source("/event.py", {"temperature": 20})
            return pool.expire()

    service = EventService(
        pool,
        NonMatchingSupervisor(),  # type: ignore[arg-type]
        poll_interval=0.5,
        sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds),
    )
    notification = service.wait(
        session_id="session",
        event="monitor_temperature",
        operator=">",
        expected=40,
        timeout=1,
    )
    assert notification.status == "timeout"
    assert "temperature > 40" in notification.message


def test_event_service_cancels_pending_wait_when_interrupted() -> None:
    pool = EventPool()
    pool.register(_temperature_event())
    cancelled = [False]

    class IdleSupervisor:
        def poll_once(self):
            return ()

    def sleeper(_: float) -> None:
        cancelled[0] = True

    service = EventService(pool, IdleSupervisor(), poll_interval=0.01, sleeper=sleeper)  # type: ignore[arg-type]

    with pytest.raises(InterruptedError, match="interrupted"):
        service.wait(
            session_id="session",
            event="monitor_temperature",
            operator=">",
            expected=40,
            timeout=1,
            is_cancelled=lambda: cancelled[0],
        )

    assert pool.pending_sources() == ()
