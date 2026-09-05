from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from pivot.actions import ACTION_TOOL
from pivot.activation import (
    ActivationState,
    AgentActivationError,
    AgentCancelled,
    CancellationToken,
    PersistentAgent,
)
from pivot.capabilities import GLOBAL_POLICY_THINK_NAME, CapabilityRegistry
from pivot.events import (
    EVENT_WAIT_TOOL,
    EventBridgeRule,
    EventDescriptor,
    EventError,
    EventPool,
    EventScriptRunner,
    EventService,
    EventStimulusBridge,
    EventSupervisor,
    load_event_bridge_rules,
)
from pivot.memory import RuntimeStore
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
                                {
                                    "id": "echo-1",
                                    "function": {
                                        "name": "echo",
                                        "arguments": '{"value":"measured"}',
                                    },
                                }
                            ]
                        }
                    }
                ]
            }
        return {"choices": [{"message": {"content": "finished"}}]}


def test_activation_executes_tools_and_persists_in_sqlite(tmp_path: Path) -> None:
    registry = CapabilityRegistry()
    registry.register(
        CapabilityDescriptor("echo", "work", "Echo", {"type": "object"}),
        lambda value: value,
    )
    memory = RuntimeStore(tmp_path / "memory")
    agent_id = memory.main_agent_id()
    agent = PersistentAgent(
        agent_id, llm=ToolLLM(), capabilities=registry, memory=memory, max_rounds=3
    )
    updates = []
    assert agent._activate("hello", progress=updates.append) == "finished"
    assert [message.role for message in agent.history] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    kinds = [update.kind for update in updates]
    assert kinds[0] == "activation_started"
    assert "capability_completed" in kinds
    assert kinds[-1] == "activation_completed"
    assert all(
        update.agent_id == agent.agent_id and update.activation_id for update in updates
    )

    restored = PersistentAgent(
        agent_id, llm=ToolLLM(), capabilities=registry, memory=memory, max_rounds=3
    )
    assert restored.history == agent.history
    memory.close()


def test_global_policy_is_injected_for_main_agent_only(tmp_path: Path) -> None:
    registry = CapabilityRegistry()
    reads = []
    registry.register_think(
        CapabilityDescriptor(
            GLOBAL_POLICY_THINK_NAME,
            "think",
            "Instance-wide policy",
        ),
        lambda: reads.append("read") or "Always protect the operator.",
    )

    class CapturingLLM:
        def __init__(self) -> None:
            self.messages = []

        def complete(self, messages, *, tools=()):
            self.messages.append(tuple(messages))
            return {"choices": [{"message": {"content": "done"}}]}

    memory = RuntimeStore(tmp_path / "memory")
    main_llm = CapturingLLM()
    main = PersistentAgent(
        memory.main_agent_id(), llm=main_llm, capabilities=registry, memory=memory
    )
    assert main._activate("hello") == "done"
    assert reads == ["read"]
    assert [message.content for message in main_llm.messages[0][:2]] == [
        "Always protect the operator.",
        next(
            message.content
            for message in main_llm.messages[0]
            if message.content.startswith("pivot runtime context:")
        ),
    ]

    worker_llm = CapturingLLM()
    worker_id = memory.create_worker(
        name="worker", parent_id=main.agent_id, capabilities=(), events=()
    )
    worker = PersistentAgent(
        worker_id,
        llm=worker_llm,
        capabilities=registry,
        memory=memory,
        role="worker",
        name="worker",
    )
    assert worker._activate("hello", source="delegation") == "done"
    assert reads == ["read"]
    assert all(
        message.content != "Always protect the operator."
        for message in worker_llm.messages[0]
    )
    memory.close()


def test_activation_preserves_multimodal_content(tmp_path: Path) -> None:
    content = [
        {"type": "text", "text": "Captured from the camera."},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AA=="}},
    ]

    class MultimodalLLM:
        def complete(self, messages, *, tools=()):
            return {"choices": [{"message": {"content": content}}]}

    memory = RuntimeStore(tmp_path / "memory")
    agent = PersistentAgent(
        memory.main_agent_id(),
        llm=MultimodalLLM(),
        capabilities=CapabilityRegistry(),
        memory=memory,
    )
    assert (
        agent._activate([{"type": "text", "text": "What do you see?"}])
        == "Captured from the camera."
    )
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

    memory = RuntimeStore(tmp_path / "memory")
    agent = PersistentAgent(
        memory.main_agent_id(),
        llm=BlockingLLM(),
        capabilities=CapabilityRegistry(),
        memory=memory,
    )
    thread = threading.Thread(target=agent._activate, args=("hello",))
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

    memory = RuntimeStore(tmp_path / "memory")
    agent = PersistentAgent(
        memory.main_agent_id(),
        llm=CancellingLLM(),
        capabilities=CapabilityRegistry(),
        memory=memory,
    )
    with pytest.raises(AgentCancelled):
        agent._activate("cancel me", cancellation=token)
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


