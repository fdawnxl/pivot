from __future__ import annotations

import os
from pathlib import Path

import pytest
from pivot.capabilities import CapabilityRegistry
from pivot.config import PivotConfig
from pivot.credentials import ProviderCredential
from pivot.events import EventPool, EventService, EventSupervisor
from pivot.memory import TextMemory
from pivot.models import CapabilityDescriptor, EventDescriptor
from pivot.runtime import PivotClient, Runtime
from pivot.session import SessionManager
from pivot.tui import PIVOT_THEME, ConversationMessage, PivotApp, PromptEditor, SessionItem, WorkflowView
from pivot.ui import RuntimeSummary, render_banner, safe_endpoint


class EchoLLM:
    def complete(self, messages, *, tools=()):
        return {"choices": [{"message": {"content": "ack"}}]}


def test_textual_cli_uses_pivot_iris_dark_palette() -> None:
    assert PIVOT_THEME.name == "pivot-iris-dark"
    assert PIVOT_THEME.dark
    assert PIVOT_THEME.primary == "#418AB4"
    assert PIVOT_THEME.secondary == "#418AB4"
    assert PIVOT_THEME.foreground == "#DAE3E6"
    assert PIVOT_THEME.background == "#000000"
    assert PIVOT_THEME.accent == "#407D52"
    assert PIVOT_THEME.success == "#407D52"
    assert PIVOT_THEME.warning == "#CFB64A"
    assert PIVOT_THEME.error == "#9E2E24"
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


def test_textual_cli_shortcut_bindings_exclude_f2_and_alt_navigation() -> None:
    bindings = {binding.key: binding for binding in PivotApp.BINDINGS}
    assert {"ctrl+q", "ctrl+n", "ctrl+b", "ctrl+g", "ctrl+left", "ctrl+right", "ctrl+l"} <= bindings.keys()
    assert all(bindings[key].show for key in ("ctrl+q", "ctrl+n", "ctrl+b", "ctrl+g", "ctrl+left", "ctrl+right", "ctrl+l"))
    assert "f2" not in bindings
    assert "alt+up" not in bindings
    assert "alt+down" not in bindings


def test_session_item_renders_runtime_state_and_current_label() -> None:
    assert "$success" not in SessionItem("12345678", state="ready")._label()
    assert "$success" in SessionItem("12345678", state="running")._label()
    assert "$warning" in SessionItem("12345678", state="pending")._label()
    assert "CURRENT" in SessionItem("12345678", current=True)._label()
    assert "ACTIVE" not in SessionItem("12345678", current=True)._label()


