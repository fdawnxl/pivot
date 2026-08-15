from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from pivot.activation import ActivationState, AgentCancelled, CancellationToken, PersistentAgent
from pivot.capabilities import CapabilityRegistry
from pivot.events import (
    EVENT_WAIT_TOOL,
    EventDescriptor,
    EventError,
    EventPool,
    EventScriptRunner,
    EventService,
    EventSupervisor,
)
from pivot.memory import MemoryStore
from pivot.models import CapabilityDescriptor


class ToolLLM:
    def complete(self, messages, *, tools=()):
        tool_messages = [message for message in messages if message.role == "tool"]
        if not tool_messages:
            return {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {"id": "echo-1", "function": {"name": "echo", "arguments": '{"value":"measured"}'}}
                            ]
                        }
                    }
                ]
            }
        return {"choices": [{"message": {"content": "finished"}}]}


def test_activation_executes_tools_and_persists_in_sqlite(tmp_path: Path) -> None:
    registry = CapabilityRegistry()
    registry.register(CapabilityDescriptor("echo", "work", "Echo", {"type": "object"}), lambda value: value)
    memory = MemoryStore(tmp_path / "memory")
    agent_id = memory.main_agent_id()
    agent = PersistentAgent(agent_id, llm=ToolLLM(), capabilities=registry, memory=memory, max_rounds=3)
    updates = []
    assert agent.activate("hello", progress=updates.append) == "finished"
    assert [message.role for message in agent.history] == ["user", "assistant", "tool", "assistant"]
    kinds = [update.kind for update in updates]
    assert kinds[0] == "activation_started"
    assert "capability_completed" in kinds
    assert kinds[-1] == "activation_completed"
    assert all(update.agent_id == agent.agent_id and update.activation_id for update in updates)

    restored = PersistentAgent(agent_id, llm=ToolLLM(), capabilities=registry, memory=memory, max_rounds=3)
    assert restored.history == agent.history
    memory.close()


def test_activation_preserves_multimodal_content(tmp_path: Path) -> None:
    content = [
        {"type": "text", "text": "Captured from the camera."},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AA=="}},
    ]

    class MultimodalLLM:
        def complete(self, messages, *, tools=()):
            return {"choices": [{"message": {"content": content}}]}

    memory = MemoryStore(tmp_path / "memory")
    agent = PersistentAgent(
        memory.main_agent_id(),
        llm=MultimodalLLM(),
        capabilities=CapabilityRegistry(),
        memory=memory,
    )
    assert agent.activate([{"type": "text", "text": "What do you see?"}]) == "Captured from the camera."
    assert agent.history[0].content == ({"type": "text", "text": "What do you see?"},)
    assert agent.history[1].content == tuple(content)
    memory.close()


def test_activation_state_tracks_running_and_ready(tmp_path: Path) -> None:
    started = threading.Event()
    release = threading.Event()

    class BlockingLLM:
        def complete(self, messages, *, tools=()):
            started.set()
            release.wait(timeout=2)
            return {"choices": [{"message": {"content": "done"}}]}

    memory = MemoryStore(tmp_path / "memory")
    agent = PersistentAgent(memory.main_agent_id(), llm=BlockingLLM(), capabilities=CapabilityRegistry(), memory=memory)
    thread = threading.Thread(target=agent.activate, args=("hello",))
    thread.start()
    assert started.wait(timeout=1)
    assert agent.state == ActivationState.RUNNING
    release.set()
    thread.join(timeout=2)
    assert agent.state == ActivationState.READY
    memory.close()


def test_cancelled_activation_is_excluded_from_later_context(tmp_path: Path) -> None:
    token = CancellationToken()

    class CancellingLLM:
        def complete(self, messages, *, tools=()):
            token.cancel()
            return {"choices": [{"message": {"content": "late"}}]}

    memory = MemoryStore(tmp_path / "memory")
    agent = PersistentAgent(memory.main_agent_id(), llm=CancellingLLM(), capabilities=CapabilityRegistry(), memory=memory)
    with pytest.raises(AgentCancelled):
        agent.activate("cancel me", cancellation=token)
    assert [message.role for message in agent.history] == ["user"]
    memory.close()


def _temperature_event(source: str = "/event.py") -> EventDescriptor:
    return EventDescriptor(
        "monitor_temperature",
        "Temperature source",
        "temperature",
        (">", "<=", "=="),
        {">": "Temperature condition {condition} matched; current value is {value}."},
        source=source,
    )


