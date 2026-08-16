from __future__ import annotations

from pathlib import Path

import pytest

from pivot.memory import RuntimeStore
from pivot.stimuli import StimulusEnvelope, StimulusError, StimulusInbox, StimulusState


def _envelope(agent_id: str, *, source: str, content: str, priority: int = 50, **extra: object) -> StimulusEnvelope:
    return StimulusEnvelope.from_mapping(
        {
            "kind": "command",
            "source": source,
            "payload": {"content": content},
            "priority": priority,
            **extra,
        },
        target_agent_id=agent_id,
    )


def test_stimulus_envelope_binds_target_and_rejects_adapter_specific_fields(tmp_path: Path) -> None:
    memory = RuntimeStore(tmp_path / "memory")
    agent_id = memory.main_agent_id()
    envelope = _envelope(agent_id, source="instance.voice-adapter", content="look ahead")

    assert envelope.target_agent_id == agent_id
    assert envelope.activation_content() == "look ahead"
    with pytest.raises(StimulusError, match="Unknown stimulus fields"):
        StimulusEnvelope.from_mapping(
            {
                "kind": "command",
                "source": "adapter",
                "target_agent_id": "untrusted-target",
                "payload": {"content": "bad"},
            },
            target_agent_id=agent_id,
        )
    memory.close()


def test_stimulus_inbox_is_priority_ordered_idempotent_and_persistent(tmp_path: Path) -> None:
    memory = RuntimeStore(tmp_path / "memory")
    agent_id = memory.main_agent_id()
    inbox = StimulusInbox(memory)
    low = _envelope(agent_id, source="sensor", content="low", priority=10)
    high = _envelope(
        agent_id,
        source="sensor",
        content="high",
        priority=90,
        dedupe_key="sample-1",
    )
    assert inbox.enqueue(low)[1]
    assert inbox.enqueue(high)[1]
    conflicting_id = StimulusEnvelope.from_mapping(
        {
            "stimulus_id": low.stimulus_id,
            "kind": "command",
            "source": "sensor",
            "payload": {"content": "different"},
            "priority": 10,
        },
        target_agent_id=agent_id,
    )
    with pytest.raises(StimulusError, match="different envelope content"):
        inbox.enqueue(conflicting_id)
    duplicate = _envelope(
        agent_id,
        source="sensor",
        content="duplicate",
        priority=90,
        dedupe_key="sample-1",
    )
    existing, created = inbox.enqueue(duplicate)
    assert not created and existing.stimulus_id == high.stimulus_id

    claimed = inbox.claim_next(timeout=0.01)
    assert claimed is not None and claimed.stimulus_id == high.stimulus_id
    inbox.finish(high.stimulus_id, StimulusState.COMPLETED, response="handled")
    inbox.close()

    reopened = StimulusInbox(memory)
    assert reopened.get(high.stimulus_id).response == "handled"
    remaining = reopened.claim_next(timeout=0.01)
    assert remaining is not None and remaining.stimulus_id == low.stimulus_id
    reopened.finish(low.stimulus_id, StimulusState.CANCELLED)
    reopened.close()
    memory.close()


def test_processing_stimulus_is_requeued_after_runtime_restart(tmp_path: Path) -> None:
    memory = RuntimeStore(tmp_path / "memory")
    agent_id = memory.main_agent_id()
    inbox = StimulusInbox(memory)
    envelope = _envelope(agent_id, source="timer", content="resume")
    inbox.enqueue(envelope)
    assert inbox.claim_next(timeout=0.01).state == StimulusState.PROCESSING  # type: ignore[union-attr]
    inbox.close()

    recovered = StimulusInbox(memory)
    assert recovered.get(envelope.stimulus_id).state == StimulusState.QUEUED
    recovered.close()
    memory.close()


def test_stimulus_inbox_enforces_pending_limit(tmp_path: Path) -> None:
    memory = RuntimeStore(tmp_path / "memory")
    agent_id = memory.main_agent_id()
    inbox = StimulusInbox(memory, max_pending=1)
    inbox.enqueue(_envelope(agent_id, source="sensor-a", content="first"))

    with pytest.raises(StimulusError, match="inbox is full"):
        inbox.enqueue(_envelope(agent_id, source="sensor-b", content="second"))

    assert inbox.pending_count() == 1
    inbox.close()
    memory.close()