def test_rising_event_wait_requires_a_false_to_true_transition_each_cycle() -> None:
    pool = EventPool()
    pool.register(_temperature_event())
    first = pool.create_wait(
        "monitor_temperature",
        "agent",
        ">",
        30,
        10,
        trigger="rising",
    )

    assert pool.report_source("/event.py", {"temperature": 31}) == ()
    assert pool.report_source("/event.py", {"temperature": 32}) == ()
    assert pool.report_source("/event.py", {"temperature": 29}) == ()
    matched = pool.report_source("/event.py", {"temperature": 31})
    assert [item.wait_id for item in matched] == [first.wait_id]
    assert matched[0].as_dict()["trigger"] == "rising"
    assert matched[0].as_dict()["transition"] == "false_to_true"

    second = pool.create_wait(
        "monitor_temperature",
        "agent",
        ">",
        30,
        10,
        trigger="rising",
    )
    assert pool.report_source("/event.py", {"temperature": 33}) == ()
    assert pool.report_source("/event.py", {"temperature": 28}) == ()
    assert [
        item.wait_id for item in pool.report_source("/event.py", {"temperature": 30.1})
    ] == [second.wait_id]


def test_falling_event_wait_requires_a_true_to_false_transition_each_cycle() -> None:
    pool = EventPool()
    pool.register(_temperature_event())
    first = pool.create_wait(
        "monitor_temperature",
        "agent",
        ">",
        30,
        10,
        trigger="falling",
    )

    assert pool.report_source("/event.py", {"temperature": 31}) == ()
    assert pool.report_source("/event.py", {"temperature": 32}) == ()
    matched = pool.report_source("/event.py", {"temperature": 29})
    assert [item.wait_id for item in matched] == [first.wait_id]
    assert matched[0].as_dict()["trigger"] == "falling"
    assert matched[0].as_dict()["transition"] == "true_to_false"
    assert "changed from true to false" in matched[0].message

    second = pool.create_wait(
        "monitor_temperature",
        "agent",
        ">",
        30,
        10,
        trigger="falling",
    )
    assert pool.report_source("/event.py", {"temperature": 28}) == ()
    assert pool.report_source("/event.py", {"temperature": 31}) == ()
    assert [
        item.wait_id for item in pool.report_source("/event.py", {"temperature": 29})
    ] == [second.wait_id]


def test_event_wait_tool_exposes_level_and_edge_triggers() -> None:
    pool = EventPool()
    pool.register(_temperature_event())
    service = EventService(pool, EventSupervisor(pool, None))

    parameters = service.llm_tools()[0]["function"]["parameters"]

    assert parameters["properties"]["trigger"]["enum"] == ["level", "rising", "falling"]
    assert "trigger" not in parameters["required"]


def test_event_supervisor_shares_source_reports_with_waits_and_subscribers() -> None:
    pool = EventPool()
    pool.register(_temperature_event())
    wait = pool.create_wait("monitor_temperature", "agent", ">", 40, 5)

    class Runner:
        def poll(self, source: str) -> dict[str, object]:
            assert source == "/event.py"
            return {"temperature": 42, "unit": "celsius"}

    supervisor = EventSupervisor(pool, Runner())
    observed: list[dict[str, object]] = []
    unsubscribe = supervisor.subscribe(
        "/event.py", lambda _source, payload: observed.append(dict(payload))
    )
    try:
        notifications = supervisor.poll_once()
    finally:
        unsubscribe()

    assert notifications[0].wait_id == wait.wait_id
    assert observed == [{"temperature": 42, "unit": "celsius"}]


def test_event_supervisor_owns_one_background_poll_loop() -> None:
    pool = EventPool()
    pool.register(_temperature_event())
    first = pool.create_wait("monitor_temperature", "agent-a", ">", 40, 5)
    second = pool.create_wait("monitor_temperature", "agent-b", ">", 40, 5)
    observed = threading.Event()

    class Runner:
        def __init__(self) -> None:
            self.calls = 0

        def poll(self, source: str) -> dict[str, object]:
            assert source == "/event.py"
            self.calls += 1
            return {"temperature": 42}

    runner = Runner()
    supervisor = EventSupervisor(pool, runner, poll_interval=60)
    unsubscribe = supervisor.subscribe("/event.py", lambda *_: observed.set())
    try:
        supervisor.start()
        assert observed.wait(timeout=1)
        assert pool.wait_completion(first.wait_id, timeout=0.1) is not None
        assert pool.wait_completion(second.wait_id, timeout=0.1) is not None
        assert runner.calls == 1
    finally:
        unsubscribe()
        supervisor.close()

    assert not supervisor.running


