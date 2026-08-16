"""Textual terminal interface for the persistent pivot main agent."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal
from uuid import uuid4

from rich.markup import escape
from textual import events, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, HorizontalScroll, Vertical, VerticalScroll
from textual.message import Message as TextualMessage
from textual.theme import Theme
from textual.widgets import (
    Button,
    Label,
    ListItem,
    ListView,
    LoadingIndicator,
    Markdown,
    Static,
    TextArea,
)

from .activation import ActivationProgress, AgentCancelled, CancellationToken
from .agents import AgentRecord
from .dependencies import DependencyState, DependencyStatus
from .models import Message
from .runtime import PivotClient
from .stimuli import StimulusState
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


class AgentMessage(Vertical):
    """One user, assistant, or error entry in the agent timeline."""

    def __init__(self, role: str, content: str) -> None:
        super().__init__(classes=f"message {role}")
        self.role = role
        self.content = content

    def compose(self) -> ComposeResult:
        labels = {
            "user": "YOU",
            "assistant": "PIVOT",
            "notice": "SYSTEM",
            "error": "ERROR",
        }
        yield Label(labels.get(self.role, self.role.upper()), classes="message-label")
        if self.role == "assistant":
            yield Markdown(self.content or "_(empty response)_", classes="message-body")
        else:
            yield Static(self.content, classes="message-body", markup=False)


@dataclass(slots=True)
class WorkflowStep:
    """One inspectable model, capability, or event phase in a turn."""

    kind: Literal[
        "model",
        "decision",
        "capability",
        "event",
        "executor",
        "control",
        "memory",
        "agent",
    ]
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
    agent_id: str
    prompt: str
    status: str = "Thinking"
    steps: list[WorkflowStep] = field(default_factory=list)
    cancellation: CancellationToken = field(default_factory=CancellationToken)
    done: bool = False
    interrupted: bool = False
    error: str | None = None
    stimulus_id: str | None = None


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
        kinds = {
            "model": "MODEL",
            "decision": "DECISION",
            "capability": "CAPABILITY",
            "event": "EVENT",
            "executor": "EXECUTOR",
            "control": "CONTROL",
            "memory": "MEMORY",
            "agent": "WORKER",
        }
        rows = []
        for index, step in enumerate(self.state.steps, 1):
            heading = f"{index:02d}  {kinds[step.kind]}  [bold]{escape(step.label)}[/]  [dim]round {step.round_number}[/]"
            rows.append(f"{symbols[step.state]}  {heading}")
            if step.request:
                rows.append(f"    [dim]Request[/]  {escape(step.request)}")
            if step.result:
                rows.append(f"    [dim]Result[/]   {escape(step.result)}")
        self.query_one(".workflow-steps", Static).update("\n".join(rows))


class AgentItem(ListItem):
    """Read-only main or worker agent lifecycle entry."""

    def __init__(self, record: AgentRecord) -> None:
        self.record = record
        super().__init__()

    def compose(self) -> ComposeResult:
        yield Label(self._label())

    def _label(self) -> str:
        snapshot = self.record.as_dict()
        state = snapshot["state"]
        marker = {
            "running": "[bold $success]●[/]",
            "pending": "[bold $warning]●[/]",
            "completed": "[bold $success]√[/]",
            "failed": "[bold $error]×[/]",
            "cancelled": "[bold $warning]×[/]",
        }.get(state, "○")
        role = "MAIN" if snapshot["role"] == "main" else "WORKER"
        return f"{marker}  {escape(snapshot['name'])}  [dim]{role}[/]"


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
    """Main-agent terminal client with inspectable delegated workers."""

    TITLE = "pivot"
    SUB_TITLE = "agent runtime"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("ctrl+q", "request_quit", "Quit", priority=True),
        Binding("ctrl+b", "toggle_agents", "Agents", priority=True),
        Binding("ctrl+g", "interrupt_turn", "Stop", priority=True),
        Binding("ctrl+l", "focus_prompt", "Prompt", priority=True),
        Binding(
            "ctrl+up", "scroll_timeline_up", "Scroll up", show=False, priority=True
        ),
        Binding(
            "ctrl+down",
            "scroll_timeline_down",
            "Scroll down",
            show=False,
            priority=True,
        ),
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

    #agents-pane {
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

    #agent-list {
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

    AgentItem {
        height: 3;
        padding: 1;
        color: $text-muted;
    }

    AgentItem:hover {
        background: $primary;
        color: $primary-ink;
    }

    AgentItem.-highlight {
        background: $primary;
        color: $primary-ink;
    }

    #main-pane {
        width: 1fr;
        height: 1fr;
        background: $background;
    }

    #agent-header {
        height: 4;
        padding: 1 2;
        border-bottom: solid $border-blurred;
    }

    #agent-title {
        width: 1fr;
        color: $foreground;
        text-style: bold;
    }

    #agent-state {
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

    AgentMessage {
        width: 100%;
        height: auto;
        margin: 0 0 1 0;
        padding: 0 1;
    }

    AgentMessage.user {
        border-left: thick $secondary;
        background: $surface;
    }

    AgentMessage.assistant {
        border-left: thick $primary;
    }

    AgentMessage.notice {
        border-left: thick $success;
        background: $positive-surface;
    }

    AgentMessage.error {
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

    .agents-hidden #agents-pane {
        display: none;
    }

    .compact #timeline {
        padding: 1 1 2 1;
    }

    .compact #runtime-meta, .compact #send {
        display: none;
    }
    """

    def __init__(self, client: PivotClient, *, show_welcome: bool = True) -> None:
        super().__init__()
        self.register_theme(PIVOT_THEME)
        self.theme = PIVOT_THEME.name
        self.client = client
        self.runtime = client.runtime
        self.main_agent_id = client.main_agent_id
        self.show_welcome = show_welcome
        self.turns: dict[str, TurnState] = {}
        self.agent_activations: dict[str, list[str]] = {}
        self._quit_armed = False
        self._agent_refresh_pending = False
        self._agent_refresh_dirty = False
        self._control_unsubscribe: Any = None
        self.reload_requested = False

    def compose(self) -> ComposeResult:
        config = self.runtime.config
        with Horizontal(id="topbar"):
            yield Static("PIVOT", id="brand")
            yield Static(
                f"{config.provider.name}  /  {config.provider.model}", id="runtime-meta"
            )
        with Horizontal(id="body"):
            with Vertical(id="agents-pane"):
                yield Label("AGENTS", classes="section-title")
                yield ListView(id="agent-list")
                with Vertical(id="dependencies"):
                    yield Label("DEPENDENCIES", classes="section-title")
                    yield VerticalScroll(id="dependency-list")
            with Vertical(id="main-pane"):
                with Horizontal(id="agent-header"):
                    yield Static("Agent", id="agent-title")
                    yield Static("Ready", id="agent-state")
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
            yield Button(
                "Agents (Ctrl+B)", id="shortcut-agents", classes="shortcut-button"
            )
            yield Button("Stop (Ctrl+G)", id="shortcut-stop", classes="shortcut-button")
            yield Button(
                "Prompt (Ctrl+L)", id="shortcut-prompt", classes="shortcut-button"
            )
            yield Button("Quit (Ctrl+Q)", id="shortcut-quit", classes="shortcut-button")

    async def on_mount(self) -> None:
        self._control_unsubscribe = self.client.control.subscribe(
            self._on_control_event
        )
        compact = self.size.width < 90
        self.set_class(compact, "compact")
        self.query_one("#body").set_class(compact, "agents-hidden")
        await self._refresh_agent_list()
        await self._refresh_dependency_list()
        if (
            self.runtime.dependencies is not None
            and self.runtime.dependencies.descriptors()
        ):
            self.set_interval(5.0, self._request_dependency_refresh)
        await self._show_main_agent()
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
        if event == "reload_requested":
            self.reload_requested = True
            self.exit()
            return
        agent_id = payload.get("target_agent_id")
        if event == "stimulus_changed" and payload.get("state") in {
            "completed",
            "failed",
            "cancelled",
        }:
            self._schedule_agent_refresh()
            correlation_id = payload.get("correlation_id")
            if (
                agent_id == self.main_agent_id
                and agent_id not in self.agent_activations
                and correlation_id not in self.turns
            ):
                self.call_later(self._show_main_agent, focus_prompt=False)

    def on_resize(self, event: events.Resize) -> None:
        compact = event.size.width < 90
        self.set_class(compact, "compact")
        if compact:
            self.query_one("#body").add_class("agents-hidden")

    async def on_prompt_editor_submitted(self, event: PromptEditor.Submitted) -> None:
        await self._submit_prompt(event.value)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "send":
            await self._submit_prompt(
                self.query_one("#prompt", PromptEditor).text.strip()
            )
        elif event.button.id == "stop":
            self.action_interrupt_turn()
        elif event.button.id == "shortcut-agents":
            self.action_toggle_agents()
        elif event.button.id == "shortcut-stop":
            self.action_interrupt_turn()
        elif event.button.id == "shortcut-prompt":
            self.action_focus_prompt()
        elif event.button.id == "shortcut-quit":
            self.action_request_quit()

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        if isinstance(event.item, AgentItem):
            snapshot = event.item.record.as_dict()
            scope = (
                ", ".join(snapshot["capabilities"] + snapshot["events"])
                or "no assigned resources"
            )
            self.notify(f"{snapshot['name']}: {snapshot['state']} · {scope}")

    async def _submit_prompt(self, prompt: str) -> None:
        if not prompt:
            return
        if prompt.startswith("/"):
            self.query_one("#prompt", PromptEditor).text = ""
            await self._run_command(prompt)
            return
        agent_id = self.main_agent_id
        editor = self.query_one("#prompt", PromptEditor)
        editor.text = ""
        turn = TurnState(str(uuid4()), agent_id, prompt)
        self.turns[turn.turn_id] = turn
        self.agent_activations.setdefault(agent_id, []).append(turn.turn_id)
        timeline = self.query_one("#timeline", VerticalScroll)
        self._remove_empty_state()
        await timeline.mount(AgentMessage("user", prompt), WorkflowView(turn))
        self._update_header()
        await self._refresh_agent_list()
        timeline.scroll_end(animate=False)
        editor.focus()
        self._run_turn(turn)

    @work(thread=True, exit_on_error=False)
    def _run_turn(self, turn: TurnState) -> None:
        def progress(update: ActivationProgress) -> None:
            try:
                self.call_from_thread(self._receive_progress, turn.turn_id, update)
            except RuntimeError:
                LOGGER.debug(
                    "TUI closed before progress delivery agent_id=%s", turn.agent_id
                )

        try:
            stimulus_id = self.client.inject(
                {
                    "kind": "command",
                    "source": "tui",
                    "payload": {"content": turn.prompt},
                    "correlation_id": turn.turn_id,
                },
                progress=progress,
                cancellation=turn.cancellation,
            )
            turn.stimulus_id = stimulus_id
            completed = self.client.wait_stimulus(stimulus_id)
            if completed.state == StimulusState.CANCELLED:
                raise AgentCancelled(completed.error or "Stimulus was cancelled")
            if completed.state == StimulusState.FAILED:
                raise RuntimeError(completed.error or "Stimulus failed")
            response = completed.response or ""
        except AgentCancelled:
            try:
                self.call_from_thread(self._finish_turn, turn.turn_id, None, None, True)
            except RuntimeError:
                LOGGER.debug(
                    "TUI closed before interruption delivery agent_id=%s", turn.agent_id
                )
        except Exception as exc:
            LOGGER.error(
                "Interactive turn failed agent_id=%s error_type=%s",
                turn.agent_id,
                type(exc).__name__,
            )
            try:
                self.call_from_thread(
                    self._finish_turn,
                    turn.turn_id,
                    None,
                    f"{type(exc).__name__}: {exc}",
                    False,
                )
            except RuntimeError:
                LOGGER.debug(
                    "TUI closed before turn failure delivery agent_id=%s", turn.agent_id
                )
        else:
            try:
                self.call_from_thread(
                    self._finish_turn, turn.turn_id, response, None, False
                )
            except RuntimeError:
                LOGGER.debug(
                    "TUI closed before turn result delivery agent_id=%s", turn.agent_id
                )

    def _receive_progress(self, turn_id: str, update: ActivationProgress) -> None:
        turn = self.turns.get(turn_id)
        if turn is None or turn.done:
            return
        if update.kind == "llm_waiting":
            round_number = update.round_number or 1
            turn.status = (
                "Understanding the request"
                if round_number == 1
                else "Integrating capability results"
            )
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
            self._complete_step(
                turn,
                update.name or "capability",
                "done",
                _summarize(update.result, limit=240),
            )
            turn.status = "Capability result received"
        elif update.kind == "capability_failed":
            self._complete_step(
                turn, update.name or "capability", "failed", _summarize(update.result)
            )
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
                if isinstance(update.result, dict)
                and (update.result.get("status") == "error" or "error" in update.result)
                else "done"
            )
            self._complete_step(
                turn, update.name or "event", state, _summarize(update.result)
            )
            turn.status = "Reviewing event"
        elif update.kind in {"executor_started", "control_started", "memory_started"}:
            kind = {
                "executor_started": "executor",
                "control_started": "control",
                "memory_started": "memory",
            }[update.kind]
            round_number = update.round_number or 1
            self._complete_model_step(turn, round_number, f"{kind.title()} selected")
            turn.steps.append(
                WorkflowStep(
                    kind,
                    update.name or kind,
                    update.name or kind,
                    round_number,
                    request=_summarize(update.result, limit=240),
                )
            )
            turn.status = f"Running {update.name or kind}"
        elif update.kind in {
            "executor_completed",
            "control_completed",
            "memory_completed",
        }:
            self._complete_step(
                turn,
                update.name or "action",
                "done",
                _summarize(update.result, limit=240),
            )
            turn.status = "Action result received"
        elif update.kind in {"executor_failed", "control_failed", "memory_failed"}:
            self._complete_step(
                turn, update.name or "action", "failed", _summarize(update.result)
            )
            turn.status = "Recovering from action error"
        elif update.kind == "agent_started":
            agent = _agent_snapshot(update.result)
            turn.steps.append(
                WorkflowStep(
                    "agent",
                    agent.get("agent_id", update.name or "worker"),
                    update.name or "worker",
                    update.round_number or 1,
                    request=_summarize(
                        {
                            "task": agent.get("task"),
                            "capabilities": agent.get("capabilities"),
                            "events": agent.get("events"),
                        },
                        limit=240,
                    ),
                )
            )
            turn.status = f"Delegated to {update.name or 'worker'}"
        elif update.kind == "agent_progress":
            turn.status = f"{update.name or 'Worker'}: {update.message}"
        elif update.kind in {"agent_completed", "agent_failed"}:
            agent = _agent_snapshot(update.result)
            self._complete_step(
                turn,
                agent.get("agent_id", update.name or "worker"),
                "done" if update.kind == "agent_completed" else "failed",
                _summarize(
                    agent.get("report") or agent.get("error") or update.message,
                    limit=240,
                ),
            )
            turn.status = (
                "Worker report received"
                if update.kind == "agent_completed"
                else "Worker failed"
            )
        elif update.kind == "activation_completed":
            self._complete_model_step(turn, update.round_number or 1, "Response ready")
        self._refresh_workflow(turn)
        if update.kind in {
            "activation_started",
            "event_wait_started",
            "event_completed",
            "agent_started",
            "agent_progress",
            "agent_completed",
            "agent_failed",
        }:
            self._schedule_agent_refresh()

    def _finish_turn(
        self,
        turn_id: str,
        response: str | None,
        error: str | None,
        interrupted: bool = False,
    ) -> None:
        turn = self.turns.get(turn_id)
        if turn is None:
            return
        turn.done = True
        turn.interrupted = interrupted
        turn.error = error
        turn.status = (
            "Interrupted" if interrupted else "Failed" if error else "Completed"
        )
        for step in turn.steps:
            if step.state == "running":
                step.state = (
                    "interrupted" if interrupted else "failed" if error else "done"
                )
                if interrupted:
                    step.result = "Stopped by the user"
        queued = self.agent_activations.get(turn.agent_id, [])
        if turn.turn_id in queued:
            queued.remove(turn.turn_id)
        if not queued:
            self.agent_activations.pop(turn.agent_id, None)
        if turn.agent_id == self.main_agent_id:
            workflow = self._workflow_for(turn)
            if workflow is not None:
                if not turn.steps and error is None and not interrupted:
                    workflow.remove()
                else:
                    workflow.refresh_state()
            if error:
                self.query_one("#timeline", VerticalScroll).mount(
                    AgentMessage("error", error)
                )
            elif interrupted:
                self.query_one("#timeline", VerticalScroll).mount(
                    AgentMessage("notice", "Turn interrupted.")
                )
            else:
                self.query_one("#timeline", VerticalScroll).mount(
                    AgentMessage("assistant", response or "")
                )
            self.query_one("#timeline", VerticalScroll).scroll_end(animate=True)
        else:
            outcome = (
                "interrupted" if interrupted else "failed" if error else "completed"
            )
            self.notify(f"Agent {turn.agent_id[:8]} {outcome}.")
        self._update_header()
        self._schedule_agent_refresh()

    def _schedule_agent_refresh(self) -> None:
        """Coalesce runtime state changes into one sidebar refresh worker."""

        self._agent_refresh_dirty = True
        if self._agent_refresh_pending:
            return
        self._agent_refresh_pending = True
        self.run_worker(self._drain_agent_refreshes(), name="refresh-agents")

    async def _drain_agent_refreshes(self) -> None:
        try:
            while self._agent_refresh_dirty:
                self._agent_refresh_dirty = False
                await self._refresh_agent_list()
        finally:
            self._agent_refresh_pending = False

    @staticmethod
    def _complete_step(turn: TurnState, name: str, state: str, detail: str) -> None:
        for step in reversed(turn.steps):
            if step.name == name and step.state == "running":
                step.state = state
                step.result = detail
                return
        turn.steps.append(
            WorkflowStep("capability", name, name, 1, state=state, result=detail)
        )

    @staticmethod
    def _complete_model_step(turn: TurnState, round_number: int, result: str) -> None:
        for step in reversed(turn.steps):
            if (
                step.kind == "model"
                and step.round_number == round_number
                and step.state == "running"
            ):
                step.state = "done"
                step.result = result
                return

    def _refresh_workflow(self, turn: TurnState) -> None:
        if turn.agent_id != self.main_agent_id:
            return
        workflow = self._workflow_for(turn)
        if workflow is not None:
            workflow.refresh_state()
            self.query_one("#timeline", VerticalScroll).scroll_end(animate=False)
        self._update_header()

    def _workflow_for(self, turn: TurnState) -> WorkflowView | None:
        return next(
            (
                view
                for view in self.query(WorkflowView)
                if view.state.turn_id == turn.turn_id
            ),
            None,
        )

    async def _show_main_agent(self, *, focus_prompt: bool = True) -> None:
        agent_id = self.main_agent_id
        timeline = self.query_one("#timeline", VerticalScroll)
        await timeline.remove_children()
        messages = tuple(_visible_messages(self.client.main_history()))
        if messages:
            await timeline.mount(
                *(AgentMessage(role, content) for role, content in messages)
            )
        turn_ids = self.agent_activations.get(agent_id, [])
        for turn_id in turn_ids:
            turn = self.turns[turn_id]
            if not messages or messages[-1] != ("user", turn.prompt):
                await timeline.mount(AgentMessage("user", turn.prompt))
            await timeline.mount(WorkflowView(turn))
        if not messages and not turn_ids:
            config = self.runtime.config
            capabilities = len(self.runtime.registry.descriptors())
            events_count = len(self.runtime.events.descriptors())
            introduction = "Talk to the persistent main agent. It can solve requests or delegate scoped work.\n\n"
            welcome = (
                introduction
                + f"Provider  {config.provider.name}\n"
                + f"Model     {config.provider.model}\n"
                + f"Endpoint  {safe_endpoint(config.provider.api_base)}\n"
                + f"Tools     {capabilities} capabilities, {events_count} events"
                if self.show_welcome
                else introduction.strip()
            )
            await timeline.mount(
                Static(
                    welcome,
                    id="empty-state",
                    markup=False,
                )
            )
        self._update_header()
        await self._refresh_agent_list()
        timeline.scroll_end(animate=False)
        if focus_prompt:
            self.query_one("#prompt", PromptEditor).focus()

    async def _refresh_agent_list(self) -> None:
        view = self.query_one("#agent-list", ListView)
        await view.clear()
        records = self.runtime.agents.records()
        await view.extend(AgentItem(record) for record in records)
        if records:
            view.index = 0

    async def _refresh_dependency_list(self) -> None:
        view = self.query_one("#dependency-list", VerticalScroll)
        await view.remove_children()
        manager = self.runtime.dependencies
        statuses = manager.statuses() if manager is not None else ()
        if statuses:
            await view.mount(*(DependencyItem(status) for status in statuses))
        else:
            await view.mount(
                Static("No dependencies", id="dependency-empty", markup=False)
            )

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

    def _remove_empty_state(self) -> None:
        empty = self.query("#empty-state")
        if empty:
            empty.first().remove()

    def _update_header(self) -> None:
        agent_id = self.main_agent_id
        title = "Main Agent"
        self.query_one("#agent-title", Static).update(title)
        turn_ids = self.agent_activations.get(agent_id, [])
        state = self.turns[turn_ids[0]].status if turn_ids else "Ready"
        if len(turn_ids) > 1:
            state += f"  ·  {len(turn_ids) - 1} queued"
        self.query_one("#agent-state", Static).update(state)
        self.query_one("#send", Button).display = True
        self.query_one("#stop", Button).display = bool(turn_ids)

    async def _run_command(self, value: str) -> None:
        command, _, _argument = value.partition(" ")
        command = command.lower()
        if command in {"/exit", "/quit"}:
            self.action_request_quit()
        elif command in {"/stop", "/interrupt"}:
            self.action_interrupt_turn()
        elif command in {"/agents", "/sidebar"}:
            self.action_toggle_agents()
        elif command == "/agent":
            await self._append_notice(f"Main agent: {self.main_agent_id}")
        elif command == "/help":
            await self._append_notice(
                "Commands: /agents, /agent, /stop, /help, /exit\n"
                "Keys: Ctrl+G stop, Ctrl+B agents, Ctrl+L prompt, Ctrl+Q quit"
            )
        else:
            await self._append_notice(
                f"Unknown command: {command}. Enter /help for available commands.",
                error=True,
            )
        self.query_one("#prompt", PromptEditor).focus()

    async def _append_notice(self, content: str, *, error: bool = False) -> None:
        self._remove_empty_state()
        timeline = self.query_one("#timeline", VerticalScroll)
        await timeline.mount(AgentMessage("error" if error else "notice", content))
        timeline.scroll_end(animate=False)

    def action_toggle_agents(self) -> None:
        self.query_one("#body").toggle_class("agents-hidden")

    def _focus_main_agent_item(self) -> None:
        body = self.query_one("#body")
        body.remove_class("agents-hidden")
        agent_list = self.query_one("#agent-list", ListView)
        agent_list.index = 0
        agent_list.focus()

    def action_interrupt_turn(self) -> None:
        turn_ids = self.agent_activations.get(self.main_agent_id, [])
        if not turn_ids:
            self.notify("The main agent is not running.")
            return
        turn = self.turns[turn_ids[0]]
        if turn.cancellation.is_cancelled():
            self.notify("Interruption is already pending.")
            return
        turn.cancellation.cancel()
        if turn.stimulus_id is not None:
            self.client.control.cancel_stimulus(turn.stimulus_id)
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
        if not self.agent_activations or self._quit_armed:
            self.exit()
            return
        self._quit_armed = True
        self.notify(
            "Agent work is still running. Press Ctrl+Q again to quit immediately.",
            severity="warning",
            timeout=3,
        )
        self.set_timer(3, self._disarm_quit)

    def _disarm_quit(self) -> None:
        self._quit_armed = False


