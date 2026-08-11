# pivot

pivot is a layered agent runtime for edge devices, industrial control and embodied systems. It keeps model access, response parsing, capability execution, events, memory and conversation orchestration replaceable through small Python interfaces.

## Quick start

The workspace is deliberately explicit. Set `PIVOT_WORKSPACE_PATH` (or pass `--workspace`) and let uv create the project environment:

```bash
uv sync --extra dev
PIVOT_WORKSPACE_PATH=/path/to/workspace uv run pivot "Hello"
```

The repository also contains a local example workspace at `.tmp/workspace`. It is ignored by git and can be used immediately:

```bash
PIVOT_WORKSPACE_PATH="$PWD/.tmp/workspace" uv run pivot "Read the CPU count"
```

The example workspace also includes a virtual D-Bus sensor:

```bash
uv sync --project .tmp/workspace/measure-env
uv run --project .tmp/workspace/measure-env python .tmp/workspace/capabilities/measure/virtual_sensor.py --serve
uv run --project .tmp/workspace/measure-env python .tmp/workspace/capabilities/measure/virtual_sensor.py -r temperature
```

An empty workspace is initialized with `config.toml`, capability directories, `events`, `memory` and `logs`. LiteLLM is a core dependency and is installed by `uv sync`:

```bash
uv sync
```

Configuration precedence is environment variable (`PIVOT_MODEL`, `PIVOT_API_BASE`, `PIVOT_API_KEY`, `PIVOT_MAX_ROUNDS`, etc.), then workspace `config.toml`/`credentials.json`, then code defaults. `credentials.json` is owner-readable only and is never written to `config.toml` or logs.

## Architecture

`pivot.config` bootstraps a workspace; `pivot.llm` wraps LiteLLM; `pivot.parser` extracts text and tool calls; `pivot.capabilities` validates and dispatches `think`, `measure` and `work` capabilities; `pivot.events` owns event definitions and FIFO waiters; `pivot.memory` writes text transcripts atomically; `pivot.session` runs the bounded conversation loop; and `pivot.orchestrator` runs independent agents concurrently.

Measure scripts are invoked through `uv run --project <measure-environment> python <script> -l/-r <feature>` with a timeout. Their dependencies are therefore isolated from the framework interpreter. The CLI automatically registers valid scripts in `capabilities/` and event descriptors in `events/`.

Run the test suite with:

```bash
uv run --extra dev pytest
```
