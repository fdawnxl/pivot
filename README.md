# pivot

pivot is a layered agent runtime for edge devices, industrial control and embodied systems. It keeps model access, response parsing, capability execution, events, memory and agent activation replaceable through small Python interfaces.

## Quick start

The instance is deliberately explicit. Set `PIVOT_INSTANCE_PATH` (or pass `--instance`) and let uv create the project environment. Omit the message to start the interactive main-agent interface:

```bash
uv sync --extra dev
PIVOT_INSTANCE_PATH=/path/to/instance uv run pivot
PIVOT_INSTANCE_PATH=/path/to/instance uv run pivot "Hello"
```

The interactive CLI is a Textual application centered on one persistent main agent. The user always talks to that main agent; it either solves the request directly or creates a worker with an explicit capability and event scope. The agent sidebar shows delegated workers and their lifecycle without turning them into user-selectable timelines. Each trace groups model phases, capability calls, event waits, executor calls, control operations, worker reports, and result-integration rounds without exposing hidden chain-of-thought.

An instance is owned by exactly one runtime process at a time. Pivot acquires an exclusive `runtime.lock` before starting dependencies and releases it at shutdown, preventing multiple local processes from controlling the same device state concurrently.

While a CLI process is running, pivot exports a narrow framework control surface on the session D-Bus as `org.pivot.Control` at `/org/pivot/Control`. Instance adapters inject typed stimulus envelopes and consume transport-neutral outputs; they cannot remotely execute capabilities, manage workers, or bypass the main Agent. Framework operations are limited to status, stimulus inspection/cancellation, interruption, reload requests, and shutdown. Run a headless persistent reactor with `uv run pivot --dbus-only`, or disable export with `--no-dbus`. See [the stimulus protocol](doc/stimuli.md) and [D-Bus control protocol](doc/control-dbus.md).

Press `Enter` to send and `Shift+Enter` for a new line. Use `Ctrl+B` to toggle the agent sidebar, `Ctrl+G` to interrupt the main agent and its active workers, `Ctrl+L` to return to the prompt, and `Ctrl+Q` to exit. The prompt accepts `/agents`, `/agent`, `/stop`, `/help`, and `/exit`. Interruption is cooperative: event waits and delegated workers stop during safe polling boundaries, while a synchronous model, capability, or executor request stops at its next safe boundary. The same runtime is available through `pivot.PivotClient.run_main` for non-terminal clients.

The main Agent continuously consumes a durable SQLite inbox. Commands, observations, timers, system notices, and worker reports use one `StimulusEnvelope` contract. Priority selects urgent work while equal-priority inputs retain FIFO order; explicit source-local deduplication keys make adapter retries idempotent. Claimed inputs are returned to the queue after a runtime restart.

Wake-word detection, speech recognition, audio capture, TTS, sensor threshold policies, and other device-specific I/O belong to the instance. Pivot provides only the framework envelope and output contracts. For example, an instance voice adapter can inject a final transcript as a `command` and consume its correlated output without introducing voice code into the framework.

The TUI shows the selected provider, model, persistent Agent, capabilities, events, and live dependency health without exposing endpoint credentials. Use `--no-banner` to suppress the welcome details. One-shot requests retain the plain stdout response and stderr runtime summary expected by scripts.

An empty instance is initialized as follows. Existing files are never overwritten:

```text
instance/
├── capabilities/{think,measure,work}/
├── dependencies/<dependency-project>/
├── environment/{think,measure,work,event}/
├── events/
├── logs/pivot.log
├── memory/pivot.db       # SQLite WAL: agents, stimuli, outputs, activations, memory, tasks and world state
├── runtime.lock          # exclusive process lease while pivot owns this instance
├── config.toml
└── credentials.toml        # named LLM providers, mode 0600
```

Long-running external programs live in `dependencies`, with one standalone uv project per first-level directory. A valid project declares a stable logical id, an explicit D-Bus bus and service name, and an argv-style start command in `dependency.toml`. Pivot synchronizes its packages only on the first successful start, launches it in its own uv environment, confirms readiness through the common D-Bus status and heartbeat interface, and stops the process when the runtime closes. See [the dependency protocol](doc/dependencies.md) for the manifest and service contract.

LiteLLM is a core dependency and is installed by `uv sync`:

```bash
uv sync
```

`credentials.toml` owns complete provider connections and is restricted to mode `0600`:

```toml
[providers.local]
model = "openai/local-model"
api_base = "http://127.0.0.1:8000/v1"
api_key = "provider-secret"

[providers.cloud]
model = "openai/gpt-4o-mini"
api_key = "provider-secret"
```

