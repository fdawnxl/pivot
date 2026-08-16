from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from pivot.actions import ACTION_TOOL
from pivot.activation import PersistentAgent
from pivot.capabilities import CapabilityRegistry
from pivot.memory import ContextBuilder, MemoryService, RuntimeStore
from pivot.models import Message, ToolCall


def test_memory_store_has_stable_main_identity_and_wal_storage(tmp_path: Path) -> None:
    root = tmp_path / "memory"
    first = RuntimeStore(root)
    agent_id = first.main_agent_id()
    assert first.main_agent_id() == agent_id
    first.close()
    first.close()

    second = RuntimeStore(root)
    assert second.main_agent_id() == agent_id
    with sqlite3.connect(second.path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert (
            connection.execute("PRAGMA user_version").fetchone()[0]
            == RuntimeStore.SCHEMA_VERSION
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM agents WHERE role = 'main'"
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM sqlite_master WHERE type = 'table' AND name IN ('stimuli', 'outputs')"
            ).fetchone()[0]
            == 2
        )
    second.close()


def test_messages_are_appended_and_context_is_bounded(tmp_path: Path) -> None:
    memory = RuntimeStore(tmp_path / "memory")
    agent_id = memory.main_agent_id()
    for index in range(4):
        activation_id = memory.create_activation(agent_id, "user", f"request {index}")
        memory.append_message(
            agent_id, activation_id, Message("user", f"request {index}")
        )
        memory.append_message(
            agent_id, activation_id, Message("assistant", f"response {index}")
        )
        memory.finish_activation(
            activation_id, "completed", response=f"response {index}"
        )

    current = memory.create_activation(agent_id, "user", "current")
    memory.append_message(agent_id, current, Message("user", "current"))
    context = ContextBuilder(memory, max_messages=3, max_chars=1024).build(
        agent_id=agent_id,
        activation_id=current,
        query="current",
        runtime_context={"generation": 1},
    )

    assert len(memory.messages(agent_id)) == 9
    assert [message.content for message in context[1:]] == [
        "request 3",
        "response 3",
        "current",
    ]
    with sqlite3.connect(memory.path) as connection:
        ids = [
            row[0]
            for row in connection.execute(
                "SELECT message_id FROM messages ORDER BY message_id"
            )
        ]
        assert ids == list(range(1, 10))
    memory.close()


def test_context_keeps_tool_chain_when_result_contains_embedded_media(
    tmp_path: Path,
) -> None:
    memory = RuntimeStore(tmp_path / "memory")
    agent_id = memory.main_agent_id()
    activation_id = memory.create_activation(agent_id, "user", "inspect camera")
    memory.append_message(agent_id, activation_id, Message("user", "inspect camera"))
    memory.append_message(
        agent_id,
        activation_id,
        Message("assistant", "", tool_calls=(ToolCall("camera", {}, "call-1"),)),
    )
    memory.append_message(
        agent_id,
        activation_id,
        Message(
            "tool",
            (
                {"type": "text", "text": "Camera frame."},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/jpeg;base64," + "A" * 5000},
                },
            ),
            name="camera",
            tool_call_id="call-1",
        ),
    )

    context = memory.context_messages(
        agent_id, activation_id, max_messages=8, max_chars=1024
    )

    assert [message.role for message in context] == ["user", "assistant", "tool"]
    assert isinstance(context[-1].content, tuple)
    memory.close()


def test_context_rebuilds_runtime_and_world_state_without_stale_snapshot(
    tmp_path: Path,
) -> None:
    memory = RuntimeStore(tmp_path / "memory")
    agent_id = memory.main_agent_id()
    activation_id = memory.create_activation(agent_id, "user", "temperature")
    memory.append_message(agent_id, activation_id, Message("user", "temperature"))
    builder = ContextBuilder(memory)

    memory.update_world_state("sensor", {"temperature": 20}, ttl=10)
    first = builder.build(
        agent_id=agent_id,
        activation_id=activation_id,
        query="temperature",
        runtime_context={"generation": 1},
    )
    memory.update_world_state("sensor", {"temperature": 21}, ttl=10)
    second = builder.build(
        agent_id=agent_id,
        activation_id=activation_id,
        query="temperature",
        runtime_context={"generation": 2},
    )

    first_context = json.loads(str(first[0].content).split("\n", 1)[1])
    second_context = json.loads(str(second[0].content).split("\n", 1)[1])
    assert first_context["generation"] == 1
    assert first_context["world_state"][0]["value"] == 20
    assert second_context["generation"] == 2
    assert second_context["world_state"][0]["value"] == 21
    assert all(message.role != "system" for message in memory.messages(agent_id))
    memory.close()


