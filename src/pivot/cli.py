"""Command-line entry point for the initial pivot runtime."""

from __future__ import annotations

import argparse
import logging
import sys
from typing import TextIO

from .config import ConfigurationError, PivotConfig
from .logging import configure_logging, configure_tui_logging
from .runtime import PivotClient, Runtime, build_runtime
from .session import ConversationSession
from .tui import run_tui
from .ui import RuntimeSummary, render_banner, safe_endpoint

LOGGER = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pivot", description="Run a pivot agent conversation")
    parser.add_argument("--workspace", help="Path to the pivot workspace (or set PIVOT_WORKSPACE_PATH)")
    parser.add_argument("--session", help="Conversation UUID to resume; omitted creates a new conversation")
    parser.add_argument("--no-banner", action="store_true", help="Suppress the startup logo and runtime summary")
    parser.add_argument("message", nargs="?", help="One user message; omit for interactive mode or stdin")
    return parser


def _show_banner(runtime: Runtime, session: ConversationSession, stream: TextIO) -> None:
    summary = RuntimeSummary(
        provider=runtime.config.provider.name,
        model=runtime.config.provider.model,
        endpoint=safe_endpoint(runtime.config.provider.api_base),
        session_id=session.session_id,
        capabilities=runtime.registry.descriptors(),
        events=runtime.events.descriptors(),
    )
    stream.write(render_banner(summary) + "\n")
    stream.flush()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging("INFO")
    client: PivotClient | None = None
    try:
        client = PivotClient(build_runtime(PivotConfig.load(workspace_path=args.workspace)))
        runtime = client.runtime
        session = runtime.sessions.get(args.session) if args.session else runtime.sessions.create()
        if args.message is None and sys.stdin.isatty():
            configure_tui_logging()
            run_tui(client, session, show_welcome=not args.no_banner)
            return 0
        if not args.no_banner:
            _show_banner(runtime, session, sys.stderr)
        message = args.message if args.message is not None else sys.stdin.read().strip()
        if not message.strip():
            raise ConfigurationError("A message argument or stdin input is required")
        response = client.run(session.session_id, message)
        LOGGER.info("CLI request completed session_id=%s", session.session_id)
        sys.stdout.write(response + "\n")
        return 0
    except Exception as exc:
        LOGGER.error("Pivot failed: %s", exc)
        return 1
    finally:
        if client is not None:
            client.close()
