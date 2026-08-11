"""Command-line entry point for the initial pivot runtime."""

from __future__ import annotations

import argparse
import logging
import sys

from .capabilities import CapabilityRegistry
from .capabilities.discovery import register_workspace_capabilities
from .config import ConfigurationError, PivotConfig
from .events import EventPool, EventScriptRunner, load_event_scripts_isolated
from .llm import LiteLLMClient
from .memory import TextMemory
from .session import SessionManager

LOGGER = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pivot", description="Run a pivot agent conversation")
    parser.add_argument("--workspace", help="Path to the pivot workspace (or set PIVOT_WORKSPACE_PATH)")
    parser.add_argument("--session", default="default", help="Session identifier")
    parser.add_argument("message", nargs="?", help="One user message; omit to read stdin")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = PivotConfig.load(workspace_path=args.workspace)
        registry = CapabilityRegistry()
        register_workspace_capabilities(config.workspace_path, registry, config.workspace_path / "measure-env")
        event_pool = EventPool()
        event_runner = EventScriptRunner(str(config.workspace_path / "event-env"))
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
        message = args.message or sys.stdin.read().strip()
        if not message:
            raise ConfigurationError("A message argument or stdin input is required")
        response = manager.run(args.session, message)
        LOGGER.info("Session completed")
        sys.stdout.write(response + "\n")
        return 0
    except (ConfigurationError, Exception) as exc:
        LOGGER.error("Pivot failed: %s", exc)
        return 1