@pytest.mark.asyncio
async def test_textual_cli_sorts_sessions_by_state_then_recent_activity(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    ready_old = runtime.sessions.create()
    pending = runtime.sessions.create()
    running_old = runtime.sessions.create()
    running_new = runtime.sessions.create()
    ready_new = runtime.sessions.create()
    pending._set_state("pending")
    running_old._set_state("running")
    running_new._set_state("running")
    ready_new._set_state("ready")
    app = PivotApp(PivotClient(runtime), ready_old)

    async with app.run_test(size=(120, 36)):
        items = list(app.query(SessionItem))
        assert [item.session_id for item in items] == [
            running_new.session_id,
            running_old.session_id,
            pending.session_id,
            ready_new.session_id,
            ready_old.session_id,
        ]


@pytest.mark.asyncio
async def test_textual_cli_shortcut_bar_labels_and_clicks_common_actions(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    first = runtime.sessions.create()
    app = PivotApp(PivotClient(runtime), first)

    async with app.run_test(size=(160, 36)) as pilot:
        labels = [str(button.label) for button in app.query("#shortcut-bar Button")]
        assert labels == [
            "New (Ctrl+N)",
            "Older (Ctrl+←)",
            "Newer (Ctrl+→)",
            "Sessions (Ctrl+B)",
            "Stop (Ctrl+G)",
            "Prompt (Ctrl+L)",
            "Quit (Ctrl+Q)",
        ]

        await pilot.click("#shortcut-new")
        await pilot.pause()
        second = app.current_session
        assert second is not first

        await pilot.click("#shortcut-older")
        await pilot.pause()
        assert app.current_session is first

        await pilot.click("#shortcut-newer")
        await pilot.pause()
        assert app.current_session is second

        app.query_one("#prompt", PromptEditor).blur()
        await pilot.click("#shortcut-prompt")
        await pilot.pause()
        assert app.query_one("#prompt", PromptEditor).has_focus


@pytest.mark.asyncio
async def test_textual_cli_lists_persisted_sessions_newest_first(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    older = runtime.sessions.create()
    newer = runtime.sessions.create()
    memory = TextMemory(tmp_path / "memory")
    memory.write(older.session_id, "")
    memory.write(newer.session_id, "")
    os.utime(memory.path_for(older.session_id), (1, 1))
    os.utime(memory.path_for(newer.session_id), (2, 2))
    app = PivotApp(PivotClient(runtime), older)

    async with app.run_test(size=(120, 36)):
        items = list(app.query(SessionItem))
        assert [item.session_id for item in items] == [newer.session_id, older.session_id]


def test_banner_contains_runtime_summary_and_safe_endpoint() -> None:
    session_id = "4b3c9f24-582c-42b1-bf25-f24a6f907f67"
    summary = RuntimeSummary(
        provider="test-provider",
        model="test-model",
        endpoint=safe_endpoint("https://user:secret@example.test/v1?token=secret"),
        session_id=session_id,
        capabilities=(CapabilityDescriptor("read", "measure", "Read"),),
        events=(EventDescriptor("ready", "Ready", "state", ("==",)),),
    )
    banner = render_banner(summary)
    assert "____  _" in banner
    assert "test-model" in banner
    assert "test-provider" in banner
    assert session_id in banner
    assert "measure:read" in banner
    assert "ready" in banner
    assert "secret" not in banner


def _runtime(tmp_path: Path, llm=None) -> Runtime:
    registry = CapabilityRegistry()
    events = EventPool()
    manager = SessionManager(llm=llm or EchoLLM(), capabilities=registry, memory=TextMemory(tmp_path / "memory"))
    config = PivotConfig(workspace_path=tmp_path, provider=ProviderCredential("test", "test-model"))
    event_service = EventService(events, EventSupervisor(events, runner=None))  # type: ignore[arg-type]
    return Runtime(config, registry, events, event_service, manager)


@pytest.mark.asyncio
async def test_textual_cli_keeps_prompt_focused_and_renders_response(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    session = runtime.sessions.create()
    app = PivotApp(PivotClient(runtime), session)

    async with app.run_test(size=(120, 36)) as pilot:
        prompt = app.query_one("#prompt", PromptEditor)
        prompt.text = "hello"
        prompt.focus()
        await pilot.press("enter")
        await pilot.pause(0.3)

        messages = list(app.query(ConversationMessage))
        assert [message.role for message in messages] == ["user", "assistant"]
        assert messages[-1].content == "ack"
        assert prompt.has_focus
        assert prompt.text == ""
        workflow = app.query_one(WorkflowView)
        assert [(step.kind, step.state, step.result) for step in workflow.state.steps] == [
            ("model", "done", "Response ready")
        ]


@pytest.mark.asyncio
async def test_textual_cli_keeps_meaningful_capability_workflow(tmp_path: Path) -> None:
    class ToolLLM:
        def __init__(self) -> None:
            self.calls = 0

        def complete(self, messages, *, tools=()):
            self.calls += 1
            if self.calls % 2 == 1:
                return {
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {"id": "tool-1", "function": {"name": "echo", "arguments": '{"value":"measured"}'}}
                                ]
                            }
                        }
                    ]
                }
            return {"choices": [{"message": {"content": "The result is **measured**."}}]}

    runtime = _runtime(tmp_path, ToolLLM())
    runtime.registry.register(
        CapabilityDescriptor("echo", "work", "Echo a value", {"type": "object"}),
        lambda value: {"value": value},
    )
    app = PivotApp(PivotClient(runtime), runtime.sessions.create())

    async with app.run_test(size=(120, 36)) as pilot:
        prompt = app.query_one("#prompt", PromptEditor)
        prompt.text = "measure it"
        await pilot.press("enter")
        await pilot.pause(0.3)

        workflow = app.query_one(WorkflowView)
        assert workflow.state.done
        assert workflow.state.status == "Completed"
        assert [(step.kind, step.name, step.state) for step in workflow.state.steps] == [
            ("model", "model-round-1", "done"),
            ("capability", "echo", "done"),
            ("model", "model-round-2", "done"),
        ]
        assert '"value":"measured"' in workflow.state.steps[1].request
        assert "measured" in workflow.state.steps[1].result
        assert workflow.state.steps[2].result == "Response ready"
        assert list(app.query(ConversationMessage))[-1].content == "The result is **measured**."

        prompt.text = "measure it again"
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert len(list(app.query(WorkflowView))) == 2