def test_event_bridge_rules_load_independently_and_validate_event_contract(
    tmp_path: Path,
) -> None:
    pool = EventPool()
    pool.register(_temperature_event())
    rules_path = tmp_path / "bridges.toml"
    rules_path.write_text(
        """
[[bridge]]
id = "temperature-alert"
event = "monitor_temperature"
operator = ">"
expected = 40
delivery = "activate"
priority = 80
cooldown = 30

[[bridge]]
id = "invalid-event"
event = "missing"
operator = ">"
expected = 1
""",
        encoding="utf-8",
    )

    rules = load_event_bridge_rules(rules_path, pool)

    assert len(rules) == 1
    assert rules[0] == EventBridgeRule(
        "temperature-alert",
        "monitor_temperature",
        ">",
        40,
        "activate",
        80,
        False,
        30.0,
    )


def test_event_bridge_uses_durable_rising_edge_state(tmp_path: Path) -> None:
    pool = EventPool()
    pool.register(_temperature_event())
    supervisor = EventSupervisor(pool, None)  # type: ignore[arg-type]
    memory = RuntimeStore(tmp_path / "memory")
    emitted: list[tuple[dict[str, object], dict[str, object]]] = []

    def publish(envelope, occurrence):
        emitted.append((dict(envelope), dict(occurrence)))
        return f"stimulus-{len(emitted)}"

    rule = EventBridgeRule("temperature-alert", "monitor_temperature", ">", 40)
    bridge = EventStimulusBridge(
        (rule,), supervisor=supervisor, store=memory, publish=publish
    )
    bridge.observe("/event.py", {"temperature": 42})
    bridge.observe("/event.py", {"temperature": 43})
    assert len(emitted) == 1
    assert emitted[0][0]["causation_id"] == emitted[0][1]["occurrence_id"]

    restored = EventStimulusBridge(
        (rule,), supervisor=supervisor, store=memory, publish=publish
    )
    restored.observe("/event.py", {"temperature": 44})
    assert len(emitted) == 1

    restored.observe("/event.py", {"temperature": 20})
    restored.observe("/event.py", {"temperature": 45})
    assert len(emitted) == 2
    assert memory.event_bridge_state(rule.bridge_id)["matched"] is True  # type: ignore[index]
    memory.close()


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
        return {
            "choices": [{"message": {"content": "Temperature reached the threshold."}}]
        }


def test_worker_event_wait_is_pending_then_resumes(tmp_path: Path) -> None:
    pool = EventPool()
    pool.register(_temperature_event())
    waiting = threading.Event()
    release = threading.Event()

    class BlockingRunner:
        def poll(self, source: str):
            assert source == "/event.py"
            waiting.set()
            release.wait(timeout=2)
            return {"temperature": 42}

    supervisor = EventSupervisor(pool, BlockingRunner(), poll_interval=0.01)
    service = EventService(pool, supervisor, poll_interval=0.01)
    memory = RuntimeStore(tmp_path / "memory")
    worker_id = memory.create_worker(
        name="waiter",
        parent_id=memory.main_agent_id(),
        capabilities=(),
        events=("monitor_temperature",),
    )
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
        target=lambda: result.append(agent._activate("wait", source="delegation")),
    )
    thread.start()
    assert waiting.wait(timeout=1)
    assert agent.state == ActivationState.PENDING
    release.set()
    thread.join(timeout=2)
    assert agent.state == ActivationState.READY
    assert result == ["Temperature reached the threshold."]
    service.close()
    memory.close()


@pytest.mark.parametrize(
    ("recurrence", "expected_error"),
    [
        ("once", "one-shot event task already matched"),
        ("rising", "Report the matched event occurrence"),
    ],
)
def test_event_policy_requires_reporting_before_another_wait(
    tmp_path: Path,
    recurrence: str,
    expected_error: str,
) -> None:
    class SkippingReportLLM:
        def complete(self, messages, *, tools=()):
            tool_messages = [message for message in messages if message.role == "tool"]
            if len(tool_messages) < 2:
                return {
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "id": f"wait-{len(tool_messages) + 1}",
                                        "function": {
                                            "name": EVENT_WAIT_TOOL,
                                            "arguments": (
                                                '{"event":"monitor_temperature","operator":">",'
                                                '"expected":30,"timeout":2,"trigger":"rising"}'
                                            ),
                                        },
                                    }
                                ]
                            }
                        }
                    ]
                }
            assert expected_error in str(tool_messages[-1].content)
            return {"choices": [{"message": {"content": "Policy enforced."}}]}

    class CrossingRunner:
        def __init__(self) -> None:
            self.calls = 0

        def poll(self, source: str):
            assert source == "/event.py"
            self.calls += 1
            return {"temperature": 20 if self.calls == 1 else 31}

    pool = EventPool()
    pool.register(_temperature_event())
    supervisor = EventSupervisor(pool, CrossingRunner(), poll_interval=0.005)
    service = EventService(pool, supervisor, poll_interval=0.005)
    memory = RuntimeStore(tmp_path / recurrence / "memory")
    worker_id = memory.create_worker(
        name="waiter",
        parent_id=memory.main_agent_id(),
        capabilities=(),
        events=("monitor_temperature",),
    )
    agent = PersistentAgent(
        worker_id,
        llm=SkippingReportLLM(),
        capabilities=CapabilityRegistry(),
        memory=memory,
        events=pool,
        event_service=service,
        event_names=("monitor_temperature",),
        role="worker",
        name="waiter",
        max_rounds=3,
    )

    try:
        with pytest.raises(AgentActivationError, match="maximum LLM rounds"):
            agent._activate("monitor", source="delegation", event_recurrence=recurrence)
    finally:
        service.close()
        memory.close()


