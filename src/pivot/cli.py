"""Command-line entry point for the initial pivot runtime."""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from typing import TextIO

from .capabilities import CapabilityRegistry
from .capabilities.discovery import register_workspace_capabilities
from .config import ConfigurationError, PivotConfig
from .events import EventPool, EventScriptRunner, load_event_scripts_isolated
from .llm import LiteLLMClient
from .logging import configure_logging
from .memory import TextMemory
from .session import ConversationSession, SessionManager
from .ui import RuntimeSummary, render_banner, safe_endpoint

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Runtime:
    """Fully assembled runtime dependencies used by one CLI process."""

    config: PivotConfig
    registry: CapabilityRegistry
    events: EventPool
    sessions: SessionManager


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pivot", description="Run a pivot agent conversation")
    parser.add_argument("--workspace", help="Path to the pivot workspace (or set PIVOT_WORKSPACE_PATH)")
    parser.add_argument("--session", help="Conversation UUID to resume; omitted creates a new conversation")
    parser.add_argument("--no-banner", action="store_true", help="Suppress the startup logo and runtime summary")
    parser.add_argument("message", nargs="?", help="One user message; omit for interactive mode or stdin")
    return parser


def build_runtime(config: PivotConfig) -> Runtime:
    """Build capability, event, LLM, memory, and session services."""

    registry = CapabilityRegistry()
    environment_root = config.workspace_path / "environment"
    register_workspace_capabilities(config.workspace_path, registry, environment_root / "measure")
    event_pool = EventPool()
    event_runner = EventScriptRunner(str(environment_root / "event"))
    for event in load_event_scripts_isolated(str(config.workspace_path / "events"), event_runner):
        try:
            event_pool.register(event)
        except Exception as exc:
            LOGGER.warning("Unable to register workspace event %s: %s", event.name, exc)
    manager = SessionManager(
        llm=LiteLLMClient(config.model, api_base=config.api_base, api_key=config.api_key, timeout=config.llm_timeout),
        capabilities=registry,
        memory=TextMemory(config.workspace_path / "memory"),
        events=event_pool,
        max_rounds=config.max_rounds,
    )
    LOGGER.info("Runtime assembly completed capabilities=%d events=%d", len(registry.descriptors()), len(event_pool.descriptors()))
    return Runtime(config, registry, event_pool, manager)


def _show_banner(runtime: Runtime, session: ConversationSession, stream: TextIO) -> None:
    summary = RuntimeSummary(
        model=runtime.config.model,
        endpoint=safe_endpoint(runtime.config.api_base),
        session_id=session.session_id,
        capabilities=runtime.registry.descriptors(),
        events=runtime.events.descriptors(),
    )
    stream.write(render_banner(summary) + "\n")
    stream.flush()


def _run_interactive(runtime: Runtime, session: ConversationSession, *, input_stream: TextIO, output_stream: TextIO) -> int:
    """Run a small line-oriented TUI while preserving one conversation context."""

    output_stream.write("Commands: /help, /session, /new, /exit\n\n")
    output_stream.flush()
    LOGGER.info("Interactive mode started session_id=%s", session.session_id)
    current = session
    while True:
        try:
            output_stream.write("you> ")
            output_stream.flush()
            line = input_stream.readline()
        except KeyboardInterrupt:
            output_stream.write("\nUse /exit to close pivot.\n")
            output_stream.flush()
            continue
        if line == "":
            output_stream.write("\n")
            break
        message = line.strip()
        if not message:
            continue
        if message == "/exit":
            break
        if message == "/help":
            output_stream.write("/session shows the UUID; /new starts a new conversation; /exit closes pivot.\n")
            output_stream.flush()
            continue
        if message == "/session":
            output_stream.write(f"Conversation: {current.session_id}\n")
            output_stream.flush()
            continue
        if message == "/new":
            current = runtime.sessions.create()
            output_stream.write(f"New conversation: {current.session_id}\n")
            output_stream.flush()
            continue
        try:
            response = current.run(message)
        except Exception as exc:
            LOGGER.error("Interactive turn failed session_id=%s error_type=%s", current.session_id, type(exc).__name__)
            output_stream.write(f"pivot! {type(exc).__name__}: {exc}\n")
        else:
            output_stream.write(f"pivot> {response}\n")
        output_stream.flush()
    LOGGER.info("Interactive mode stopped session_id=%s", current.session_id)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging("INFO")
    try:
        runtime = build_runtime(PivotConfig.load(workspace_path=args.workspace))
        session = runtime.sessions.get(args.session) if args.session else runtime.sessions.create()
        if not args.no_banner:
            _show_banner(runtime, session, sys.stderr)
        if args.message is None and sys.stdin.isatty():
            return _run_interactive(runtime, session, input_stream=sys.stdin, output_stream=sys.stdout)
        message = args.message if args.message is not None else sys.stdin.read().strip()
        if not message.strip():
            raise ConfigurationError("A message argument or stdin input is required")
        response = session.run(message)
        LOGGER.info("CLI request completed session_id=%s", session.session_id)
        sys.stdout.write(response + "\n")
        return 0
    except Exception as exc:
        LOGGER.error("Pivot failed: %s", exc)
        return 1