def run_tui(client: PivotClient, *, show_welcome: bool = True) -> bool:
    """Run the TUI and report whether the host should rebuild the runtime."""

    LOGGER.info("Textual interface started agent_id=%s", client.main_agent_id)
    app = PivotApp(client, show_welcome=show_welcome)
    app.run()
    LOGGER.info("Textual interface stopped agent_id=%s", client.main_agent_id)
    return app.reload_requested


def _visible_messages(messages: Iterable[Message]) -> Iterable[tuple[str, str]]:
    for message in messages:
        if message.role == "user" and message.content:
            yield "user", _display_content(message.content)
        elif (
            message.role == "system"
            and isinstance(message.content, str)
            and message.content.startswith("pivot stimulus:\n")
        ):
            try:
                stimulus = json.loads(message.content.split("\n", 1)[1])
            except (json.JSONDecodeError, IndexError):
                yield "notice", message.content
            else:
                kind = str(stimulus.get("kind", "stimulus")).upper()
                source = str(stimulus.get("source", "unknown"))
                payload = _summarize(stimulus.get("payload", {}), limit=240)
                yield "notice", f"{kind} · {source}\n{payload}"
        elif message.role == "assistant" and not message.tool_calls:
            yield "assistant", _display_content(message.content)


def _display_content(content: Any) -> str:
    """Render text content and represent multimodal parts without embedding binary data."""

    if isinstance(content, str):
        return content
    if isinstance(content, (tuple, list)):
        labels = []
        for part in content:
            if isinstance(part, dict):
                part_type = part.get("type", "content")
                labels.append(f"[{part_type} attached]")
        return " ".join(labels) or "[multimodal content]"
    return str(content or "")


def _summarize(value: Any, *, limit: int = 120) -> str:
    if value is None:
        return ""
    if isinstance(value, dict) and isinstance(value.get("message"), str):
        text = value["message"]
    else:
        try:
            text = json.dumps(
                value, ensure_ascii=False, default=str, separators=(",", ":")
            )
        except (TypeError, ValueError):
            text = str(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _event_name(value: Any) -> str:
    if isinstance(value, dict) and isinstance(value.get("event"), str):
        return value["event"]
    return "event"


def _agent_snapshot(value: Any) -> dict[str, Any]:
    if isinstance(value, dict) and isinstance(value.get("agent"), dict):
        return dict(value["agent"])
    return {}


__all__ = [
    "AgentItem",
    "AgentMessage",
    "DependencyItem",
    "PivotApp",
    "PromptEditor",
    "WorkflowView",
    "run_tui",
]