def test_event_pool_supports_dynamic_conditions_fifo_timeout_and_error() -> None:
    now = [0.0]
    pool = EventPool(clock=lambda: now[0])
    pool.register(_temperature_event())
    first = pool.create_wait("monitor_temperature", "agent-a", ">", 40, 10)
    second = pool.create_wait("monitor_temperature", "agent-b", ">", 50, 10)
    notifications = pool.report_source("/event.py", {"temperature": 45})
    assert [item.wait_id for item in notifications] == [first.wait_id]
    assert pool.take_completion(first.wait_id).status == "matched"  # type: ignore[union-attr]
    assert pool.take_completion(second.wait_id) is None

    timed = pool.create_wait("monitor_temperature", "agent", "<=", 5, 3)
    now[0] = 4
    assert pool.expire()[0].wait_id == timed.wait_id
    assert pool.take_completion(timed.wait_id).status == "timeout"  # type: ignore[union-attr]

    failed = pool.create_wait("monitor_temperature", "agent", ">", 40, 5)
    pool.fail_source("/event.py", "sensor offline")
    assert pool.take_completion(failed.wait_id).status == "error"  # type: ignore[union-attr]


def test_isolated_event_runner_discovers_and_polls_source(tmp_path: Path) -> None:
    environment = tmp_path / "environment"
    environment.mkdir()
    (environment / "pyproject.toml").write_text(
        '[project]\nname="event-test"\nversion="0.1.0"\nrequires-python=">=3.11"\ndependencies=[]\n',
        encoding="utf-8",
    )
    script = tmp_path / "event.py"
    script.write_text(
        "import json,sys\n"
        "D={'name':'temperature','description':'Temperature','field':'temperature','operators':['>']}\n"
        "print(json.dumps([D] if '-l' in sys.argv else {'temperature':42}))\n",
        encoding="utf-8",
    )
    runner = EventScriptRunner(environment, instance=tmp_path, timeout=5)
    descriptors = runner.list_events(script)
    assert descriptors[0].name == "temperature"
    assert runner.poll(script) == {"temperature": 42}
    with pytest.raises(EventError, match="does not exist"):
        EventScriptRunner(environment, instance=tmp_path).poll(tmp_path / "missing.py")


class EventLLM:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, messages, *, tools=()):
        self.calls += 1
        if self.calls == 1:
            assert any(tool["function"]["name"] == EVENT_WAIT_TOOL for tool in tools)
            return {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "wait-1",
                                    "function": {
                                        "name": EVENT_WAIT_TOOL,
                                        "arguments": '{"event":"monitor_temperature","operator":">","expected":40,"timeout":5}',
                                    },
                                }
                            ]
                        }
                    }
                ]
            }
        return {"choices": [{"message": {"content": "Temperature reached the threshold."}}]}


def test_worker_event_wait_is_pending_then_resumes(tmp_path: Path) -> None:
    pool = EventPool()
    pool.register(_temperature_event())
    waiting = threading.Event()
    release = threading.Event()

    class BlockingSupervisor:
        def poll_once(self):
            waiting.set()
            release.wait(timeout=2)
            return pool.report_source("/event.py", {"temperature": 42})

    service = EventService(pool, BlockingSupervisor(), poll_interval=0.01)  # type: ignore[arg-type]
    memory = MemoryStore(tmp_path / "memory")
    worker_id = memory.create_worker(name="waiter", parent_id=memory.main_agent_id(), capabilities=(), events=("monitor_temperature",))
    agent = PersistentAgent(
        worker_id,
        llm=EventLLM(),
        capabilities=CapabilityRegistry(),
        memory=memory,
        events=pool,
        event_service=service,
        event_names=("monitor_temperature",),
        role="worker",
        name="waiter",
    )
    result: list[str] = []
    thread = threading.Thread(
        target=lambda: result.append(agent.activate("wait", source="delegation")),
    )
    thread.start()
    assert waiting.wait(timeout=1)
    assert agent.state == ActivationState.PENDING
    release.set()
    thread.join(timeout=2)
    assert agent.state == ActivationState.READY
    assert result == ["Temperature reached the threshold."]
    memory.close()


def test_event_service_timeout_and_interruption() -> None:
    now = [0.0]
    pool = EventPool(clock=lambda: now[0])
    pool.register(_temperature_event())

    class IdleSupervisor:
        def poll_once(self):
            pool.report_source("/event.py", {"temperature": 20})
            return pool.expire()

    service = EventService(
        pool,
        IdleSupervisor(),  # type: ignore[arg-type]
        poll_interval=0.5,
        sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds),
    )
    notification = service.wait(
        agent_id="agent",
        event="monitor_temperature",
        operator=">",
        expected=40,
        timeout=1,
    )
    assert notification.status == "timeout"

    cancelled = [False]
    service = EventService(
        pool,
        IdleSupervisor(),  # type: ignore[arg-type]
        poll_interval=0.01,
        sleeper=lambda _: cancelled.__setitem__(0, True),
    )
    with pytest.raises(InterruptedError):
        service.wait(
            agent_id="agent",
            event="monitor_temperature",
            operator=">",
            expected=40,
            timeout=1,
            is_cancelled=lambda: cancelled[0],
        )
