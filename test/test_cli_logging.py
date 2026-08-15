from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from pivot.activation import PersistentAgent
from pivot.agents import AgentControl
from pivot.capabilities import CapabilityRegistry
from pivot.config import PivotConfig
from pivot.credentials import ProviderCredential
from pivot.dependencies import DependencyState, DependencyStatus
from pivot.events import EventPool, EventService, EventSupervisor
from pivot.executors import ExecutorRegistry, ShellExecutor
from pivot.memory import MemoryStore
from pivot.models import CapabilityDescriptor, EventDescriptor
from pivot.runtime import PivotClient, Runtime
from pivot.tui import AgentItem, AgentMessage, DependencyItem, PIVOT_THEME, PivotApp, PromptEditor, WorkflowView
from pivot.ui import RuntimeSummary, render_banner, safe_endpoint


class EchoLLM:
    def complete(self, messages, *, tools=()):
        return {"choices": [{"message": {"content": "ack"}}]}


def _runtime(tmp_path: Path, llm: Any | None = None) -> Runtime:
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
        max_rounds=8,
    )
    config = PivotConfig(instance_path=tmp_path, provider=ProviderCredential("test", "test-model"))
    return Runtime(config, registry, events, event_service, memory, main, None, executors, agents)


def test_textual_cli_theme_and_bindings_are_global_agent_only() -> None:
    assert PIVOT_THEME.name == "pivot-iris-dark"
    assert PIVOT_THEME.dark
    allowed = {"#418AB4", "#407D52", "#9E2E24", "#CFB64A", "#DAE3E6", "#000000"}
    assert {
        PIVOT_THEME.primary,
        PIVOT_THEME.secondary,
        PIVOT_THEME.accent,
        PIVOT_THEME.success,
        PIVOT_THEME.warning,
        PIVOT_THEME.error,
        PIVOT_THEME.foreground,
        PIVOT_THEME.background,
        PIVOT_THEME.surface,
        PIVOT_THEME.panel,
        PIVOT_THEME.boost,
        *PIVOT_THEME.variables.values(),
    } <= allowed
    bindings = {binding.key: binding for binding in PivotApp.BINDINGS}
    assert {"ctrl+q", "ctrl+b", "ctrl+g", "ctrl+l"} <= bindings.keys()
    assert {"ctrl+n", "ctrl+left", "ctrl+right", "f2"}.isdisjoint(bindings)
    labels = {state: DependencyItem(DependencyStatus("sensor", state))._label() for state in DependencyState}
    assert "$success" in labels[DependencyState.READY] and "√" in labels[DependencyState.READY]
    assert all(
        "$warning" in labels[state] and "⚪" in labels[state]
        for state in (DependencyState.STARTING, DependencyState.DEGRADED, DependencyState.STOPPING)
    )
    assert all(
        "$error" in labels[state] and "×" in labels[state]
        for state in (DependencyState.STOPPED, DependencyState.ERROR)
    )
    assert all("\n" not in label for label in labels.values())


@pytest.mark.asyncio
async def test_textual_cli_displays_agent_dependencies_and_global_controls(tmp_path: Path) -> None:
    class Dependencies:
        refreshed = False

        def descriptors(self):
            return (object(),)

        def statuses(self, *, refresh: bool = False):
            self.refreshed = self.refreshed or refresh
            return (DependencyStatus("sensor", DependencyState.READY, "available"),)

        def close(self) -> None:
            pass

    runtime = _runtime(tmp_path)
    object.__setattr__(runtime, "dependencies", Dependencies())
    client = PivotClient(runtime)
    app = PivotApp(client, client.main_agent())
    async with app.run_test(size=(140, 36)) as pilot:
        assert [item.record.role.value for item in app.query(AgentItem)] == ["main"]
        dependencies = list(app.query(DependencyItem))
        assert [item.status.dependency_id for item in dependencies] == ["sensor"]
        assert dependencies[0].parent is app.query_one("#dependency-list")
        assert [str(button.label) for button in app.query("#shortcut-bar Button")] == [
            "Agents (Ctrl+B)",
            "Stop (Ctrl+G)",
            "Prompt (Ctrl+L)",
            "Quit (Ctrl+Q)",
        ]
        app.query_one("#prompt", PromptEditor).blur()
        await pilot.click("#shortcut-prompt")
        assert app.query_one("#prompt", PromptEditor).has_focus
        app._request_dependency_refresh()
        await app.workers.wait_for_complete()
        await app._refresh_dependency_list()
        assert runtime.dependencies is not None
        assert runtime.dependencies.refreshed  # type: ignore[attr-defined]
    client.close()


def test_banner_contains_runtime_summary_and_safe_endpoint() -> None:
    agent_id = "4b3c9f24-582c-42b1-bf25-f24a6f907f67"
    summary = RuntimeSummary(
        provider="test-provider",
        model="test-model",
        endpoint=safe_endpoint("https://user:secret@example.test/v1?token=secret"),
        agent_id=agent_id,
        capabilities=(CapabilityDescriptor("read", "measure", "Read"),),
        events=(EventDescriptor("ready", "Ready", "state", ("==",)),),
    )
    banner = render_banner(summary)
    assert "test-model" in banner
    assert agent_id in banner
    assert "measure:read" in banner
    assert "ready" in banner
    assert "secret" not in banner


