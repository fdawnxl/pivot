from __future__ import annotations

import json
import threading
import time
from pathlib import Path
import pytest

from pivot.actions import ACTION_TOOL, ActionDetector, ActionKind
from pivot.activation import PersistentAgent
from pivot.agents import AgentControl, AgentControlError
from pivot.capabilities import CapabilityRegistry
from pivot.config import PivotConfig
from pivot.credentials import ProviderCredential
from pivot.events import EventDescriptor, EventPool, EventService, EventSupervisor
from pivot.executors import ExecutorError, ExecutorRegistry, ShellExecutor
from pivot.memory import MemoryStore
from pivot.models import CapabilityDescriptor, ParsedResponse, ToolCall
from pivot.runtime import PivotClient, Runtime


def test_action_detector_normalizes_fixed_text_protocol() -> None:
    content = (
        "I will inspect the runtime.\n"
        '<pivot-action>{"kind":"executor","name":"shell","arguments":{"command":"pwd"}}</pivot-action>'
    )

    detected = ActionDetector().detect(ParsedResponse(text=content, content=content))

    assert detected.text == "I will inspect the runtime."
    assert detected.content == detected.text
    assert len(detected.actions) == 1
    assert detected.actions[0].kind == ActionKind.EXECUTOR
    assert detected.actions[0].name == "shell"
    assert detected.actions[0].call.name == ACTION_TOOL


def test_action_detector_normalizes_capability_kind_aliases() -> None:
    for kind in ("think", "measure", "work"):
        response = ParsedResponse(
            tool_calls=(
                ToolCall(
                    ACTION_TOOL,
                    {"kind": kind, "name": "linux_package_runbook", "arguments": {"operation": "search"}},
                    "alias-call",
                ),
            )
        )
        detected = ActionDetector().detect(response)
        assert detected.actions[0].kind == ActionKind.CAPABILITY
        assert detected.actions[0].name == "linux_package_runbook"


def test_shell_executor_uses_instance_cwd_and_controlled_output(tmp_path: Path) -> None:
    executor = ShellExecutor(tmp_path, timeout=2, max_output_bytes=128)

    result = executor.execute({"command": 'printf "%s\\n" "$PIVOT_INSTANCE_PATH"; pwd'})

    assert result["exit_code"] == 0
    assert result["stderr"] == ""
    assert result["stdout"].splitlines() == [str(tmp_path), str(tmp_path)]
    assert not result["truncated"]
    with pytest.raises(ExecutorError, match="between 0 and 2"):
        executor.execute({"command": "true", "timeout": 3})


def test_fixed_text_action_executes_through_agent_router(tmp_path: Path) -> None:
    class TextActionLLM:
        def complete(self, messages, *, tools=()):
            if not any(message.role == "tool" for message in messages):
                content = (
                    '<pivot-action>{"kind":"executor","name":"shell",'
                    '"arguments":{"command":"printf routed"}}</pivot-action>'
                )
                return {"choices": [{"message": {"content": content}}]}
            result = json.loads(messages[-1].content)
            assert result["stdout"] == "routed"
            return {"choices": [{"message": {"content": "Execution was routed."}}]}

    executors = ExecutorRegistry()
    executors.register(ShellExecutor(tmp_path, timeout=2))
    memory = MemoryStore(tmp_path / "memory")
    agent = PersistentAgent(
        memory.main_agent_id(),
        llm=TextActionLLM(),
        capabilities=CapabilityRegistry(),
        memory=memory,
        executors=executors,
    )

    assert agent.activate("run it") == "Execution was routed."
    assert agent.history[1].content == ""
    assert agent.history[1].tool_calls[0].name == ACTION_TOOL
    memory.close()


