from pathlib import Path
from uuid import uuid4

from pivot.capabilities import CapabilityRegistry
from pivot.events import EventPool, EventScriptRunner, EventSupervisor, load_event_scripts_isolated
from pivot.memory import TextMemory
from pivot.models import CapabilityDescriptor, EventDescriptor
from pivot.session import ConversationSession


class FakeLLM:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, messages, *, tools=()):
        self.calls += 1
        if self.calls == 1:
            return {"choices": [{"message": {"content": "", "tool_calls": [{"id": "c1", "function": {"name": "echo", "arguments": '{"value":"ok"}'}}]}}]}
        return {"choices": [{"message": {"content": "finished"}}]}


def test_session_executes_tools_and_persists(tmp_path: Path) -> None:
    registry = CapabilityRegistry()
    registry.register(CapabilityDescriptor("echo", "work", "Echo value", {"type": "object"}), lambda value: value)
    session_id = str(uuid4())
    session = ConversationSession(session_id, llm=FakeLLM(), capabilities=registry, memory=TextMemory(tmp_path), max_rounds=3)
    assert session.run("hello") == "finished"
    assert "finished" in (tmp_path / session_id / "history.jsonl").read_text(encoding="utf-8")
    restored = ConversationSession(session_id, llm=FakeLLM(), capabilities=registry, memory=TextMemory(tmp_path), max_rounds=3)
    assert len(restored.history) == len(session.history)


def test_event_pool_notifies_matching_waiters() -> None:
    pool = EventPool()
    pool.register(EventDescriptor("hot", "Temperature is high", "temperature", ">", 40))
    seen: list[str] = []
    pool.wait("hot", "session", lambda notification: seen.append(notification.event_name))
    assert pool.report("hot", {"temperature": 41}) == ("session",)
    assert seen == ["hot"]


def test_isolated_event_runner_and_supervisor(tmp_path: Path) -> None:
    event_root = tmp_path / "events"
    event_root.mkdir()
    environment = tmp_path / "event-env"
    environment.mkdir()
    (environment / "pyproject.toml").write_text(
        '[project]\nname = "test-event-env"\nversion = "0.1.0"\nrequires-python = ">=3.11"\ndependencies = []\n', encoding="utf-8"
    )
    (event_root / "sample.py").write_text(
        "import json, os\nD={'name':'hot','description':'hot','field':'temperature','operator':'>=','expected':40}\n"
        "import sys\nprint(json.dumps([D] if '-l' in sys.argv else {'temperature': 42}))\n", encoding="utf-8"
    )
    runner = EventScriptRunner(str(environment))
    events = load_event_scripts_isolated(str(event_root), runner)
    pool = EventPool()
    pool.register(events[0])
    seen: list[str] = []
    pool.wait("hot", "s1", lambda notification: seen.append(notification.event_name))
    assert EventSupervisor(pool, str(event_root), runner).poll_once() == {"hot": ("s1",)}
    assert seen == ["hot"]
