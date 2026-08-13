# pivot

pivot is a layered agent runtime for edge devices, industrial control and embodied systems. It keeps model access, response parsing, capability execution, events, memory and conversation orchestration replaceable through small Python interfaces.

## Quick start

The instance is deliberately explicit. Set `PIVOT_INSTANCE_PATH` (or pass `--instance`) and let uv create the project environment. Omit the message to start an interactive conversation:

```bash
uv sync --extra dev
PIVOT_INSTANCE_PATH=/path/to/instance uv run pivot
PIVOT_INSTANCE_PATH=/path/to/instance uv run pivot "Hello"
```

The interactive CLI is a Textual application with a persistent prompt, Markdown conversation timeline, responsive session sidebar, and inspectable agent trace. Each trace groups model analysis phases, model-provided decision summaries, capability arguments and results, event waits, and result-integration rounds without exposing hidden chain-of-thought. Agent turns run in background workers, so another conversation can be opened while one is working.

Press `Enter` to send and `Shift+Enter` for a new line. Use `Ctrl+N` for a new conversation, `Ctrl+Left`/`Ctrl+Right` to enter the session sidebar and navigate older or newer conversations, `Ctrl+B` to toggle the sidebar, `Ctrl+G` to interrupt the selected conversation, `Ctrl+L` to return to the prompt, and `Ctrl+Q` to exit. The bottom shortcut bar shows these common actions and is also clickable. The prompt accepts `/new`, `/next`, `/prev`, `/switch <id-prefix>`, `/session`, `/sessions`, `/stop`, `/help`, and `/exit`. Interruption is cooperative: event waits stop during polling, while a synchronous model or capability request stops at its next safe boundary. The same runtime is also available through `pivot.PivotClient` for services and other clients that do not use the terminal UI. Resume an existing conversation with:

```bash
PIVOT_INSTANCE_PATH=/path/to/instance uv run pivot --session 4b3c9f24-582c-42b1-bf25-f24a6f907f67
```

The TUI shows the selected provider, model, conversation, capabilities, and events without exposing endpoint credentials. Use `--no-banner` to suppress the welcome details. One-shot requests retain the plain stdout response and stderr runtime summary expected by scripts.

The repository also contains a local example instance at `.tmp/instance`. It is ignored by git and can be used immediately:

```bash
PIVOT_INSTANCE_PATH="$PWD/.tmp/instance" uv run pivot "Read the CPU count"
```

The example instance also includes a virtual D-Bus sensor:

```bash
uv add --project .tmp/instance/environment/measure "dbus-next>=0.2.3"
uv run --project .tmp/instance/environment/measure python .tmp/instance/capabilities/measure/virtual_sensor.py --serve
uv run --project .tmp/instance/environment/measure python .tmp/instance/capabilities/measure/virtual_sensor.py -r temperature
```

An empty instance is initialized as follows. Existing files are never overwritten:

```text
instance/
├── capabilities/{think,measure,work}/
├── dependencies/<dependency-project>/
├── environment/{think,measure,work,event}/
├── events/
├── logs/pivot.log
├── memory/<conversation-uuid>/history.jsonl
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
```

`PIVOT_PROVIDER` overrides the selected provider. Other runtime settings use `PIVOT_<NAME>`, then `config.toml`, then code defaults. Model names, API endpoints, and API keys are read only from `credentials.toml` and are never written to ordinary configuration or logs.

Terminal and persisted log levels are independent. Supported values are `debug`, `info`, `warn` and `error`:

```toml
[logging]
display_level = "info"
storage_level = "debug"
```

The corresponding environment variables are `PIVOT_LOG_DISPLAY_LEVEL` and `PIVOT_LOG_STORAGE_LEVEL`. The aliases `log_console_level`/`PIVOT_LOG_CONSOLE_LEVEL` and `log_file_level`/`PIVOT_LOG_FILE_LEVEL` are also accepted. Files rotate at 5 MiB and retain three backups. Terminal logs remain human-readable; persisted logs use JSON Lines and include `correlation_id`, `session_id`, and `agent_id` when available.

## Architecture

`pivot.config` bootstraps an instance; `pivot.logging` configures terminal and rotating-file output; `pivot.dependencies` installs, starts, checks, and stops external uv projects; `pivot.llm` wraps LiteLLM; `pivot.parser` extracts text and tool calls; `pivot.capabilities` validates and dispatches `think`, `measure` and `work` capabilities; `pivot.events` owns event definitions and FIFO waiters; `pivot.memory` writes UUID-isolated transcripts atomically; `pivot.session` runs the bounded conversation loop; and `pivot.orchestrator` runs independent agents concurrently.

Instance capability scripts are never imported by the pivot process. They use dedicated uv environments and JSON command protocols:

```text
think:   -l -> descriptor, -r -> full triple-quoted capability text
measure: -l -> descriptor, -r <feature> -> measured JSON value
work:    -l -> descriptor, -x + JSON stdin -> JSON execution result
```

Think descriptors are injected lazily. The model sees their names and summaries at first, then calls the built-in `pivot_read_think` tool only when it needs the full text. Work processes use a fixed instance directory, a restricted environment, a timeout, and an output limit. This is process isolation intended to protect the pivot interpreter; deployments executing hostile commands should add an operating-system sandbox.

Events are generic monitored fields rather than fixed thresholds. An event descriptor advertises a field and supported operators. The model calls `pivot_wait_event` with the chosen operator, expected value, and timeout. A matching value, timeout, or isolated source error is formatted with the event's injection template, appended as a tool result, and sent back to the LLM so the same conversation can continue.

Run the test suite with:

```bash
uv run --extra dev pytest
```
