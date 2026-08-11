from pathlib import Path

from pivot.capabilities import CapabilityRegistry
from pivot.events import EventPool
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
    session = ConversationSession("s1", llm=FakeLLM(), capabilities=registry, memory=TextMemory(tmp_path), max_rounds=3)
    assert session.run("hello") == "finished"
    assert "finished" in (tmp_path / "s1.txt").read_text(encoding="utf-8")


def test_event_pool_notifies_matching_waiters() -> None:
    pool = EventPool()
    pool.register(EventDescriptor("hot", "Temperature is high", "temperature", ">", 40))
    seen: list[str] = []
    pool.wait("hot", "session", lambda notification: seen.append(notification.event_name))
    assert pool.report("hot", {"temperature": 41}) == ("session",)
    assert seen == ["hot"]
