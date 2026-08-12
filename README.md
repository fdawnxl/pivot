# pivot

pivot is a layered agent runtime for edge devices, industrial control and embodied systems. It keeps model access, response parsing, capability execution, events, memory and conversation orchestration replaceable through small Python interfaces.

## Quick start

The workspace is deliberately explicit. Set `PIVOT_WORKSPACE_PATH` (or pass `--workspace`) and let uv create the project environment. Omit the message to start an interactive conversation:

```bash
uv sync --extra dev
PIVOT_WORKSPACE_PATH=/path/to/workspace uv run pivot
PIVOT_WORKSPACE_PATH=/path/to/workspace uv run pivot "Hello"
```

The interactive CLI keeps one UUID conversation active and supports `/help`, `/session`, `/new` and `/exit`. Resume an existing conversation with:

```bash
PIVOT_WORKSPACE_PATH=/path/to/workspace uv run pivot --session 4b3c9f24-582c-42b1-bf25-f24a6f907f67
```

At startup pivot displays its ASCII logo, model, endpoint, conversation UUID, capabilities and events. Use `--no-banner` to suppress that summary.

The repository also contains a local example workspace at `.tmp/workspace`. It is ignored by git and can be used immediately:

```bash
PIVOT_WORKSPACE_PATH="$PWD/.tmp/workspace" uv run pivot "Read the CPU count"
```

The example workspace also includes a virtual D-Bus sensor:

```bash
uv add --project .tmp/workspace/environment/measure "dbus-next>=0.2.3"
uv run --project .tmp/workspace/environment/measure python .tmp/workspace/capabilities/measure/virtual_sensor.py --serve
uv run --project .tmp/workspace/environment/measure python .tmp/workspace/capabilities/measure/virtual_sensor.py -r temperature
```

An empty workspace is initialized as follows. Existing files are never overwritten:

```text
workspace/
├── capabilities/{think,measure,work}/
├── environment/{measure,event}/
├── events/
├── logs/pivot.log
├── memory/<conversation-uuid>/history.jsonl
├── config.toml
└── credentials.toml        # named LLM providers, mode 0600
```

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

`pivot.config` bootstraps a workspace; `pivot.logging` configures terminal and rotating-file output; `pivot.llm` wraps LiteLLM; `pivot.parser` extracts text and tool calls; `pivot.capabilities` validates and dispatches `think`, `measure` and `work` capabilities; `pivot.events` owns event definitions and FIFO waiters; `pivot.memory` writes UUID-isolated transcripts atomically; `pivot.session` runs the bounded conversation loop; and `pivot.orchestrator` runs independent agents concurrently.

Measure scripts are invoked through `uv run --project <measure-environment> python <script> -l/-r <feature>` with a timeout. Their dependencies are therefore isolated from the framework interpreter. The CLI automatically registers valid scripts in `capabilities/` and event descriptors in `events/`.

Run the test suite with:

```bash
uv run --extra dev pytest
```
