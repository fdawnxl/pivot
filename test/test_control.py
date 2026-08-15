from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from pivot.activation import AgentCancelled, PersistentAgent
from pivot.agents import AgentControl
from pivot.capabilities import CapabilityRegistry
from pivot.config import PivotConfig
from pivot.control import ControlTaskState, PivotControl
from pivot.credentials import ProviderCredential
from pivot.events import EventPool, EventService, EventSupervisor
from pivot.executors import ExecutorRegistry, ShellExecutor
from pivot.memory import MemoryStore
from pivot.runtime import PivotClient, Runtime


class EchoLLM:
    def complete(self, messages, *, tools=()):
        stimulus = next(message.content for message in reversed(messages) if message.role in {"user", "system"})
        return {"choices": [{"message": {"content": f"ack: {stimulus}"}}]}


def _runtime(tmp_path: Path, llm: Any | None = None) -> Runtime:
    config = PivotConfig(tmp_path, ProviderCredential("test", "test-model"))
    registry = CapabilityRegistry()
    events = EventPool()
    event_service = EventService(events, EventSupervisor(events, None))
    executors = ExecutorRegistry()
    executors.register(ShellExecutor(tmp_path, timeout=2))
    memory = MemoryStore(tmp_path / "memory")
    model = llm or EchoLLM()
    main = PersistentAgent(
        memory.main_agent_id(),
        llm=model,
        capabilities=registry,
        memory=memory,
        events=events,
        event_service=event_service,
        executors=executors,
    )
    agents = AgentControl(
        main,
        llm=model,
        capabilities=registry,
        memory=memory,
        events=events,
        event_service=event_service,
        executors=executors,
        max_rounds=4,
    )
    return Runtime(config, registry, events, event_service, memory, main, None, executors, agents)


def test_control_runs_tasks_and_reads_durable_history(tmp_path: Path) -> None:
    control = PivotControl(_runtime(tmp_path))
    events: list[tuple[str, str]] = []
    control.subscribe(lambda event, payload: events.append((event, str(payload.get("state", "")))))

    task_id = control.submit_message("hello")
    task = control.wait_task(task_id, timeout=2)
    assert task.state == ControlTaskState.COMPLETED
    assert task.result == {"agent_id": control.runtime.main_agent.agent_id, "response": "ack: hello"}
    assert [message["role"] for message in control.history()] == ["user", "assistant"]
    assert any(event == "task_changed" and state == "completed" for event, state in events)
    control.close()
    control.runtime.close()


def test_control_interrupts_local_main_activation(tmp_path: Path) -> None:
    started = threading.Event()
    release = threading.Event()

    class SlowLLM:
        def complete(self, messages, *, tools=()):
            started.set()
            release.wait(timeout=2)
            return {"choices": [{"message": {"content": "late"}}]}

    client = PivotClient(_runtime(tmp_path, SlowLLM()))
    error: list[BaseException] = []

    def run() -> None:
        try:
            client.run_main("slow")
        except BaseException as exc:
            error.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    assert started.wait(timeout=1)
    assert client.control.interrupt_main()
    release.set()
    thread.join(timeout=2)
    assert len(error) == 1 and isinstance(error[0], AgentCancelled)
    client.close()


def test_main_agent_requests_run_in_fifo_order(tmp_path: Path) -> None:
    first_started = threading.Event()
    release_first = threading.Event()
    received: list[str] = []

    class OrderedLLM:
        def complete(self, messages, *, tools=()):
            value = next(message.content for message in reversed(messages) if message.role == "user")
            assert isinstance(value, str)
            received.append(value)
            if value == "first":
                first_started.set()
                release_first.wait(timeout=2)
            return {"choices": [{"message": {"content": f"ack: {value}"}}]}

    runtime = _runtime(tmp_path, OrderedLLM())
    control = PivotControl(runtime)
    first = control.submit_message("first")
    assert first_started.wait(timeout=1)
    second = control.submit_message("second")
    cancelled = control.submit_message("cancelled")
    third = control.submit_message("third")
    time.sleep(0.05)
    assert control.task(second).state == ControlTaskState.QUEUED
    assert control.task(cancelled).state == ControlTaskState.QUEUED
    assert control.task(third).state == ControlTaskState.QUEUED
    assert control.cancel_task(cancelled)
    release_first.set()
    assert control.wait_task(first, timeout=2).state == ControlTaskState.COMPLETED
    assert control.wait_task(second, timeout=2).result["response"] == "ack: second"
    assert control.wait_task(cancelled, timeout=2).state == ControlTaskState.CANCELLED
    assert control.wait_task(third, timeout=2).result["response"] == "ack: third"
    assert received == ["first", "second", "third"]
    control.close()
    runtime.close()
