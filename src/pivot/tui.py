"""Textual terminal interface for interactive pivot conversations."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal
from uuid import UUID, uuid4

from rich.markup import escape
from textual import events, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, HorizontalScroll, Vertical, VerticalScroll
from textual.message import Message as TextualMessage
from textual.theme import Theme
from textual.widgets import Button, Label, ListItem, ListView, LoadingIndicator, Markdown, Static, TextArea

from .dependencies import DependencyState, DependencyStatus
from .models import Message
from .runtime import PivotClient
from .session import CancellationToken, ConversationSession, SessionCancelled, SessionProgress, SessionState
from .ui import safe_endpoint

LOGGER = logging.getLogger(__name__)

PIVOT_THEME = Theme(
    name="pivot-iris-dark",
    primary="#418AB4",
    secondary="#418AB4",
    accent="#407D52",
    success="#407D52",
    warning="#CFB64A",
    error="#9E2E24",
    foreground="#DAE3E6",
    background="#000000",
    surface="#000000",
    panel="#418AB4",
    boost="#418AB4",
    dark=True,
    variables={
        "text-muted": "#DAE3E6",
        "border": "#418AB4",
        "border-blurred": "#418AB4",
        "primary-ink": "#000000",
        "warning-ink": "#000000",
        "positive-surface": "#000000",
        "error-surface": "#000000",
        "input-cursor-background": "#418AB4",
        "input-cursor-foreground": "#DAE3E6",
    },
)


class PromptEditor(TextArea):
    """Prompt editor that submits with Enter and inserts lines with Shift+Enter."""

    BINDINGS = [Binding("ctrl+enter", "submit", "Send", show=False), *TextArea.BINDINGS]

    class Submitted(TextualMessage):
        """Posted when the user submits the current prompt."""

        def __init__(self, editor: "PromptEditor", value: str) -> None:
            super().__init__()
            self.editor = editor
            self.value = value

        @property
        def control(self) -> "PromptEditor":
            return self.editor

    def action_submit(self) -> None:
        value = self.text.strip()
        if value:
            self.post_message(self.Submitted(self, value))

    async def _on_key(self, event: events.Key) -> None:
        if event.key in {"enter", "ctrl+enter"}:
            event.stop()
            event.prevent_default()
            self.action_submit()
            return
        if event.key == "shift+enter":
            event.stop()
            event.prevent_default()
            self.insert("\n")
            return
        await super()._on_key(event)


class ConversationMessage(Vertical):
    """One user, assistant, or error entry in the conversation timeline."""

    def __init__(self, role: str, content: str) -> None:
        super().__init__(classes=f"message {role}")
        self.role = role
        self.content = content

    def compose(self) -> ComposeResult:
        labels = {"user": "YOU", "assistant": "PIVOT", "notice": "SYSTEM", "error": "ERROR"}
        yield Label(labels.get(self.role, self.role.upper()), classes="message-label")
        if self.role == "assistant":
            yield Markdown(self.content or "_(empty response)_", classes="message-body")
        else:
            yield Static(self.content, classes="message-body", markup=False)


@dataclass(slots=True)
class WorkflowStep:
    """One inspectable model, capability, or event phase in a turn."""

    kind: Literal["model", "decision", "capability", "event"]
    name: str
    label: str
    round_number: int
    state: str = "running"
    request: str = ""
    result: str = ""


@dataclass(slots=True)
class TurnState:
    """UI state retained while a turn runs in the background."""

    turn_id: str
    session_id: str
    prompt: str
    status: str = "Thinking"
    steps: list[WorkflowStep] = field(default_factory=list)
    cancellation: CancellationToken = field(default_factory=CancellationToken)
    done: bool = False
    interrupted: bool = False
    error: str | None = None


class WorkflowView(Vertical):
    """Compact, in-place view of an agent turn's useful workflow."""

    def __init__(self, state: TurnState) -> None:
        super().__init__(classes="workflow")
        self.state = state

    def compose(self) -> ComposeResult:
        with Horizontal(classes="workflow-status"):
            yield LoadingIndicator(classes="workflow-spinner")
            with Vertical(classes="workflow-heading"):
                yield Label("AGENT TRACE", classes="workflow-eyebrow")
                yield Label(self.state.status, classes="workflow-title")
        yield Static("", classes="workflow-steps")

    def on_mount(self) -> None:
        self.refresh_state()

    def refresh_state(self) -> None:
        """Update status and step rows without adding terminal output."""

        title = self.query_one(".workflow-title", Label)
        spinner = self.query_one(".workflow-spinner", LoadingIndicator)
        title.update(self.state.status)
        spinner.display = not self.state.done
        self.set_class(self.state.done, "done")
        self.set_class(self.state.error is not None, "failed")
        self.set_class(self.state.interrupted, "interrupted")
        symbols = {
            "running": "[bold $primary]>[/]",
            "done": "[bold $success]ok[/]",
            "failed": "[bold $error]![/]",
            "interrupted": "[bold $warning]x[/]",
        }
        kinds = {"model": "MODEL", "decision": "DECISION", "capability": "CAPABILITY", "event": "EVENT"}
        rows = []
        for index, step in enumerate(self.state.steps, 1):
            heading = f"{index:02d}  {kinds[step.kind]}  [bold]{escape(step.label)}[/]  [dim]round {step.round_number}[/]"
            rows.append(f"{symbols[step.state]}  {heading}")
            if step.request:
                rows.append(f"    [dim]Request[/]  {escape(step.request)}")
            if step.result:
                rows.append(f"    [dim]Result[/]   {escape(step.result)}")
        self.query_one(".workflow-steps", Static).update("\n".join(rows))


