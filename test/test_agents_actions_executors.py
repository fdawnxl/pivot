from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from pivot.actions import ACTION_TOOL, ActionDetector, ActionKind
from pivot.agents import AgentControl, AgentControlError
from pivot.capabilities import CapabilityRegistry
from pivot.config import PivotConfig
from pivot.credentials import ProviderCredential
from pivot.events import EventDescriptor, EventPool, EventService, EventSupervisor
from pivot.executors import ExecutorError, ExecutorRegistry, ShellExecutor
from pivot.memory import TextMemory
from pivot.models import CapabilityDescriptor, ParsedResponse
from pivot.runtime import Runtime
from pivot.session import ConversationSession, SessionManager


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


def test_shell_executor_uses_instance_cwd_and_controlled_output(tmp_path: Path) -> None:
    executor = ShellExecutor(tmp_path, timeout=2, max_output_bytes=128)

    result = executor.execute({"command": 'printf "%s\\n" "$PIVOT_INSTANCE_PATH"; pwd'})

    assert result["exit_code"] == 0
    assert result["stderr"] == ""
    assert result["stdout"].splitlines() == [str(tmp_path), str(tmp_path)]
    assert not result["truncated"]
    with pytest.raises(ExecutorError, match="between 0 and 2"):
        executor.execute({"command": "true", "timeout": 3})


def test_fixed_text_action_executes_through_session_router(tmp_path: Path) -> None:
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
    session = ConversationSession(
        str(uuid4()),
        llm=TextActionLLM(),
        capabilities=CapabilityRegistry(),
        executors=executors,
    )

    assert session.run("run it") == "Execution was routed."
    assert session.history[2].content == ""
    assert session.history[2].tool_calls[0].name == ACTION_TOOL


class DelegatingLLM:
    def complete(self, messages, *, tools=()):
        context = json.loads(messages[0].content.split("\n", 1)[1])
        assert any(tool["function"]["name"] == ACTION_TOOL for tool in tools)
        tool_messages = [message for message in messages if message.role == "tool"]
        if context["agent"]["role"] == "main":
            if tool_messages:
                result = json.loads(tool_messages[-1].content)
                assert result["result"] == {"finding": "scoped work complete"}
                return {"choices": [{"message": {"content": "Main agent synthesized the worker report."}}]}
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
    memory = TextMemory(tmp_path / "memory")
    sessions = SessionManager(
        llm=llm,
        capabilities=registry,
        memory=memory,
        events=events,
        event_service=event_service,
        executors=executors,
        max_rounds=6,
    )
    main = sessions.get(str(uuid4()))
    agents = AgentControl(
        main,
        llm=llm,
        capabilities=registry,
        child_memory=TextMemory(tmp_path / "memory" / "agents"),
        events=events,
        event_service=event_service,
        executors=executors,
        max_rounds=6,
    )
    config = PivotConfig(tmp_path, ProviderCredential("test", "test-model"))
    return Runtime(config, registry, events, event_service, sessions, None, executors, agents)


def test_main_agent_delegates_scoped_work_and_receives_report(tmp_path: Path) -> None:
    runtime = _agent_runtime(tmp_path)
    assert runtime.agents is not None
    updates = []

    response = runtime.agents.main_agent.run("Handle this", progress=updates.append)

    assert response == "Main agent synthesized the worker report."
    records = runtime.agents.records()
    assert len(records) == 2
    worker = records[1]
    assert worker.capabilities == ("echo",)
    assert worker.events == ("ready",)
    assert worker.report == {"finding": "scoped work complete"}
    assert worker.state == "completed"
    assert "agent_started" in [update.kind for update in updates]
    assert "agent_completed" in [update.kind for update in updates]
    with pytest.raises(AgentControlError, match="Unknown assigned capabilities"):
        runtime.agents.invoke_main(
            "agent.create",
            {"capabilities": ["missing"], "events": []},
        )
