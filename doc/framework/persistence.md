# Persistence and stimuli

All writable runtime state lives in `instance/memory/pivot.db`. `RuntimeStore` owns one SQLite connection configured with WAL, foreign keys, and full synchronous commits.

## Data model

The current schema stores:

- `agents`: stable main and worker identities, scope, lifecycle, and one-shot policy;
- `activations`: finite input-to-outcome runs;
- `messages`: ordered provider-neutral messages within activations;
- `journal`: append-only runtime audit events;
- `memories`: sourced long-term records;
- `tasks`: delegated work and terminal results;
- `continuations`: event wait conditions and outcomes;
- `world_state`: latest observations with optional expiry;
- `stimuli`: durable external and internal inbox items;
- `outputs`: transport-neutral results with monotonic sequence numbers;
- `event_bridge_state` and `event_occurrences`: durable autonomous event edges.

Schema changes use `PRAGMA user_version` and explicit idempotent migrations.

## Memory layers

Long-term records use `fact`, `preference`, `episode`, or `procedure` kinds. A record includes source, confidence, validity, sensitivity, optional supersession, and soft deletion. Recall searches the global and current-Agent namespaces with FTS5 when available and substring matching otherwise.

Completed activations create episode records automatically. Failed and cancelled activations remain in the audit database but are excluded from future prompt context.

## Bounded context

`ContextBuilder` combines an ephemeral runtime system message with bounded persisted history. Message selection limits both count and approximate text size. Embedded media payloads are represented by a short placeholder for context-budget accounting, so base64 data does not evict the surrounding tool chain.

Expired world state and memory are excluded. Superseded records do not reappear when their replacements are forgotten.

## Stimulus envelope

Every external input is validated and bound to the main Agent:

```json
{
  "kind": "command | observation | worker_report | timer | system",
  "source": "instance.voice",
  "payload": {"content": "Describe the scene"},
  "priority": 50,
  "delivery": "activate",
  "replay_safe": false,
  "correlation_id": "optional-id",
  "causation_id": "optional-parent-id",
  "dedupe_key": "optional-source-local-id"
}
```

The caller cannot provide `target_agent_id`. Commands require `payload.content`. State-only observations require a non-empty `payload.values` object and accept a positive `ttl`.

Default priorities are command 50, worker report 40, system 30, observation 20, and timer 10. Priority aging prevents starvation, while equal effective priorities remain FIFO. A `(source, dedupe_key)` pair makes retries idempotent.

## Delivery and replay

Observations default to `delivery=state`: they update world state without an LLM activation and are replay-safe by default. Other kinds default to `delivery=activate` and replay-unsafe.

After an unclean restart, a processing stimulus returns to the queue only when `replay_safe=true`. Unsafe work is marked failed because an external side effect may already have happened.

Stimulus state is `queued`, `processing`, then `completed`, `failed`, or `cancelled`. The queue is bounded and terminal rows are removed after the configured retention period.

## Outputs

A successful main-Agent activation persists an output:

```json
{
  "output_id": "uuid",
  "sequence": 42,
  "stimulus_id": "uuid",
  "agent_id": "main-agent-uuid",
  "kind": "response",
  "payload": {
    "content": "The path ahead is clear.",
    "stimulus_kind": "command",
    "source": "instance.voice"
  },
  "correlation_id": "optional-id"
}
```

Consumers resume after disconnect with `ListOutputs(after_sequence, limit)`. They should persist the highest successfully processed sequence instead of assuming transient signals are complete delivery.
