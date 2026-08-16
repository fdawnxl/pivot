from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from pivot.activation import AgentCancelled, PersistentAgent
from pivot.agents import AgentControl
from pivot.capabilities import CapabilityRegistry
from pivot.config import PivotConfig
from pivot.control import PivotControl
from pivot.credentials import ProviderCredential
from pivot.events import EventBridgeRule, EventPool, EventService, EventStimulusBridge, EventSupervisor
from pivot.executors import ExecutorRegistry, ShellExecutor
from pivot.memory import RuntimeStore
from pivot.models import EventDescriptor
from pivot.runtime import PivotClient, Runtime
from pivot.stimuli import StimulusState


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
    memory = RuntimeStore(tmp_path / "memory")
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


def test_control_injects_durable_envelopes_and_emits_outputs(tmp_path: Path) -> None:
    control = PivotControl(_runtime(tmp_path))
    events: list[tuple[str, str]] = []
    control.subscribe(lambda event, payload: events.append((event, str(payload.get("state", "")))))

    stimulus_id = control.inject(
        {
            "kind": "command",
            "source": "test-adapter",
            "payload": {"content": "hello"},
            "correlation_id": "request-1",
        }
    )
    stimulus = control.wait_stimulus(stimulus_id, timeout=2)
    assert stimulus.state == StimulusState.COMPLETED
    assert stimulus.response == "ack: hello"
    assert stimulus.activation_id is not None
    assert [message.role for message in control.runtime._main_agent.history] == ["user", "assistant"]
    output = control.outputs()[0]
    assert output.stimulus_id == stimulus_id
    assert output.payload["content"] == "ack: hello"
    assert any(event == "stimulus_changed" and state == "completed" for event, state in events)
    assert any(event == "output_available" for event, _state in events)
    control.close()
    control.runtime.close()


def test_event_bridge_persists_occurrence_and_routes_through_main_inbox(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    runtime.events.register(
        EventDescriptor(
            "monitor_temperature",
            "Temperature source",
            "temperature",
            (">",),
            source="/event.py",
        )
    )
    control = PivotControl(runtime)
    assert runtime.reactor is not None
    bridge = EventStimulusBridge(
        (EventBridgeRule("temperature-alert", "monitor_temperature", ">", 40, "state", 80, True),),
        supervisor=runtime.event_service.supervisor,
        store=runtime.memory,
        publish=runtime.reactor.inject_event_occurrence,
    )
    try:
        bridge.observe("/event.py", {"temperature": 42, "unit": "celsius"})
        stimulus = control.stimuli(limit=1)[0]
        completed = control.wait_stimulus(stimulus.stimulus_id, timeout=2)
        occurrences = runtime.memory.event_occurrences()

        assert completed.state == StimulusState.COMPLETED
        assert completed.source == "event-bridge:temperature-alert"
        assert completed.causation_id == occurrences[0]["occurrence_id"]
        assert occurrences[0]["stimulus_id"] == completed.stimulus_id
        assert runtime.memory.current_world_state()[0]["value"] == 42
    finally:
        control.close()
        runtime.close()


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


def test_main_agent_stimuli_preserve_fifo_and_skip_cancelled_items(tmp_path: Path) -> None:
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
    first = control.inject_command("first")
    assert first_started.wait(timeout=1)
    second = control.inject_command("second")
    cancelled = control.inject_command("cancelled")
    third = control.inject_command("third")
    time.sleep(0.05)
    assert control.stimulus(second).state == StimulusState.QUEUED
    assert control.stimulus(cancelled).state == StimulusState.QUEUED
    assert control.stimulus(third).state == StimulusState.QUEUED
    assert control.cancel_stimulus(cancelled)
    release_first.set()
    assert control.wait_stimulus(first, timeout=2).state == StimulusState.COMPLETED
    assert control.wait_stimulus(second, timeout=2).response == "ack: second"
    assert control.wait_stimulus(cancelled, timeout=2).state == StimulusState.CANCELLED
    assert control.wait_stimulus(third, timeout=2).response == "ack: third"
    assert received == ["first", "second", "third"]
    control.close()
    runtime.close()


def test_observation_uses_the_same_main_agent_reactor(tmp_path: Path) -> None:
    control = PivotControl(_runtime(tmp_path))
    stimulus_id = control.inject(
        {
            "kind": "observation",
            "delivery": "activate",
            "source": "instance.temperature-monitor",
            "payload": {
                "event": "temperature.high",
                "value": 83.5,
                "threshold": 80,
            },
            "priority": 80,
            "dedupe_key": "temperature.high:started",
        }
    )
    completed = control.wait_stimulus(stimulus_id, timeout=2)
    assert completed.state == StimulusState.COMPLETED
    assert '"kind":"observation"' in (completed.response or "")
    assert control.runtime._main_agent.history[0].role == "system"
    control.close()
    control.runtime.close()


def test_observation_defaults_to_world_state_without_llm_activation(tmp_path: Path) -> None:
    control = PivotControl(_runtime(tmp_path))
    stimulus_id = control.inject(
        {
            "kind": "observation",
            "source": "instance.temperature-sensor",
            "payload": {"values": {"temperature": 23.5}, "ttl": 30},
        }
    )

    completed = control.wait_stimulus(stimulus_id, timeout=2)
    assert completed.state == StimulusState.COMPLETED
    assert completed.activation_id is None
    assert control.runtime.memory.current_world_state()[0]["value"] == 23.5
    assert control.runtime._main_agent.history == ()
    output = control.outputs()[0]
    assert output.kind == "state_updated"
    assert output.sequence == 1
    control.close()
    control.runtime.close()