class DelegatingLLM:
    def complete(self, messages, *, tools=()):
        context = json.loads(messages[0].content.split("\n", 1)[1])
        assert any(tool["function"]["name"] == ACTION_TOOL for tool in tools)
        tool_messages = [message for message in messages if message.role == "tool"]
        if context["agent"]["role"] == "main":
            assert all(tool["function"]["name"] != "pivot_wait_event" for tool in tools)
            if tool_messages:
                result = json.loads(tool_messages[-1].content)
                assert result["accepted"] is True
                return {"choices": [{"message": {"content": "Worker task accepted."}}]}
            action = {
                "kind": "control",
                "name": "agent.delegate",
                "arguments": {
                    "task": "Inspect the assigned resource.",
                    "name": "inspector",
                    "capabilities": ["echo"],
                    "events": ["ready"],
                },
            }
            return _action_response(action, "main-delegate")

        assert [item["name"] for item in context["capabilities"]] == ["echo"]
        assert [item["name"] for item in context["events"]] == ["ready"]
        assert any(tool["function"]["name"] == "pivot_wait_event" for tool in tools)
        if not tool_messages:
            return _action_response(
                {"kind": "capability", "name": "echo", "arguments": {"value": "checked"}},
                "worker-capability",
            )
        if len(tool_messages) == 1:
            return _action_response(
                {
                    "kind": "control",
                    "name": "agent.report",
                    "arguments": {"result": {"finding": "scoped work complete"}},
                },
                "worker-report",
            )
        return {"choices": [{"message": {"content": "Worker finished."}}]}


def _action_response(action: dict[str, object], call_id: str) -> dict[str, object]:
    return {
        "choices": [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": call_id,
                            "function": {"name": ACTION_TOOL, "arguments": json.dumps(action)},
                        }
                    ],
                }
            }
        ]
    }


def _agent_runtime(tmp_path: Path) -> Runtime:
    llm = DelegatingLLM()
    registry = CapabilityRegistry()
    registry.register(
        CapabilityDescriptor("echo", "work", "Echo", {"type": "object"}),
        lambda value: {"value": value},
    )
    registry.register(
        CapabilityDescriptor("unassigned", "work", "Must remain unavailable", {"type": "object"}),
        lambda: {"unexpected": True},
    )
    events = EventPool()
    events.register(EventDescriptor("ready", "Ready state", "ready", ("==",)))
    events.register(EventDescriptor("private", "Private state", "private", ("==",)))
    event_service = EventService(events, EventSupervisor(events, None))
    executors = ExecutorRegistry()
    executors.register(ShellExecutor(tmp_path, timeout=2))
    memory = MemoryStore(tmp_path / "memory")
    main = PersistentAgent(
        memory.main_agent_id(),
        llm=llm,
        capabilities=registry,
        memory=memory,
        events=events,
        event_service=event_service,
        executors=executors,
        max_rounds=6,
    )
    agents = AgentControl(
        main,
        llm=llm,
        capabilities=registry,
        memory=memory,
        events=events,
        event_service=event_service,
        executors=executors,
        max_rounds=6,
    )
    config = PivotConfig(tmp_path, ProviderCredential("test", "test-model"))
    return Runtime(config, registry, events, event_service, memory, main, None, executors, agents)


def test_main_agent_delegates_scoped_work_and_receives_report(tmp_path: Path) -> None:
    runtime = _agent_runtime(tmp_path)
    assert runtime.agents is not None
    updates = []

    response = runtime.agents.main_agent.activate("Handle this", progress=updates.append)

    assert response == "Worker task accepted."
    records = runtime.agents.records()
    assert len(records) == 2
    worker = records[1]
    worker = runtime.agents.wait(worker.agent_id, timeout=2)
    assert worker.capabilities == ("echo",)
    assert worker.events == ("ready",)
    assert worker.report == {"finding": "scoped work complete"}
    assert worker.state == "completed"
    assert "agent_started" in [update.kind for update in updates]
    with pytest.raises(AgentControlError, match="Unknown assigned capabilities"):
        runtime.agents.invoke_main(
            "agent.create",
            {"capabilities": ["missing"], "events": []},
        )


