# pivot architecture

The runtime follows a one-way dependency flow from models and configuration through adapters into activation and orchestration. Provider responses are converted at the LLM boundary, so the Agent core never imports LiteLLM types. `ActionDetector` normalizes native tool calls and fixed-format `<pivot-action>{JSON}</pivot-action>` output into capability, event, control, executor, or memory actions. Instance scripts are optional runtime extensions: one broken script is logged and skipped while the rest of the instance remains usable.

## Runtime ownership and identity

Runtime assembly first acquires an exclusive process lease in the resolved instance. Only one process may own dependency lifecycle, the main Agent, and writable runtime state for an instance at a time; a competing process fails before it starts dependencies. The lease is released during orderly shutdown and failed assembly.

The memory database owns one stable main-Agent UUID, enforced by a unique SQLite index. The identity is generated once rather than derived from a filesystem path. User-facing clients always submit to this Agent. Worker Agents are durable internal identities created and scheduled by the main Agent, but they are not user-selectable endpoints.

All user-facing submissions reserve a position in one main-Agent FIFO mailbox before entering the control worker pool. Only the request at the head may activate the main Agent; later requests remain queued. Cancelling any queued position advances the mailbox safely. Worker execution bypasses this admission path because workers are explicitly owned and scheduled by the main Agent.

## Persistent Agents and finite activations

`PersistentAgent` separates enduring identity from finite work. Each stimulus creates one activation with a bounded model/action loop and the states `running`, `completed`, `failed`, or `cancelled`. The Agent itself exposes the runtime states `ready`, `running`, and `pending`. Failed and cancelled activation data remains auditable in SQLite but is excluded from later model context.

Before every model round, `ContextBuilder` constructs a fresh prompt from:

- current Agent role and resource scope;
- current capability, event, executor, control, and memory descriptions;
- a bounded recent-message window from completed and current activations;
- relevant sourced long-term memories;
- non-expired world-state observations.

Static runtime context is never saved as prompt history. This prevents stale capability or device state from being replayed and prevents lifetime history from growing every request. See [memory](memory.md) for the persistence model.

## Worker scheduling and event waits

`AgentControl` lets the main Agent create a worker, assign explicit capability/event allowlists, submit a task, observe progress, and receive a structured report. `agent.delegate` creates and starts a worker asynchronously. Worker completion, failure, or cancellation is reinjected through the main-Agent FIFO mailbox instead of blocking the activation that created it.

The event runtime separates generic sources from per-request conditions. Event scripts expose `-l` descriptors containing a stable name, field, supported operators, and injection templates; they do not fix a threshold. The native `pivot_wait_event` tool is advertised to workers, which own normal long-lived waits without occupying the main-Agent mailbox. Direct main-Agent waits over one second are rejected. `EventService` creates a FIFO wait, polls only sources with active waiters, and returns a matched, timeout, or source-error notification as a tool message. Waiting conditions and outcomes are recorded as durable continuations.

## Capabilities, executors, and dependencies

Capabilities describe or provide domain work, while executors perform concrete machine actions. The initial `shell` executor uses a fixed instance cwd, a restricted inherited environment, a maximum timeout, and bounded output. This boundary is injectable and observable, but it remains process control rather than an operating-system sandbox.

Think, measure, and work scripts are discovered through `-l` in dedicated uv projects, so instance Python is never imported into the framework interpreter. Think scripts return their body through `-r` only after the model selects the summary. Measure scripts read one feature through `-r <feature>`. Work scripts accept JSON arguments on stdin through `-x` and emit a JSON result. Subprocess timeouts, output limits, fixed working directories, and restricted environments provide a lightweight process boundary.

External dependencies are standalone uv projects under `instance/dependencies`. Runtime assembly starts valid dependencies before discovering capabilities and events. The dependency manager performs one initial `uv sync`, records success with an atomic `.pivot-installed` marker, and uses `uv run --no-sync` thereafter. Every dependency implements `org.pivot.Dependency1` at `/org/pivot/Dependency`; heartbeat and structured status checks establish readiness without coupling pivot to application data protocols.

## Control and presentation

`pivot.runtime` exposes `PivotClient.main_agent` and `PivotClient.run_main` independently from presentation. The Textual application shows the main timeline plus a read-only Agent lifecycle sidebar. Progress callbacks update one trace containing model, capability, event, executor, memory, control, delegation, and integration phases.

`pivot.control` is the shared application command surface. Local clients call it directly; `pivot.dbus_control` exports the same object through `org.pivot.Control1` without making CLI/TUI behavior depend on D-Bus. Fast reads are synchronous, while main-Agent activations and other work run as observable asynchronous control tasks. The generic operation registry makes future operations remotely available without extending the D-Bus ABI for every feature.