@pytest.mark.parametrize(
    ("edge", "observations"),
    [
        ("rising", (20, 31)),
        ("falling", (31, 20)),
    ],
)
def test_recurring_event_report_resets_the_activation_round_budget(
    tmp_path: Path,
    edge: str,
    observations: tuple[int, int],
) -> None:
    reports: list[object] = []

    class ReportingControl:
        def prompt_context(self, _agent):
            return [{"name": "agent.report", "arguments": ["result"]}]

        def execute(self, _agent, operation, arguments, **_kwargs):
            assert operation == "agent.report"
            reports.append(arguments["result"])
            return {"accepted": True}

    class TwoOccurrenceLLM:
        def __init__(self) -> None:
            self.calls = 0

        def complete(self, _messages, *, tools=()):
            self.calls += 1
            if self.calls in {1, 3}:
                call = {
                    "id": f"wait-{self.calls}",
                    "function": {
                        "name": EVENT_WAIT_TOOL,
                        "arguments": (
                            '{"event":"monitor_temperature","operator":">",'
                            f'"expected":30,"timeout":2,"trigger":"{edge}"}}'
                        ),
                    },
                }
            elif self.calls in {2, 4}:
                call = {
                    "id": f"report-{self.calls}",
                    "function": {
                        "name": ACTION_TOOL,
                        "arguments": json.dumps(
                            {
                                "kind": "control",
                                "name": "agent.report",
                                "arguments": {
                                    "result": {"occurrence": self.calls // 2}
                                },
                            }
                        ),
                    },
                }
            else:
                return {
                    "choices": [{"message": {"content": "Two occurrences reported."}}]
                }
            return {"choices": [{"message": {"tool_calls": [call]}}]}

    class CrossingRunner:
        def __init__(self) -> None:
            self.calls = 0

        def poll(self, source: str):
            assert source == "/event.py"
            self.calls += 1
            return {"temperature": observations[(self.calls - 1) % 2]}

    pool = EventPool()
    pool.register(_temperature_event())
    supervisor = EventSupervisor(pool, CrossingRunner(), poll_interval=0.005)
    service = EventService(pool, supervisor, poll_interval=0.005)
    memory = RuntimeStore(tmp_path / edge / "memory")
    worker_id = memory.create_worker(
        name="recurring",
        parent_id=memory.main_agent_id(),
        capabilities=(),
        events=("monitor_temperature",),
    )
    agent = PersistentAgent(
        worker_id,
        llm=TwoOccurrenceLLM(),
        capabilities=CapabilityRegistry(),
        memory=memory,
        events=pool,
        event_service=service,
        event_names=("monitor_temperature",),
        agent_control=ReportingControl(),  # type: ignore[arg-type]
        role="worker",
        name="recurring",
        max_rounds=3,
    )

    try:
        assert (
            agent._activate(
                "monitor",
                source="delegation",
                event_recurrence=edge,
            )
            == "Two occurrences reported."
        )
        assert reports == [{"occurrence": 1}, {"occurrence": 2}]
    finally:
        service.close()
        memory.close()


def test_event_service_timeout_and_interruption() -> None:
    pool = EventPool()
    pool.register(_temperature_event())

    class IdleRunner:
        def poll(self, source: str):
            assert source == "/event.py"
            return {"temperature": 20}

    supervisor = EventSupervisor(pool, IdleRunner(), poll_interval=1)
    service = EventService(
        pool,
        supervisor,
        poll_interval=0.005,
    )
    notification = service.wait(
        agent_id="agent",
        event="monitor_temperature",
        operator=">",
        expected=40,
        timeout=0.02,
    )
    assert notification.status == "timeout"

    with pytest.raises(InterruptedError):
        service.wait(
            agent_id="agent",
            event="monitor_temperature",
            operator=">",
            expected=40,
            timeout=1,
            is_cancelled=lambda: True,
        )
    service.close()