def test_recall_respects_validity_supersession_and_forgetting(tmp_path: Path) -> None:
    memory = RuntimeStore(tmp_path / "memory")
    expired = memory.remember(
        namespace="global",
        kind="fact",
        content="door code is expired",
        source="test",
        valid_until=time.time() - 1,
    )
    old = memory.remember(
        namespace="global",
        kind="preference",
        content="operator prefers green indicators",
        source="user",
    )
    new = memory.remember(
        namespace="global",
        kind="preference",
        content="operator prefers blue indicators",
        source="user",
        supersedes=old.memory_id,
    )

    assert memory.recall("expired", namespaces=("global",)) == ()
    recalled = memory.recall("indicators", namespaces=("global",))
    assert [record.memory_id for record in recalled] == [new.memory_id]
    assert memory.forget(new.memory_id)
    assert memory.recall("indicators", namespaces=("global",)) == ()
    assert not memory.forget(expired.memory_id + "-missing")
    memory.close()


def test_expired_world_state_is_not_injected(tmp_path: Path) -> None:
    memory = RuntimeStore(tmp_path / "memory")
    memory.update_world_state("camera", {"scene": "corridor"}, ttl=-1)
    memory.update_world_state("imu", {"moving": False})
    assert memory.current_world_state() == (
        {
            "source": "imu",
            "field": "moving",
            "value": False,
            "observed_at": memory.current_world_state()[0]["observed_at"],
            "valid_until": None,
        },
    )
    memory.close()


def test_tasks_and_event_continuations_are_durable(tmp_path: Path) -> None:
    root = tmp_path / "memory"
    memory = RuntimeStore(root)
    agent_id = memory.main_agent_id()
    memory.upsert_task("task-1", agent_id, "watch the doorway", "running")
    memory.save_continuation(
        "wait-1",
        agent_id,
        "event",
        "waiting",
        {"event": "door_open"},
        task_id="task-1",
        deadline=123.0,
    )
    memory.close()

    reopened = RuntimeStore(root)
    with sqlite3.connect(reopened.path) as connection:
        task = connection.execute(
            "SELECT description, state FROM tasks WHERE task_id = 'task-1'"
        ).fetchone()
        continuation = connection.execute(
            "SELECT kind, state, condition_json, deadline FROM continuations WHERE continuation_id = 'wait-1'"
        ).fetchone()
    assert task == ("watch the doorway", "running")
    assert continuation == ("event", "waiting", '{"event":"door_open"}', 123.0)
    reopened.close()


def test_memory_action_is_routed_through_agent_activation(tmp_path: Path) -> None:
    class RememberingLLM:
        def complete(self, messages, *, tools=()):
            assert any(tool["function"]["name"] == ACTION_TOOL for tool in tools)
            if not any(message.role == "tool" for message in messages):
                action = {
                    "kind": "memory",
                    "name": "memory.remember",
                    "arguments": {
                        "kind": "preference",
                        "content": "User prefers concise answers",
                    },
                }
                return {
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "id": "remember-1",
                                        "function": {
                                            "name": ACTION_TOOL,
                                            "arguments": json.dumps(action),
                                        },
                                    }
                                ]
                            }
                        }
                    ]
                }
            return {"choices": [{"message": {"content": "Preference saved."}}]}

    memory = RuntimeStore(tmp_path / "memory")
    agent_id = memory.main_agent_id()
    agent = PersistentAgent(
        agent_id,
        llm=RememberingLLM(),
        capabilities=CapabilityRegistry(),
        memory=memory,
        memory_service=MemoryService(memory),
        max_rounds=3,
    )
    assert agent._activate("Remember this") == "Preference saved."
    recalled = memory.recall("concise", namespaces=(f"agent:{agent_id}",))
    assert len(recalled) == 1
    assert recalled[0].kind == "preference"
    memory.close()
