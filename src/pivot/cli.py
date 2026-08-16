"""Command-line entry point for the initial pivot runtime."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from typing import TextIO

from .config import ConfigurationError, PivotConfig
from .dbus_control import ControlDBusError
from .logging import configure_logging, configure_tui_logging
from .runtime import PivotClient, Runtime, build_runtime
from .activation import PersistentAgent
from .tui import run_tui
from .ui import RuntimeSummary, render_banner, safe_endpoint

LOGGER = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pivot", description="Run the persistent pivot main agent")
    parser.add_argument("--instance", help="Path to the pivot instance (or set PIVOT_INSTANCE_PATH)")
    parser.add_argument("--no-banner", action="store_true", help="Suppress the startup logo and runtime summary")
    parser.add_argument("--no-dbus", action="store_true", help="Do not export the pivot D-Bus control interface")
    parser.add_argument("--dbus-only", action="store_true", help="Run only the D-Bus control service until interrupted")
    parser.add_argument("message", nargs="?", help="One user message; omit for interactive mode or stdin")
    return parser


def _show_banner(runtime: Runtime, agent: PersistentAgent, stream: TextIO) -> None:
    summary = RuntimeSummary(
        provider=runtime.config.provider.name,
        model=runtime.config.provider.model,
        endpoint=safe_endpoint(runtime.config.provider.api_base),
        agent_id=agent.agent_id,
        capabilities=runtime.registry.descriptors(),
        events=runtime.events.descriptors(),
    )
    stream.write(render_banner(summary) + "\n")
    stream.flush()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.dbus_only and args.no_dbus:
        build_parser().error("--dbus-only and --no-dbus cannot be used together")
    if args.dbus_only and args.message is not None:
        build_parser().error("--dbus-only does not accept a message")
    configure_logging("INFO")
    client: PivotClient | None = None
    try:
        client = PivotClient(build_runtime(PivotConfig.load(instance_path=args.instance)))
        runtime = client.runtime
        agent = client.main_agent()
        dbus_required = args.dbus_only
        if runtime.config.dbus_control_enabled and not args.no_dbus:
            try:
                client.start_dbus(
                    bus=runtime.config.dbus_control_bus,
                    service_name=runtime.config.dbus_control_service,
                    start_timeout=runtime.config.dbus_control_start_timeout,
                )
            except ControlDBusError:
                if dbus_required:
                    raise
                LOGGER.warning("D-Bus control is unavailable; continuing with the local client")
        elif dbus_required:
            raise ConfigurationError("D-Bus control is disabled by configuration")
        if args.dbus_only:
            _wait_for_shutdown(client)
            return 0
        if args.message is None and sys.stdin.isatty():
            configure_tui_logging()
            run_tui(client, agent, show_welcome=not args.no_banner)
            return 0
        if not args.no_banner:
            _show_banner(runtime, agent, sys.stderr)
        message = args.message if args.message is not None else sys.stdin.read().strip()
        if not message.strip():
            raise ConfigurationError("A message argument or stdin input is required")
        response = client.run_main(message)
        LOGGER.info("CLI request completed agent_id=%s", agent.agent_id)
        sys.stdout.write(response + "\n")
        return 0
    except Exception as exc:
        LOGGER.error("Pivot failed: %s", exc)
        return 1
    finally:
        if client is not None:
            client.close()


def _wait_for_shutdown(client: PivotClient) -> None:
    """Wait for SIGINT or SIGTERM while the D-Bus control service is active."""

    stopped = threading.Event()
    previous: dict[int, object] = {}

    def request_stop(_signum: int, _frame: object) -> None:
        stopped.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.signal(signum, request_stop)
    unsubscribe = client.control.subscribe(
        lambda event, _payload: stopped.set()
        if event in {"shutdown_requested", "reload_requested"}
        else None
    )
    LOGGER.info("D-Bus-only control process is ready")
    try:
        stopped.wait()
    finally:
        unsubscribe()
        for signum, handler in previous.items():
            signal.signal(signum, handler)