def test_worker_assignment_does_not_block_main_agent(tmp_path: Path) -> None:
    worker_started = threading.Event()
    release_worker = threading.Event()

    class BlockingWorkerLLM:
        def complete(self, messages, *, tools=()):
            context = json.loads(messages[0].content.split("\n", 1)[1])
            assert context["agent"]["role"] == "worker"
            worker_started.set()
            release_worker.wait(timeout=2)
            return {"choices": [{"message": {"content": "worker result"}}]}

    registry = CapabilityRegistry()
    events = EventPool()
    service = EventService(events, EventSupervisor(events, None))
    executors = ExecutorRegistry()
    executors.register(ShellExecutor(tmp_path, timeout=2))
    memory = MemoryStore(tmp_path / "memory")
    main = PersistentAgent(
        memory.main_agent_id(),
        llm=BlockingWorkerLLM(),
        capabilities=registry,
        memory=memory,
        events=events,
        event_service=service,
        executors=executors,
    )
    agents = AgentControl(
        main,
        llm=BlockingWorkerLLM(),
        capabilities=registry,
        memory=memory,
        events=events,
        event_service=service,
        executors=executors,
        max_rounds=2,
    )

    assignment = agents.invoke_main(
        "agent.delegate",
        {"task": "wait for release", "capabilities": [], "events": []},
    )
    assert assignment["accepted"] is True
    assert worker_started.wait(timeout=1)
    worker_id = assignment["agent"]["agent_id"]
    assert agents.get(worker_id).state == "running"

    release_worker.set()
    worker = agents.wait(worker_id, timeout=2)
    assert worker.state == "completed"
    assert worker.report == "worker result"
    agents.close()
    memory.close()


def test_worker_completion_reenters_main_agent_mailbox(tmp_path: Path) -> None:
    class CompletionLLM:
        def complete(self, messages, *, tools=()):
            context = json.loads(messages[0].content.split("\n", 1)[1])
            if context["agent"]["role"] == "worker":
                return {"choices": [{"message": {"content": "worker evidence"}}]}
            assert any(
                message.role == "system" and "worker_completion" in str(message.content)
                for message in messages[1:]
            )
            return {"choices": [{"message": {"content": "worker evidence integrated"}}]}

    llm = CompletionLLM()
    registry = CapabilityRegistry()
    events = EventPool()
    service = EventService(events, EventSupervisor(events, None))
    executors = ExecutorRegistry()
    executors.register(ShellExecutor(tmp_path, timeout=2))
    memory = MemoryStore(tmp_path / "memory")
    main = PersistentAgent(
        memory.main_agent_id(),
        llm=llm,
        capabilities=registry,
        memory=memory,
        events=events,
        event_service=service,
        executors=executors,
    )
    agents = AgentControl(
        main,
        llm=llm,
        capabilities=registry,
        memory=memory,
        events=events,
        event_service=service,
        executors=executors,
        max_rounds=2,
    )
    runtime = Runtime(
        PivotConfig(tmp_path, ProviderCredential("test", "test-model")),
        registry,
        events,
        service,
        memory,
        main,
        None,
        executors,
        agents,
    )
    client = PivotClient(runtime)
    try:
        delegated = client.control.submit(
            "agent.delegate",
            {"task": "collect evidence", "capabilities": [], "events": []},
        )
        assert client.control.wait_task(delegated, timeout=2).state == "completed"
        worker = agents.records()[1]
        agents.wait(worker.agent_id, timeout=2)

        deadline = time.monotonic() + 2
        completion_task = None
        while time.monotonic() < deadline:
            candidates = [task for task in client.control.tasks() if task.operation == "agent.message"]
            if candidates:
                completion_task = client.control.wait_task(candidates[-1].task_id, timeout=0.2)
                if completion_task.state not in {"queued", "running"}:
                    break
            time.sleep(0.01)
        assert completion_task is not None
        assert completion_task.state == "completed"
        assert main.history[-1].content == "worker evidence integrated"
    finally:
        client.close()
