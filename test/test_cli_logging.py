from __future__ import annotations

from io import StringIO
from pathlib import Path
from uuid import UUID

from pivot.capabilities import CapabilityRegistry
from pivot.cli import Runtime, _run_interactive
from pivot.config import PivotConfig
from pivot.credentials import ProviderCredential
from pivot.events import EventPool
from pivot.memory import TextMemory
from pivot.models import CapabilityDescriptor, EventDescriptor
from pivot.session import SessionManager
from pivot.ui import RuntimeSummary, render_banner, safe_endpoint


class EchoLLM:
    def complete(self, messages, *, tools=()):
        return {"choices": [{"message": {"content": "ack"}}]}


def test_banner_contains_runtime_summary_and_safe_endpoint() -> None:
    session_id = "4b3c9f24-582c-42b1-bf25-f24a6f907f67"
    summary = RuntimeSummary(
        model="test-model",
        endpoint=safe_endpoint("https://user:secret@example.test/v1?token=secret"),
        session_id=session_id,
        capabilities=(CapabilityDescriptor("read", "measure", "Read"),),
        events=(EventDescriptor("ready", "Ready", "state", "is", "ready"),),
    )
    banner = render_banner(summary)
    assert "____  _" in banner
    assert "test-model" in banner
    assert session_id in banner
    assert "measure:read" in banner
    assert "ready" in banner
    assert "secret" not in banner


def test_interactive_cli_reuses_and_creates_uuid_sessions(tmp_path: Path) -> None:
    registry = CapabilityRegistry()
    events = EventPool()
    manager = SessionManager(llm=EchoLLM(), capabilities=registry, memory=TextMemory(tmp_path / "memory"))
    config = PivotConfig(workspace_path=tmp_path, provider=ProviderCredential("test", "test-model"))
    runtime = Runtime(config, registry, events, manager)
    session = manager.create()
    output = StringIO()

    result = _run_interactive(
        runtime,
        session,
        input_stream=StringIO("hello\n/session\n/new\n/exit\n"),
        output_stream=output,
    )

    assert result == 0
    assert "pivot> ack" in output.getvalue()
    assert f"Conversation: {session.session_id}" in output.getvalue()
    new_id = output.getvalue().split("New conversation: ", 1)[1].splitlines()[0]
    assert UUID(new_id)