@pytest.mark.asyncio
async def test_textual_cli_keeps_meaningful_capability_workflow(tmp_path: Path) -> None:
    class ToolLLM:
        def __init__(self) -> None:
            self.calls = 0

        def complete(self, messages, *, tools=()):
            self.calls += 1
            if self.calls % 2:
                return {
                    "choices": [{"message": {"tool_calls": [{
                        "id": "tool-1",
                        "function": {"name": "echo", "arguments": '{"value":"measured"}'},
                    }]}}]
                }
            return {"choices": [{"message": {"content": "The result is **measured**."}}]}

    runtime = _runtime(tmp_path, ToolLLM())
    runtime.registry.register(
        CapabilityDescriptor("echo", "work", "Echo a value", {"type": "object"}),
        lambda value: {"value": value},
    )
    client = PivotClient(runtime)
    app = PivotApp(client, client.main_agent())
    async with app.run_test(size=(120, 36)) as pilot:
        prompt = app.query_one("#prompt", PromptEditor)
        prompt.text = "measure it"
        await pilot.press("enter")
        await pilot.pause(0.3)
        workflow = app.query_one(WorkflowView)
        assert [(step.kind, step.name, step.state) for step in workflow.state.steps] == [
            ("model", "model-round-1", "done"),
            ("capability", "echo", "done"),
            ("model", "model-round-2", "done"),
        ]
        messages = list(app.query(AgentMessage))
        assert [message.role for message in messages] == ["user", "assistant"]
        assert messages[-1].content == "The result is **measured**."
        assert prompt.has_focus and prompt.text == ""
    client.close()


@pytest.mark.asyncio
async def test_textual_cli_queues_messages_while_main_agent_runs(tmp_path: Path) -> None:
    started = threading.Event()
    release = threading.Event()
    prompts: list[str] = []

    class SlowLLM:
        def complete(self, messages, *, tools=()):
            prompt = next(message.content for message in reversed(messages) if message.role == "user")
            prompts.append(prompt)
            if prompt == "first":
                started.set()
                release.wait(timeout=2)
            return {"choices": [{"message": {"content": f"ack {prompt}"}}]}

    client = PivotClient(_runtime(tmp_path, SlowLLM()))
    app = PivotApp(client, client.main_agent())
    async with app.run_test(size=(120, 36)) as pilot:
        prompt = app.query_one("#prompt", PromptEditor)
        prompt.text = "first"
        await pilot.press("enter")
        assert started.wait(timeout=1)
        prompt.text = "second"
        await pilot.press("enter")
        await pilot.pause(0.05)
        assert "1 queued" in str(app.query_one("#agent-state").render())
        release.set()
        await pilot.pause(0.8)
        assert prompts == ["first", "second"]
        assert [item.content for item in app.query(AgentMessage) if item.role == "assistant"] == [
            "ack first",
            "ack second",
        ]
    client.close()


@pytest.mark.asyncio
async def test_textual_cli_interrupts_active_activation(tmp_path: Path) -> None:
    started = threading.Event()
    release = threading.Event()

    class SlowLLM:
        def complete(self, messages, *, tools=()):
            started.set()
            release.wait(timeout=2)
            return {"choices": [{"message": {"content": "late response"}}]}

    client = PivotClient(_runtime(tmp_path, SlowLLM()))
    app = PivotApp(client, client.main_agent())
    async with app.run_test(size=(120, 36)) as pilot:
        prompt = app.query_one("#prompt", PromptEditor)
        prompt.text = "long task"
        await pilot.press("enter")
        assert started.wait(timeout=1)
        await pilot.press("ctrl+g")
        turn = app.turns[app.agent_activations[app.main_agent.agent_id][0]]
        assert turn.cancellation.is_cancelled()
        release.set()
        await pilot.pause(0.3)
        assert turn.interrupted
        assert all(message.content != "late response" for message in app.query(AgentMessage))
        assert list(app.query(AgentMessage))[-1].content == "Turn interrupted."
    client.close()


@pytest.mark.asyncio
async def test_textual_cli_commands_and_compact_layout(tmp_path: Path) -> None:
    client = PivotClient(_runtime(tmp_path))
    app = PivotApp(client, client.main_agent())
    async with app.run_test(size=(72, 24)) as pilot:
        assert app.has_class("compact")
        assert app.query_one("#body").has_class("agents-hidden")
        prompt = app.query_one("#prompt", PromptEditor)
        prompt.text = "/help"
        await pilot.press("enter")
        notice = list(app.query(AgentMessage))[-1]
        assert notice.role == "notice"
        assert "/agents" in notice.content and "/stop" in notice.content
        app.action_toggle_agents()
        assert not app.query_one("#body").has_class("agents-hidden")
    client.close()


def test_pivot_client_exposes_only_persistent_main_agent(tmp_path: Path) -> None:
    client = PivotClient(_runtime(tmp_path))
    try:
        main = client.main_agent()
        assert client.main_agent() is main
        operation_names = {item.name for item in client.control.operations()}
        assert {"agent.main", "agent.message", "agent.create", "agent.assign", "agent.delegate"} <= operation_names
        assert client.run_main("hello") == "ack"
    finally:
        client.close()
