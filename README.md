# pivot

pivot is a layered agent runtime for edge devices, industrial control and embodied systems. It keeps model access, response parsing, capability execution, events, memory and conversation orchestration replaceable through small Python interfaces.

## Quick start

The workspace is deliberately explicit. Set `PIVOT_WORKSPACE_PATH` (or pass `--workspace`) and let uv create the project environment:

```bash
uv sync --extra dev
PIVOT_WORKSPACE_PATH=/path/to/workspace uv run pivot "Hello"
```

An empty workspace is initialized with `config.toml`, capability directories, `events`, `memory` and `logs`. Add the LLM provider extra when using LiteLLM:

```bash
uv sync --extra llm
```

Configuration precedence is environment variable (`PIVOT_MODEL`, `PIVOT_MAX_ROUNDS`, etc.), then workspace `config.toml`, then defaults. API credentials are read by LiteLLM from its normal environment; pivot never stores them in the workspace.

## Architecture

`pivot.config` bootstraps a workspace; `pivot.llm` wraps LiteLLM; `pivot.parser` extracts text and tool calls; `pivot.capabilities` validates and dispatches `think`, `measure` and `work` capabilities; `pivot.events` owns event definitions and FIFO waiters; `pivot.memory` writes text transcripts atomically; `pivot.session` runs the bounded conversation loop; and `pivot.orchestrator` runs independent agents concurrently.

Measure scripts are invoked through `uv run --project <measure-environment> python <script> -l/-r <feature>` with a timeout. Their dependencies are therefore isolated from the framework interpreter.

Run the test suite with:

```bash
uv run --extra dev pytest
```