class SessionItem(ListItem):
    """Selectable session entry with a stable full UUID."""

    def __init__(
        self,
        session_id: str,
        *,
        current: bool = False,
        state: SessionState = SessionState.READY,
    ) -> None:
        self.session_id = session_id
        self.current = current
        self.state = state
        super().__init__()

    def compose(self) -> ComposeResult:
        yield Label(self._label())

    def _label(self) -> str:
        markers = {
            SessionState.RUNNING: "[bold $success]●[/]",
            SessionState.PENDING: "[bold $warning]●[/]",
            SessionState.READY: "○",
        }
        current = "  CURRENT" if self.current else ""
        return f"{markers[self.state]}  {self.session_id[:8]}{current}"


class DependencyItem(Static):
    """One dependency lifecycle snapshot rendered for quick scanning."""

    def __init__(self, status: DependencyStatus) -> None:
        self.status = status
        super().__init__(self._label(), classes="dependency-item")

    def _label(self) -> str:
        markers = {
            DependencyState.READY: "[bold $success]√[/]",
            DependencyState.STARTING: "[bold $warning]⚪[/]",
            DependencyState.DEGRADED: "[bold $warning]⚪[/]",
            DependencyState.STOPPING: "[bold $warning]⚪[/]",
            DependencyState.STOPPED: "[bold $error]×[/]",
            DependencyState.ERROR: "[bold $error]×[/]",
        }
        return f"{markers[self.status.state]}  {escape(self.status.dependency_id)}"