`config.toml` only selects one provider and configures runtime behavior:

```toml
provider = "local"
max_rounds = 8
executor_timeout = 30
executor_max_output_bytes = 1048576
dbus_control_enabled = true
dbus_control_bus = "session"
dbus_control_service = "org.pivot.Control"
```

`PIVOT_PROVIDER` overrides the selected provider. Other runtime settings use `PIVOT_<NAME>`, then `config.toml`, then code defaults. Model names, API endpoints, and API keys are read only from `credentials.toml` and are never written to ordinary configuration or logs.

Terminal and persisted log levels are independent. Supported values are `debug`, `info`, `warn` and `error`:

```toml
[logging]
display_level = "info"
storage_level = "debug"
```

The corresponding environment variables are `PIVOT_LOG_DISPLAY_LEVEL` and `PIVOT_LOG_STORAGE_LEVEL`. The aliases `log_console_level`/`PIVOT_LOG_CONSOLE_LEVEL` and `log_file_level`/`PIVOT_LOG_FILE_LEVEL` are also accepted. Files rotate at 5 MiB and retain three backups. Terminal logs remain human-readable; persisted logs use JSON Lines and include `correlation_id`, `activation_id`, and `agent_id` when available.

## Architecture

`pivot.config` bootstraps an instance; `pivot.logging` configures output; `pivot.dependencies` manages external uv projects; `pivot.llm` wraps LiteLLM; `pivot.parser` extracts provider responses; `pivot.stimuli` owns the durable inbox, outputs and main-Agent reactor; `pivot.actions` normalizes model actions; `pivot.capabilities` dispatches `think`, `measure` and `work`; `pivot.executors` performs concrete execution; `pivot.events` owns event waits; `pivot.agents` owns main/worker control; `pivot.memory` owns durable identity and layered memory; and `pivot.activation` runs finite model/action activations.

The preferred model operation is the single `pivot_action` tool with `{kind, name, arguments}`. The same detector accepts a textual fallback in the form `<pivot-action>{JSON}</pivot-action>`. Capability calls, event waits, framework control, executor requests, and memory operations all pass through this normalized route. Provider-native capability and event tools are normalized through the same router. See [agent control, actions, and executors](doc/agent-control.md) for the operation contract.

The built-in `shell` executor runs through `/bin/sh` in the explicit instance directory with a restricted environment, configured timeout, and bounded stdout/stderr. It is an execution mechanism rather than a work methodology. It does not provide an operating-system security sandbox.

Instance capability scripts are never imported by the pivot process. They use dedicated uv environments and JSON command protocols:

```text
think:   -l -> descriptor, -r -> full triple-quoted capability text
measure: -l -> descriptor, -r <feature> -> measured JSON value
work:    -l -> descriptor, -x + JSON stdin -> JSON execution result
```

Messages may contain either text or provider-compatible content-part arrays. A capability can return
`{"content": [...]}` to attach image, audio, or video parts to its tool result; pivot preserves those parts through
the following model round and SQLite message history without interpreting the media payload.

Think descriptors are injected lazily. The model sees their names and summaries at first, then calls the built-in `pivot_read_think` tool only when it needs the full text. Work processes use a fixed instance directory, a restricted environment, a timeout, and an output limit. This is process isolation intended to protect the pivot interpreter; deployments executing hostile commands should add an operating-system sandbox.

Events are generic monitored fields rather than fixed thresholds. An event descriptor advertises a field and supported operators. A worker calls `pivot_wait_event` with the chosen operator, expected value, and timeout. A matching value, timeout, or isolated source error is formatted with the event's injection template, appended as a tool result, and resumes that worker activation.

Long event waits belong to delegated workers. The persistent main Agent does not advertise the native wait tool, and direct waits longer than one second are rejected with guidance to delegate. Worker completion, failure, or cancellation is delivered later as a typed `worker_report` stimulus. Instance event adapters publish `observation` stimuli for autonomous device monitoring.

Memory is not an unbounded prompt transcript. Every activation and message is appended to `memory/pivot.db`; prompt construction selects only a bounded recent window, retrieves relevant sourced memories, and injects non-expired world state. Runtime capability/event/control descriptions are rebuilt on every model round, so old environment snapshots are never replayed as durable messages. Long-term records use `fact`, `preference`, `episode`, or `procedure` kinds with confidence, validity, source, sensitivity, and supersession metadata. See [the memory model](doc/memory.md).

Run the test suite with:

```bash
uv run --extra dev pytest
```
