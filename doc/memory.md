# Persistent Agent memory

Pivot treats memory as a set of durable layers, not as one ever-growing prompt transcript. All layers live in `instance/memory/pivot.db`, an SQLite database using WAL and full synchronous commits.

## Layers

The database stores:

- `agents`: stable main and worker identities, ownership, scope, and lifecycle state;
- `activations`: finite input-to-outcome runs, including failure and cancellation audit data;
- `messages`: append-only provider-neutral messages ordered within each activation;
- `journal`: append-only lifecycle and memory events;
- `memories`: sourced `fact`, `preference`, `episode`, and `procedure` records;
- `tasks`: delegated work descriptions and terminal results;
- `continuations`: event waiting conditions, deadlines, and outcomes;
- `world_state`: latest observations with optional expiry.

No conversation identifier or user-selectable history container exists. The main Agent has one durable identity and one lifetime timeline. Worker histories remain isolated by `agent_id`.

## Prompt construction

`ContextBuilder` rebuilds the model input on every round. The system message contains current runtime descriptions, retrieved memory, and non-expired world state. It is ephemeral and is not appended to history. The remainder is a bounded window of recent messages from completed activations plus the current activation. Messages from failed or cancelled activations remain in the audit store but are excluded from later prompts.

This design bounds prompt growth and prevents an old capability list, event descriptor, dependency state, or measurement from becoming permanent context. Current measurements take precedence over retrieved memory; the prompt explicitly tells the model that recalled records may be stale or incorrect.

## Long-term records

Every structured record contains:

```text
memory_id
namespace
kind                 fact | preference | episode | procedure
content
source
confidence           0.0 .. 1.0
valid_from
valid_until          optional
supersedes           optional prior memory_id
sensitivity
created_at
```

Recall searches the global namespace and the current Agent namespace, filters expired, deleted, and superseded records, then ranks relevant results. FTS5 is used when available; otherwise pivot falls back to substring matching. Forgetting writes a deletion timestamp and removes the search index entry instead of rewriting a history file. Superseded records do not silently reappear when the newer record is forgotten.

Completed activations automatically create an episode record. Models and control clients can explicitly invoke `memory.remember`, `memory.recall`, and `memory.forget` for durable facts, preferences, and procedures.

## Runtime facts and waits

World state is keyed by source and field. Each observation has `observed_at` and optional `valid_until`; expired values are not injected. Event waits are recorded as continuations before blocking, then updated with completion or cancellation data. Normal long waits run in worker Agents so this durability does not turn the main Agent into a blocked worker.