class PivotApp(App[None]):
    """Modern multi-session terminal client for a pivot runtime."""

    TITLE = "pivot"
    SUB_TITLE = "agent runtime"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("ctrl+q", "request_quit", "Quit", priority=True),
        Binding("ctrl+n", "new_session", "New", priority=True),
        Binding("ctrl+b", "toggle_sessions", "Sessions", priority=True),
        Binding("ctrl+g", "interrupt_turn", "Stop", priority=True),
        Binding("ctrl+left", "previous_session", "Older", priority=True),
        Binding("ctrl+right", "next_session", "Newer", priority=True),
        Binding("ctrl+l", "focus_prompt", "Prompt", priority=True),
        Binding("ctrl+up", "scroll_timeline_up", "Scroll up", show=False, priority=True),
        Binding("ctrl+down", "scroll_timeline_down", "Scroll down", show=False, priority=True),
    ]
    CSS = """
    Screen {
        background: $background;
        color: $foreground;
    }

    #topbar {
        height: 3;
        padding: 0 2;
        background: $surface;
        border-bottom: solid $border;
        align-vertical: middle;
    }

    #brand {
        width: auto;
        color: $primary;
        text-style: bold;
    }

    #runtime-meta {
        width: 1fr;
        content-align: right middle;
        color: $text-muted;
    }

    #body {
        height: 1fr;
    }

    #sessions-pane {
        width: 29;
        min-width: 24;
        background: $surface;
        border-right: solid $border;
        padding: 1;
    }

    .section-title {
        height: 2;
        color: $text-muted;
        text-style: bold;
        padding-left: 1;
    }

    #session-list {
        height: 1fr;
        background: transparent;
        border: none;
    }

    #dependencies {
        height: auto;
        max-height: 10;
        margin-top: 1;
        border-top: solid $border-blurred;
        padding-top: 1;
    }

    #dependency-list {
        height: auto;
        max-height: 7;
        overflow-y: auto;
    }

    .dependency-item {
        width: 100%;
        height: 1;
        min-height: 1;
        padding: 0 1;
        color: $text-muted;
        text-overflow: ellipsis;
    }

    #dependency-empty {
        height: 2;
        padding: 0 1;
        color: $text-muted;
    }

    SessionItem {
        height: 3;
        padding: 1;
        color: $text-muted;
    }

    SessionItem:hover {
        background: $primary;
        color: $primary-ink;
    }

    SessionItem.-highlight {
        background: $primary;
        color: $primary-ink;
    }

    #new-session {
        width: 100%;
        min-width: 0;
        height: 3;
        margin-top: 1;
        border: solid $primary;
        background: $background;
        color: $primary;
    }

    #main-pane {
        width: 1fr;
        height: 1fr;
        background: $background;
    }

    #conversation-header {
        height: 4;
        padding: 1 2;
        border-bottom: solid $border-blurred;
    }

    #conversation-title {
        width: 1fr;
        color: $foreground;
        text-style: bold;
    }

    #conversation-state {
        width: auto;
        color: $primary;
    }

    #timeline {
        height: 1fr;
        padding: 1 3 2 3;
        scrollbar-color: $border;
        scrollbar-color-hover: $primary;
        scrollbar-background: $background;
    }

    #empty-state {
        height: 1fr;
        content-align: center middle;
        color: $text-muted;
    }

    ConversationMessage {
        width: 100%;
        height: auto;
        margin: 0 0 1 0;
        padding: 0 1;
    }

    ConversationMessage.user {
        border-left: thick $secondary;
        background: $surface;
    }

    ConversationMessage.assistant {
        border-left: thick $primary;
    }

    ConversationMessage.notice {
        border-left: thick $success;
        background: $positive-surface;
    }

    ConversationMessage.error {
        border-left: thick $error;
        background: $error-surface;
    }

    .message-label {
        height: 2;
        padding-top: 1;
        color: $text-muted;
        text-style: bold;
    }

    .message-body {
        height: auto;
        padding: 0 0 1 0;
        color: $foreground;
    }

    WorkflowView {
        width: 100%;
        height: auto;
        margin: 0 0 1 0;
        padding: 0 1 1 1;
        border-left: thick $accent;
        background: $surface;
    }

    WorkflowView.done {
        border-left: thick $success;
    }

    WorkflowView.failed {
        border-left: thick $error;
    }

    WorkflowView.interrupted {
        border-left: thick $warning;
    }

    .workflow-status {
        height: 3;
        align-vertical: middle;
    }

    .workflow-spinner {
        width: 4;
        height: 1;
        color: $primary;
    }

    .workflow-heading {
        width: 1fr;
        height: auto;
    }

    .workflow-eyebrow {
        height: 1;
        color: $text-muted;
        text-style: bold;
    }

    .workflow-title {
        width: 1fr;
        color: $foreground;
        text-style: bold;
    }

    .workflow-steps {
        height: auto;
        color: $foreground;
        padding-left: 1;
    }

    #composer-shell {
        height: 9;
        padding: 1 2;
        border-top: solid $border;
        background: $surface;
    }

    #prompt {
        width: 1fr;
        height: 6;
        border: solid $border;
        background: $background;
        padding: 0 1;
    }

    #prompt:focus {
        border: solid $primary;
    }

    #send {
        width: 10;
        min-width: 10;
        height: 6;
        margin-left: 1;
        border: none;
        background: $primary;
        color: $primary-ink;
        text-style: bold;
    }

    #stop {
        width: 10;
        min-width: 10;
        height: 6;
        margin-left: 1;
        border: none;
        background: $warning;
        color: $warning-ink;
        text-style: bold;
        display: none;
    }

    #shortcut-bar {
        width: 100%;
        height: 3;
        padding: 0 1;
        background: $background;
        border-top: solid $primary;
        scrollbar-size-horizontal: 1;
        scrollbar-color: $primary;
        scrollbar-background: $background;
    }

    #shortcut-bar Button {
        width: auto;
        min-width: 0;
        height: 1;
        margin: 0 2 0 0;
        padding: 0 1;
        border: none;
        background: $background;
        color: $foreground;
    }

    #shortcut-bar Button:hover, #shortcut-bar Button:focus {
        background: $primary;
        color: $primary-ink;
        text-style: bold;
    }

    .sessions-hidden #sessions-pane {
        display: none;
    }

    .compact #timeline {
        padding: 1 1 2 1;
    }

    .compact #runtime-meta, .compact #send {
        display: none;
    }
    """

    def __init__(self, client: PivotClient, session: ConversationSession, *, show_welcome: bool = True) -> None:
        super().__init__()
        self.register_theme(PIVOT_THEME)
        self.theme = PIVOT_THEME.name
        self.client = client
        self.runtime = client.runtime
        self.current_session = session
        self.show_welcome = show_welcome
        self.turns: dict[str, TurnState] = {}
        self.session_turns: dict[str, str] = {}
        self._quit_armed = False
        self._session_refresh_pending = False
        self._session_refresh_dirty = False
        self._control_unsubscribe: Any = None
        self._session_ids = self._discover_session_ids()

    def compose(self) -> ComposeResult:
        config = self.runtime.config
        with Horizontal(id="topbar"):
            yield Static("PIVOT", id="brand")
            yield Static(f"{config.provider.name}  /  {config.provider.model}", id="runtime-meta")
        with Horizontal(id="body"):
            with Vertical(id="sessions-pane"):
                yield Label("SESSIONS", classes="section-title")
                yield ListView(id="session-list")
                yield Button("New session", id="new-session")
                with Vertical(id="dependencies"):
                    yield Label("DEPENDENCIES", classes="section-title")
                    yield VerticalScroll(id="dependency-list")
            with Vertical(id="main-pane"):
                with Horizontal(id="conversation-header"):
                    yield Static("Conversation", id="conversation-title")
                    yield Static("Ready", id="conversation-state")
                yield VerticalScroll(id="timeline")
                with Horizontal(id="composer-shell"):
                    yield PromptEditor(
                        placeholder="Ask pivot anything...  Enter to send, Shift+Enter for a new line",
                        id="prompt",
                        soft_wrap=True,
                        show_line_numbers=False,
                    )
                    yield Button("Send", id="send", variant="primary")
                    yield Button("Stop", id="stop")
        with HorizontalScroll(id="shortcut-bar"):
            yield Button("New (Ctrl+N)", id="shortcut-new", classes="shortcut-button")
            yield Button("Older (Ctrl+←)", id="shortcut-older", classes="shortcut-button")
            yield Button("Newer (Ctrl+→)", id="shortcut-newer", classes="shortcut-button")
            yield Button("Sessions (Ctrl+B)", id="shortcut-sessions", classes="shortcut-button")
            yield Button("Stop (Ctrl+G)", id="shortcut-stop", classes="shortcut-button")
            yield Button("Prompt (Ctrl+L)", id="shortcut-prompt", classes="shortcut-button")
            yield Button("Quit (Ctrl+Q)", id="shortcut-quit", classes="shortcut-button")

    async def on_mount(self) -> None:
        self._control_unsubscribe = self.client.control.subscribe(self._on_control_event)
        compact = self.size.width < 90
        self.set_class(compact, "compact")
        self.query_one("#body").set_class(compact, "sessions-hidden")
        await self._refresh_session_list()
        await self._refresh_dependency_list()
        if self.runtime.dependencies is not None and self.runtime.dependencies.descriptors():
            self.set_interval(5.0, self._request_dependency_refresh)
        await self._show_session(self.current_session)
        self.query_one("#prompt", PromptEditor).focus()

    def on_unmount(self) -> None:
        if self._control_unsubscribe is not None:
            self._control_unsubscribe()
            self._control_unsubscribe = None

    def _on_control_event(self, event: str, payload: dict[str, Any]) -> None:
        try:
            self.call_from_thread(self._apply_control_event, event, payload)
        except RuntimeError:
            LOGGER.debug("TUI closed before control event delivery event=%s", event)

    def _apply_control_event(self, event: str, payload: dict[str, Any]) -> None:
        if event == "shutdown_requested":
            self.exit()
            return
        session_id = payload.get("session_id")
        if event == "session_selected" and isinstance(session_id, str):
            if session_id != self.current_session.session_id:
                self.call_later(self._show_session, self.runtime.sessions.get(session_id))
            return
        if event == "session_created":
            self._schedule_session_refresh()
            return
        if (
            event == "task_changed"
            and payload.get("operation") == "session.send"
        ):
            self._schedule_session_refresh()
            if (
                payload.get("state") in {"completed", "failed", "cancelled"}
                and session_id == self.current_session.session_id
                and session_id not in self.session_turns
            ):
                self.call_later(self._show_session, self.current_session, focus_prompt=False)

    def on_resize(self, event: events.Resize) -> None:
        compact = event.size.width < 90
        self.set_class(compact, "compact")
        if compact:
            self.query_one("#body").add_class("sessions-hidden")

    async def on_prompt_editor_submitted(self, event: PromptEditor.Submitted) -> None:
        await self._submit_prompt(event.value)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "send":
            await self._submit_prompt(self.query_one("#prompt", PromptEditor).text.strip())
        elif event.button.id == "new-session":
            await self.action_new_session()
        elif event.button.id == "stop":
            self.action_interrupt_turn()
        elif event.button.id == "shortcut-new":
            await self.action_new_session()
        elif event.button.id == "shortcut-older":
            await self._cycle_session(1)
        elif event.button.id == "shortcut-newer":
            await self._cycle_session(-1)
        elif event.button.id == "shortcut-sessions":
            self.action_toggle_sessions()
        elif event.button.id == "shortcut-stop":
            self.action_interrupt_turn()
        elif event.button.id == "shortcut-prompt":
            self.action_focus_prompt()
        elif event.button.id == "shortcut-quit":
            self.action_request_quit()

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        if isinstance(event.item, SessionItem) and event.item.session_id != self.current_session.session_id:
            await self._show_session(self.runtime.sessions.get(event.item.session_id))

    async def _submit_prompt(self, prompt: str) -> None:
        if not prompt:
            return
        if prompt.startswith("/"):
            self.query_one("#prompt", PromptEditor).text = ""
            await self._run_command(prompt)
            return
        session_id = self.current_session.session_id
        if session_id in self.session_turns:
            self.notify("This conversation is still working. Switch sessions to start another task.", severity="warning")
            return
        editor = self.query_one("#prompt", PromptEditor)
        editor.text = ""
        turn = TurnState(str(uuid4()), session_id, prompt)
        self.turns[turn.turn_id] = turn
        self.session_turns[session_id] = turn.turn_id
        timeline = self.query_one("#timeline", VerticalScroll)
        self._remove_empty_state()
        await timeline.mount(ConversationMessage("user", prompt), WorkflowView(turn))
        self._update_header()
        await self._refresh_session_list()
        timeline.scroll_end(animate=False)
        editor.focus()
        self._run_turn(turn)

    @work(thread=True, exit_on_error=False)
    def _run_turn(self, turn: TurnState) -> None:
        def progress(update: SessionProgress) -> None:
            try:
                self.call_from_thread(self._receive_progress, turn.turn_id, update)
            except RuntimeError:
                LOGGER.debug("TUI closed before progress delivery session_id=%s", turn.session_id)

        try:
            response = self.client.run(
                turn.session_id,
                turn.prompt,
                progress=progress,
                cancellation=turn.cancellation,
            )
        except SessionCancelled:
            try:
                self.call_from_thread(self._finish_turn, turn.turn_id, None, None, True)
            except RuntimeError:
                LOGGER.debug("TUI closed before interruption delivery session_id=%s", turn.session_id)
        except Exception as exc:
            LOGGER.error("Interactive turn failed session_id=%s error_type=%s", turn.session_id, type(exc).__name__)
            try:
                self.call_from_thread(self._finish_turn, turn.turn_id, None, f"{type(exc).__name__}: {exc}", False)
            except RuntimeError:
                LOGGER.debug("TUI closed before turn failure delivery session_id=%s", turn.session_id)
        else:
            try:
                self.call_from_thread(self._finish_turn, turn.turn_id, response, None, False)
            except RuntimeError:
                LOGGER.debug("TUI closed before turn result delivery session_id=%s", turn.session_id)

    def _receive_progress(self, turn_id: str, update: SessionProgress) -> None:
        turn = self.turns.get(turn_id)
        if turn is None or turn.done:
            return
        if update.kind == "llm_waiting":
            round_number = update.round_number or 1
            turn.status = "Understanding the request" if round_number == 1 else "Integrating capability results"
            turn.steps.append(
                WorkflowStep(
                    "model",
                    f"model-round-{round_number}",
                    "Analyze context" if round_number == 1 else "Review new evidence",
                    round_number,
                )
            )
        elif update.kind == "assistant_update":
            round_number = update.round_number or 1
            self._complete_model_step(turn, round_number, "Capability selected")
            turn.status = "Preparing capability calls"
            turn.steps.append(
                WorkflowStep(
                    "decision",
                    f"decision-{round_number}",
                    "Model decision",
                    round_number,
                    state="done",
                    result=_summarize(update.message, limit=240),
                )
            )
        elif update.kind == "capability_started":
            turn.status = f"Running {update.name or 'capability'}"
            round_number = update.round_number or 1
            self._complete_model_step(turn, round_number, "Capability selected")
            turn.steps.append(
                WorkflowStep(
                    "capability",
                    update.name or "capability",
                    update.name or "capability",
                    round_number,
                    request=_summarize(update.result, limit=240),
                )
            )
        elif update.kind == "capability_completed":
            self._complete_step(turn, update.name or "capability", "done", _summarize(update.result, limit=240))
            turn.status = "Capability result received"
        elif update.kind == "capability_failed":
            self._complete_step(turn, update.name or "capability", "failed", _summarize(update.result))
            turn.status = "Recovering from capability error"
        elif update.kind == "event_wait_started":
            event_name = _event_name(update.result)
            turn.status = f"Waiting for {event_name}"
            round_number = update.round_number or 1
            self._complete_model_step(turn, round_number, "Event selected")
            turn.steps.append(
                WorkflowStep(
                    "event",
                    update.name or "event",
                    f"Wait for {event_name}",
                    round_number,
                    request=_summarize(update.result, limit=240),
                )
            )
        elif update.kind == "event_completed":
            state = (
                "failed"
                if isinstance(update.result, dict) and (update.result.get("status") == "error" or "error" in update.result)
                else "done"
            )
            self._complete_step(turn, update.name or "event", state, _summarize(update.result))
            turn.status = "Reviewing event"
        elif update.kind == "turn_completed":
            self._complete_model_step(turn, update.round_number or 1, "Response ready")
        self._refresh_workflow(turn)
        if update.kind in {"turn_started", "event_wait_started", "event_completed"}:
            self._schedule_session_refresh()

    def _finish_turn(self, turn_id: str, response: str | None, error: str | None, interrupted: bool = False) -> None:
        turn = self.turns.get(turn_id)
        if turn is None:
            return
        turn.done = True
        turn.interrupted = interrupted
        turn.error = error
        turn.status = "Interrupted" if interrupted else "Failed" if error else "Completed"
        for step in turn.steps:
            if step.state == "running":
                step.state = "interrupted" if interrupted else "failed" if error else "done"
                if interrupted:
                    step.result = "Stopped by the user"
        self.session_turns.pop(turn.session_id, None)
        if turn.session_id == self.current_session.session_id:
            workflow = self._workflow_for(turn)
            if workflow is not None:
                if not turn.steps and error is None and not interrupted:
                    workflow.remove()
                else:
                    workflow.refresh_state()
            if error:
                self.query_one("#timeline", VerticalScroll).mount(ConversationMessage("error", error))
            elif interrupted:
                self.query_one("#timeline", VerticalScroll).mount(ConversationMessage("notice", "Turn interrupted."))
            else:
                self.query_one("#timeline", VerticalScroll).mount(ConversationMessage("assistant", response or ""))
            self.query_one("#timeline", VerticalScroll).scroll_end(animate=True)
        else:
            outcome = "interrupted" if interrupted else "failed" if error else "completed"
            self.notify(f"Conversation {turn.session_id[:8]} {outcome}.")
        self._update_header()
        self._schedule_session_refresh()

    def _schedule_session_refresh(self) -> None:
        """Coalesce runtime state changes into one sidebar refresh worker."""

        self._session_refresh_dirty = True
        if self._session_refresh_pending:
            return
        self._session_refresh_pending = True
        self.run_worker(self._drain_session_refreshes(), name="refresh-sessions")

    async def _drain_session_refreshes(self) -> None:
        try:
            while self._session_refresh_dirty:
                self._session_refresh_dirty = False
                await self._refresh_session_list()
        finally:
            self._session_refresh_pending = False

    @staticmethod
    def _complete_step(turn: TurnState, name: str, state: str, detail: str) -> None:
        for step in reversed(turn.steps):
            if step.name == name and step.state == "running":
                step.state = state
                step.result = detail
                return
        turn.steps.append(WorkflowStep("capability", name, name, 1, state=state, result=detail))

    @staticmethod
    def _complete_model_step(turn: TurnState, round_number: int, result: str) -> None:
        for step in reversed(turn.steps):
            if step.kind == "model" and step.round_number == round_number and step.state == "running":
                step.state = "done"
                step.result = result
                return

    def _refresh_workflow(self, turn: TurnState) -> None:
        if turn.session_id != self.current_session.session_id:
            return
        workflow = self._workflow_for(turn)
        if workflow is not None:
            workflow.refresh_state()
            self.query_one("#timeline", VerticalScroll).scroll_end(animate=False)
        self._update_header()

    def _workflow_for(self, turn: TurnState) -> WorkflowView | None:
        return next((view for view in self.query(WorkflowView) if view.state.turn_id == turn.turn_id), None)

    async def _show_session(self, session: ConversationSession, *, focus_prompt: bool = True) -> None:
        self.client.select_session(session.session_id)
        self.current_session = session
        timeline = self.query_one("#timeline", VerticalScroll)
        await timeline.remove_children()
        messages = tuple(_visible_messages(session.history))
        if messages:
            await timeline.mount(*(ConversationMessage(role, content) for role, content in messages))
        turn_id = self.session_turns.get(session.session_id)
        if turn_id is not None:
            turn = self.turns[turn_id]
            if not messages or messages[-1] != ("user", turn.prompt):
                await timeline.mount(ConversationMessage("user", turn.prompt))
            await timeline.mount(WorkflowView(turn))
        if not messages and turn_id is None:
            config = self.runtime.config
            capabilities = len(self.runtime.registry.descriptors())
            events_count = len(self.runtime.events.descriptors())
            welcome = (
                "Start a conversation. Pivot can reason, inspect measurements, run capabilities, and wait for events.\n\n"
                f"Provider  {config.provider.name}\n"
                f"Model     {config.provider.model}\n"
                f"Endpoint  {safe_endpoint(config.provider.api_base)}\n"
                f"Tools     {capabilities} capabilities, {events_count} events"
                if self.show_welcome
                else "Start a conversation."
            )
            await timeline.mount(
                Static(
                    welcome,
                    id="empty-state",
                    markup=False,
                )
            )
        self._update_header()
        await self._refresh_session_list()
        timeline.scroll_end(animate=False)
        if focus_prompt:
            self.query_one("#prompt", PromptEditor).focus()

    async def _refresh_session_list(self) -> None:
        current_id = self.current_session.session_id
        known = list(self._session_ids)
        for session in self.runtime.sessions.sessions():
            if session.session_id not in known:
                known.append(session.session_id)
        sessions = {session_id: self.runtime.sessions.get(session_id) for session_id in known}
        state_priority = {
            SessionState.RUNNING: 0,
            SessionState.PENDING: 1,
            SessionState.READY: 2,
        }
        known.sort(
            key=lambda session_id: (
                state_priority[sessions[session_id].state],
                -sessions[session_id].last_active_at,
                session_id,
            )
        )
        self._session_ids = known
        view = self.query_one("#session-list", ListView)
        await view.clear()
        await view.extend(
            SessionItem(session_id, current=session_id == current_id, state=sessions[session_id].state)
            for session_id in known
        )
        if current_id in known:
            view.index = known.index(current_id)

    async def _refresh_dependency_list(self) -> None:
        view = self.query_one("#dependency-list", VerticalScroll)
        await view.remove_children()
        manager = self.runtime.dependencies
        statuses = manager.statuses() if manager is not None else ()
        if statuses:
            await view.mount(*(DependencyItem(status) for status in statuses))
        else:
            await view.mount(Static("No dependencies", id="dependency-empty", markup=False))

    def _request_dependency_refresh(self) -> None:
        self._refresh_dependencies()

    @work(thread=True, exclusive=True, group="dependency-status", exit_on_error=False)
    def _refresh_dependencies(self) -> None:
        manager = self.runtime.dependencies
        if manager is None:
            return
        manager.statuses(refresh=True)
        try:
            self.call_from_thread(self._refresh_dependency_list)
        except RuntimeError:
            LOGGER.debug("TUI closed before dependency status delivery")

    def _discover_session_ids(self) -> list[str]:
        current_id = self.current_session.session_id
        managed = [session.session_id for session in self.runtime.sessions.sessions()]
        persisted: list[tuple[float, str]] = []
        memory_root = self.runtime.config.instance_path / "memory"
        if memory_root.is_dir():
            try:
                paths = tuple(memory_root.iterdir())
            except OSError as exc:
                LOGGER.warning("Unable to discover session memory error_type=%s", type(exc).__name__)
                paths = ()
            for path in paths:
                try:
                    if path.is_dir() and _is_uuid(path.name) and (path / "history.jsonl").is_file():
                        persisted.append(((path / "history.jsonl").stat().st_mtime, path.name))
                except OSError:
                    LOGGER.debug("Session memory entry became unavailable path=%s", path)
        persisted_ids = [session_id for _, session_id in sorted(persisted, reverse=True)]
        ephemeral = [session_id for session_id in managed if session_id not in persisted_ids]
        if current_id in ephemeral:
            ephemeral.remove(current_id)
            ephemeral.insert(0, current_id)
        return ephemeral + persisted_ids

    def _remove_empty_state(self) -> None:
        empty = self.query("#empty-state")
        if empty:
            empty.first().remove()

    def _update_header(self) -> None:
        session_id = self.current_session.session_id
        self.query_one("#conversation-title", Static).update(f"Conversation  {session_id[:8]}")
        turn_id = self.session_turns.get(session_id)
        state = self.turns[turn_id].status if turn_id else "Ready"
        self.query_one("#conversation-state", Static).update(state)
        self.query_one("#send", Button).display = turn_id is None
        self.query_one("#stop", Button).display = turn_id is not None

    async def _run_command(self, value: str) -> None:
        command, _, argument = value.partition(" ")
        command = command.lower()
        if command in {"/exit", "/quit"}:
            self.action_request_quit()
        elif command == "/new":
            await self.action_new_session()
        elif command in {"/next", "/n"}:
            await self._cycle_session(-1)
        elif command in {"/prev", "/previous", "/p"}:
            await self._cycle_session(1)
        elif command in {"/stop", "/interrupt"}:
            self.action_interrupt_turn()
        elif command in {"/sessions", "/sidebar"}:
            self.action_toggle_sessions()
        elif command == "/session":
            await self._append_notice(f"Current conversation: {self.current_session.session_id}")
        elif command == "/switch":
            await self._switch_session_prefix(argument.strip())
        elif command == "/help":
            await self._append_notice(
                "Commands: /new, /next, /prev, /switch <id>, /session, /sessions, /stop, /help, /exit\n"
                "Keys: Ctrl+N new, Ctrl+Left/Right navigate sessions, Ctrl+G stop, Ctrl+B sessions, Ctrl+L prompt, Ctrl+Q quit"
            )
        else:
            await self._append_notice(f"Unknown command: {command}. Enter /help for available commands.", error=True)
        self.query_one("#prompt", PromptEditor).focus()

    async def _append_notice(self, content: str, *, error: bool = False) -> None:
        self._remove_empty_state()
        timeline = self.query_one("#timeline", VerticalScroll)
        await timeline.mount(ConversationMessage("error" if error else "notice", content))
        timeline.scroll_end(animate=False)

    async def _switch_session_prefix(self, prefix: str) -> None:
        if not prefix:
            await self._append_notice("Usage: /switch <conversation-id-prefix>", error=True)
            return
        matches = [session_id for session_id in self._session_ids if session_id.startswith(prefix)]
        if len(matches) != 1:
            await self._append_notice(
                "Conversation prefix is ambiguous." if matches else f"Conversation not found: {prefix}",
                error=True,
            )
            return
        await self._show_session(self.runtime.sessions.get(matches[0]))

    async def _cycle_session(self, offset: int) -> None:
        if len(self._session_ids) < 2:
            self.notify("There is only one conversation.")
            return
        current_index = self._session_ids.index(self.current_session.session_id)
        session_id = self._session_ids[(current_index + offset) % len(self._session_ids)]
        await self._show_session(self.runtime.sessions.get(session_id), focus_prompt=False)
        self._focus_current_session_item()

    async def action_new_session(self) -> None:
        session = self.client.create_session()
        self._session_ids.insert(0, session.session_id)
        await self._show_session(session)

    async def action_previous_session(self) -> None:
        if not self._session_list_has_focus():
            self._focus_current_session_item()
            return
        await self._cycle_session(1)

    async def action_next_session(self) -> None:
        if not self._session_list_has_focus():
            self._focus_current_session_item()
            return
        await self._cycle_session(-1)

    def action_toggle_sessions(self) -> None:
        self.query_one("#body").toggle_class("sessions-hidden")

    def _session_list_has_focus(self) -> bool:
        return self.query_one("#session-list", ListView).has_focus_within

    def _focus_current_session_item(self) -> None:
        body = self.query_one("#body")
        body.remove_class("sessions-hidden")
        session_list = self.query_one("#session-list", ListView)
        if self.current_session.session_id in self._session_ids:
            session_list.index = self._session_ids.index(self.current_session.session_id)
        session_list.focus()

    def action_interrupt_turn(self) -> None:
        turn_id = self.session_turns.get(self.current_session.session_id)
        if turn_id is None:
            self.notify("This conversation is not running.")
            return
        turn = self.turns[turn_id]
        if turn.cancellation.is_cancelled():
            self.notify("Interruption is already pending.")
            return
        turn.cancellation.cancel()
        turn.status = "Stopping at the next safe point"
        self._refresh_workflow(turn)
        self.notify("Interruption requested.", severity="warning")

    def action_focus_prompt(self) -> None:
        self.query_one("#prompt", PromptEditor).focus()

    def action_scroll_timeline_up(self) -> None:
        self.query_one("#timeline", VerticalScroll).scroll_page_up(animate=False)

    def action_scroll_timeline_down(self) -> None:
        self.query_one("#timeline", VerticalScroll).scroll_page_down(animate=False)

    def action_request_quit(self) -> None:
        if not self.session_turns or self._quit_armed:
            self.exit()
            return
        self._quit_armed = True
        self.notify("Agent work is still running. Press Ctrl+Q again to quit immediately.", severity="warning", timeout=3)
        self.set_timer(3, self._disarm_quit)

    def _disarm_quit(self) -> None:
        self._quit_armed = False


def run_tui(client: PivotClient, session: ConversationSession, *, show_welcome: bool = True) -> None:
    """Run the interactive Textual application until the user exits."""

    LOGGER.info("Textual interface started session_id=%s", session.session_id)
    PivotApp(client, session, show_welcome=show_welcome).run()
    LOGGER.info("Textual interface stopped session_id=%s", session.session_id)


def _visible_messages(messages: Iterable[Message]) -> Iterable[tuple[str, str]]:
    for message in messages:
        if message.role == "user" and message.content:
            yield "user", message.content
        elif message.role == "assistant" and not message.tool_calls:
            yield "assistant", message.content or ""


def _summarize(value: Any, *, limit: int = 120) -> str:
    if value is None:
        return ""
    if isinstance(value, dict) and isinstance(value.get("message"), str):
        text = value["message"]
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
        except (TypeError, ValueError):
            text = str(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _event_name(value: Any) -> str:
    if isinstance(value, dict) and isinstance(value.get("event"), str):
        return value["event"]
    return "event"


def _is_uuid(value: str) -> bool:
    try:
        return str(UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


__all__ = ["PivotApp", "PromptEditor", "run_tui"]