@pytest.mark.asyncio
async def test_textual_cli_switches_sessions_while_work_continues(tmp_path: Path) -> None:
    import threading

    release = threading.Event()

    class SlowLLM:
        def complete(self, messages, *, tools=()):
            release.wait(timeout=2)
            return {"choices": [{"message": {"content": "slow ack"}}]}

    runtime = _runtime(tmp_path, SlowLLM())
    first = runtime.sessions.create()
    app = PivotApp(PivotClient(runtime), first)
    async with app.run_test(size=(120, 36)) as pilot:
        prompt = app.query_one("#prompt", PromptEditor)
        prompt.text = "long task"
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert first.session_id in app.session_turns

        await pilot.press("ctrl+n")
        await pilot.pause()
        second = app.current_session
        assert second.session_id != first.session_id
        assert prompt.has_focus

        release.set()
        await pilot.pause(0.3)
        assert first.session_id not in app.session_turns
        assert app.current_session is second


@pytest.mark.asyncio
async def test_textual_cli_keyboard_navigation_and_slash_commands(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    first = runtime.sessions.create()
    app = PivotApp(PivotClient(runtime), first)

    async with app.run_test(size=(120, 36)) as pilot:
        prompt = app.query_one("#prompt", PromptEditor)
        await pilot.press("ctrl+n")
        await pilot.pause()
        second = app.current_session
        items = list(app.query(SessionItem))
        assert [item.session_id for item in items] == [second.session_id, first.session_id]

        await pilot.press("ctrl+left")
        await pilot.pause()
        session_list = app.query_one("#session-list")
        assert app.current_session is second
        assert session_list.has_focus_within
        assert session_list.index == 0

        await pilot.press("ctrl+left")
        await pilot.pause()
        assert app.current_session is first
        assert session_list.has_focus_within
        assert session_list.index == 1

        await pilot.press("ctrl+right")
        await pilot.pause()
        assert app.current_session is second
        assert session_list.has_focus_within
        assert session_list.index == 0

        prompt.text = "/prev"
        prompt.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert app.current_session is first

        prompt.text = f"/switch {second.session_id[:8]}"
        await pilot.press("enter")
        await pilot.pause()
        assert app.current_session is second

        prompt.text = "/help"
        await pilot.press("enter")
        await pilot.pause()
        notice = list(app.query(ConversationMessage))[-1]
        assert notice.role == "notice"
        assert "/stop" in notice.content
        assert "Ctrl+G" in notice.content


@pytest.mark.asyncio
async def test_textual_cli_interrupts_active_turn(tmp_path: Path) -> None:
    import threading

    started = threading.Event()
    release = threading.Event()

    class SlowLLM:
        def complete(self, messages, *, tools=()):
            started.set()
            release.wait(timeout=2)
            return {"choices": [{"message": {"content": "late response"}}]}

    runtime = _runtime(tmp_path, SlowLLM())
    session = runtime.sessions.create()
    app = PivotApp(PivotClient(runtime), session)
    async with app.run_test(size=(120, 36)) as pilot:
        prompt = app.query_one("#prompt", PromptEditor)
        prompt.text = "long task"
        await pilot.press("enter")
        assert started.wait(timeout=1)

        await pilot.press("ctrl+g")
        await pilot.pause()
        turn = app.turns[app.session_turns[session.session_id]]
        assert turn.cancellation.is_cancelled()
        assert turn.status == "Stopping at the next safe point"

        release.set()
        await pilot.pause(0.3)
        assert session.session_id not in app.session_turns
        assert turn.interrupted
        assert turn.status == "Interrupted"
        assert all(message.content != "late response" for message in app.query(ConversationMessage))
        assert list(app.query(ConversationMessage))[-1].content == "Turn interrupted."


@pytest.mark.asyncio
async def test_textual_cli_uses_compact_layout_on_narrow_terminals(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    app = PivotApp(PivotClient(runtime), runtime.sessions.create())

    async with app.run_test(size=(72, 24)):
        assert app.has_class("compact")
        assert not app.query_one("#sessions-pane").display
        assert app.query_one("#prompt", PromptEditor).has_focus

        app.action_toggle_sessions()
        assert app.query_one("#sessions-pane").display


def test_pivot_client_keeps_runtime_api_outside_cli(tmp_path: Path) -> None:
    client = PivotClient(_runtime(tmp_path))

    session = client.create_session()
    assert client.get_session(session.session_id) is session
    assert client.run(session.session_id, "hello") == "ack"
    assert client.sessions() == (session,)
