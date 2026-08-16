from __future__ import annotations

import time
from pathlib import Path

import pytest

from pivot.memory import RuntimeStore
from pivot.stimuli import (
    OutputEnvelope,
    StimulusEnvelope,
    StimulusError,
    StimulusInbox,
    StimulusState,
)


def _envelope(
    agent_id: str, *, source: str, content: str, priority: int = 50, **extra: object
) -> StimulusEnvelope:
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


def test_stimulus_envelope_binds_target_and_rejects_adapter_specific_fields(
    tmp_path: Path,
) -> None:
    memory = RuntimeStore(tmp_path / "memory")
    agent_id = memory.main_agent_id()
    envelope = _envelope(
        agent_id, source="instance.voice-adapter", content="look ahead"
    )

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


def test_stimulus_inbox_is_priority_ordered_idempotent_and_persistent(
    tmp_path: Path,
) -> None:
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


def test_event_occurrence_stimulus_and_edge_state_are_atomic(tmp_path: Path) -> None:
    memory = RuntimeStore(tmp_path / "memory")
    agent_id = memory.main_agent_id()
    inbox = StimulusInbox(memory, retention_seconds=0.001)
    first = _envelope(agent_id, source="event-bridge:temperature", content="first")
    occurrence = {
        "occurrence_id": "occurrence-1",
        "bridge_id": "temperature-alert",
        "event_name": "temperature",
        "source": "/event.py",
        "field": "temperature",
        "operator": ">",
        "expected": 40,
        "value": 42,
        "payload": {"values": {"temperature": 42}},
        "observed_at": 10.0,
        "rule_signature": '["temperature",">",40]',
        "fired_at": 10.0,
    }
    inbox.enqueue(first, occurrence=occurrence)

    state = memory.event_bridge_state("temperature-alert")
    assert state is not None and state["matched"] is True and state["fired_at"] == 10.0
    assert memory.event_occurrences()[0]["stimulus_id"] == first.stimulus_id

    second = _envelope(agent_id, source="event-bridge:temperature", content="second")
    with pytest.raises(StimulusError, match="occurrence conflicts"):
        inbox.enqueue(second, occurrence=occurrence)
    with pytest.raises(StimulusError, match="Unknown stimulus"):
        inbox.get(second.stimulus_id)

    inbox.finish(first.stimulus_id, StimulusState.COMPLETED)
    time.sleep(0.01)
    third = _envelope(agent_id, source="client", content="trigger retention")
    inbox.enqueue(third)
    assert memory.event_occurrences() == ()
    with pytest.raises(StimulusError, match="Unknown stimulus"):
        inbox.get(first.stimulus_id)
    inbox.finish(third.stimulus_id, StimulusState.CANCELLED)
    inbox.close()
    memory.close()


def test_replay_safe_processing_stimulus_is_requeued_after_runtime_restart(
    tmp_path: Path,
) -> None:
    memory = RuntimeStore(tmp_path / "memory")
    agent_id = memory.main_agent_id()
    inbox = StimulusInbox(memory)
    envelope = _envelope(agent_id, source="timer", content="resume", replay_safe=True)
    inbox.enqueue(envelope)
    assert inbox.claim_next(timeout=0.01).state == StimulusState.PROCESSING  # type: ignore[union-attr]
    inbox.close()

    recovered = StimulusInbox(memory)
    assert recovered.get(envelope.stimulus_id).state == StimulusState.QUEUED
    recovered.close()
    memory.close()


def test_unsafe_processing_stimulus_is_failed_after_runtime_restart(
    tmp_path: Path,
) -> None:
    memory = RuntimeStore(tmp_path / "memory")
    agent_id = memory.main_agent_id()
    inbox = StimulusInbox(memory)
    envelope = _envelope(agent_id, source="client", content="may have side effects")
    inbox.enqueue(envelope)
    assert inbox.claim_next(timeout=0.01) is not None
    inbox.close()

    recovered = StimulusInbox(memory)
    interrupted = recovered.get(envelope.stimulus_id)
    assert interrupted.state == StimulusState.FAILED
    assert "automatic replay was refused" in (interrupted.error or "")
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


def test_outputs_support_monotonic_resume_cursor(tmp_path: Path) -> None:
    memory = RuntimeStore(tmp_path / "memory")
    agent_id = memory.main_agent_id()
    inbox = StimulusInbox(memory)
    first_stimulus = _envelope(agent_id, source="client", content="first")
    second_stimulus = _envelope(agent_id, source="client", content="second")
    inbox.enqueue(first_stimulus)
    inbox.enqueue(second_stimulus)
    first = inbox.append_output(
        OutputEnvelope(
            "output-1", first_stimulus.stimulus_id, agent_id, "response", {}, None
        )
    )
    second = inbox.append_output(
        OutputEnvelope(
            "output-2", second_stimulus.stimulus_id, agent_id, "response", {}, None
        )
    )

    assert first.sequence == 1
    assert second.sequence == 2
    assert [item.output_id for item in inbox.outputs(after_sequence=1)] == ["output-2"]
    inbox.close()
    memory.close()
