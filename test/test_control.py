from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from pivot.capabilities import CapabilityRegistry
from pivot.config import PivotConfig
from pivot.control import ControlTaskState, PivotControl
from pivot.credentials import ProviderCredential
from pivot.events import EventPool, EventService, EventSupervisor
from pivot.memory import TextMemory
from pivot.runtime import PivotClient, Runtime
from pivot.session import SessionCancelled, SessionManager


class EchoLLM:
    def complete(self, messages, *, tools=()):
        user = next(message.content for message in reversed(messages) if message.role == "user")
        return {"choices": [{"message": {"content": f"ack: {user}"}}]}


def _runtime(tmp_path: Path, llm: Any | None = None) -> Runtime:
    config = PivotConfig(tmp_path, ProviderCredential("test", "test-model"))
    registry = CapabilityRegistry()
    events = EventPool()
    event_service = EventService(events, EventSupervisor(events, None))
    sessions = SessionManager(
        llm=llm or EchoLLM(),
        capabilities=registry,
        memory=TextMemory(tmp_path / "memory"),
        events=events,
        event_service=event_service,
    )
    return Runtime(config, registry, events, event_service, sessions)


def test_control_manages_sessions_tasks_and_history(tmp_path: Path) -> None:
    control = PivotControl(_runtime(tmp_path))
    events: list[tuple[str, str]] = []
    control.subscribe(lambda event, payload: events.append((event, str(payload.get("state", "")))))

    session = control.create_session()
    assert control.selected_session_id == session.session_id
    assert control.list_sessions()[0]["selected"]

    task_id = control.submit_message("hello")
    task = control.wait_task(task_id, timeout=2)
    assert task.state == ControlTaskState.COMPLETED
    assert task.result == {"session_id": session.session_id, "response": "ack: hello"}
    assert [message["role"] for message in control.history()] == ["system", "user", "assistant"]
    assert any(event == "task_changed" and state == "completed" for event, state in events)
    task_states = [state for event, state in events if event == "task_changed"]
    assert task_states[0] == "queued"
    assert task_states[-1] == "completed"
    control.close()


def test_control_discovers_persisted_sessions(tmp_path: Path) -> None:
    first = PivotControl(_runtime(tmp_path))
    session = first.create_session()
    first.wait_task(first.submit_message("persist me"), timeout=2)
    first.close()

    second = PivotControl(_runtime(tmp_path))
    sessions = second.list_sessions()
    assert [item["session_id"] for item in sessions] == [session.session_id]
    assert second.history(session.session_id)[-1]["content"] == "ack: persist me"
    second.close()


def test_control_cancels_local_client_turn(tmp_path: Path) -> None:
    started = threading.Event()
    release = threading.Event()

    class SlowLLM:
        def complete(self, messages, *, tools=()):
            started.set()
            release.wait(timeout=2)
            return {"choices": [{"message": {"content": "late"}}]}

    client = PivotClient(_runtime(tmp_path, SlowLLM()))
    session = client.create_session()
    error: list[BaseException] = []

    def run() -> None:
        try:
            client.run(session.session_id, "slow")
        except BaseException as exc:
            error.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    assert started.wait(timeout=1)
    assert client.control.cancel_session(session.session_id)
    release.set()
    thread.join(timeout=2)
    assert len(error) == 1 and isinstance(error[0], SessionCancelled)
    client.close()


def test_main_agent_requests_run_in_fifo_order(tmp_path: Path) -> None:
    first_started = threading.Event()
    release_first = threading.Event()
    received: list[str] = []

    class OrderedLLM:
        def complete(self, messages, *, tools=()):
            user = next(message.content for message in reversed(messages) if message.role == "user")
            assert isinstance(user, str)
            received.append(user)
            if user == "first":
                first_started.set()
                release_first.wait(timeout=2)
            return {"choices": [{"message": {"content": f"ack: {user}"}}]}

    control = PivotControl(_runtime(tmp_path, OrderedLLM()))
    control.create_session()
    first = control.submit_message("first")
    assert first_started.wait(timeout=1)
    second = control.submit_message("second")
    third = control.submit_message("third")
    time.sleep(0.05)
    assert control.task(second).state == ControlTaskState.QUEUED
    assert control.task(third).state == ControlTaskState.QUEUED

    release_first.set()
    assert control.wait_task(first, timeout=2).state == ControlTaskState.COMPLETED
    assert control.wait_task(second, timeout=2).result["response"] == "ack: second"
    assert control.wait_task(third, timeout=2).result["response"] == "ack: third"
    assert received == ["first", "second", "third"]
    control.close()


def test_cancelling_queued_request_does_not_block_later_requests(tmp_path: Path) -> None:
    first_started = threading.Event()
    release_first = threading.Event()
    received: list[str] = []

    class OrderedLLM:
        def complete(self, messages, *, tools=()):
            user = next(message.content for message in reversed(messages) if message.role == "user")
            assert isinstance(user, str)
            received.append(user)
            if user == "first":
                first_started.set()
                release_first.wait(timeout=2)
            return {"choices": [{"message": {"content": f"ack: {user}"}}]}

    control = PivotControl(_runtime(tmp_path, OrderedLLM()))
    control.create_session()
    first = control.submit_message("first")
    assert first_started.wait(timeout=1)
    cancelled = control.submit_message("cancelled")
    last = control.submit_message("last")
    assert control.cancel_task(cancelled)
    release_first.set()

    assert control.wait_task(first, timeout=2).state == ControlTaskState.COMPLETED
    assert control.wait_task(cancelled, timeout=2).state == ControlTaskState.CANCELLED
    assert control.wait_task(last, timeout=2).state == ControlTaskState.COMPLETED
    assert received == ["first", "last"]
    control.close()
