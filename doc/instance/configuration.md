# Instance configuration

Configuration precedence is always environment variable, then `config.toml`, then the code default. A setting named `max_rounds` maps to `PIVOT_MAX_ROUNDS`.

`config.toml` may place settings at the root or under `[pivot]`. Use `[logging]` with root settings, or `[pivot.logging]` when using the wrapper table; do not mix the two layouts.

## Provider credentials

`credentials.toml` contains named provider connections:

```toml
[providers.edge]
model = "openai/device-model"
api_base = "http://127.0.0.1:8000/v1"
api_key = "secret"

[providers.cloud]
model = "openai/gpt-4o-mini"
api_key = "secret"
```

Required field: `model`. Optional fields: `api_base`, `api_key`. The selected name comes from `provider` or `PIVOT_PROVIDER`. pivot forces the credentials file to mode `0600` when it reads or writes it.

## Model strategy groups

The selected `provider` is always the main Agent's primary provider. A model strategy group adds ordered fallbacks:

```toml
provider = "edge"
main_model_group = "interactive"

[model_groups.interactive]
description = "Low-latency interaction with a cloud fallback"
capabilities = ["text", "tool_use"]
cost = "medium"
providers = ["edge", "cloud"]
```

Every name in `providers` must reference a table in `credentials.toml`. Put the selected `provider` first: the runtime uses `provider` as the primary and tries entries after the first group member in order when a request raises an exception. If all attempts fail, the activation receives a sanitized `LLMError`.

`description`, `capabilities`, and `cost` are strategy metadata retained in `PivotConfig`; they do not currently select a group or route individual requests automatically. `main_model_group` selects the group used by the main Agent. Without it, or when it does not match a defined group, only `provider` is used. Strategy group tables are configured in TOML; `PIVOT_MAIN_MODEL_GROUP` can override the selected group.

## Runtime settings

| `config.toml` key | Default | Purpose |
| --- | ---: | --- |
| `provider` | required | Named table in `credentials.toml` |
| `main_model_group` | unset | Named ordered fallback group for the main Agent |
| `max_rounds` | `8` | Model/action rounds allowed per finite activation or recurring occurrence |
| `max_workers` | `4` | Maximum concurrent worker activations |
| `llm_timeout` | `120` | LLM request timeout in seconds |
| `capability_timeout` | `15` | Capability and event script timeout |
| `executor_timeout` | `30` | Maximum shell executor timeout |
| `executor_max_output_bytes` | `1048576` | Per-stream executor output cap |
| `event_poll_interval` | `1` | Background event polling interval |
| `event_max_wait` | `3600` | Maximum single Agent event wait |
| `stimulus_max_pending` | `1000` | Maximum queued or processing stimuli |
| `stimulus_retention_seconds` | `604800` | Retention for terminal stimuli |
| `stimulus_priority_aging_seconds` | `5` | Seconds per effective priority aging step |
| `agent_worker_retention_seconds` | `300` | Retention for terminal one-shot workers |
| `agent_worker_cleanup_interval` | `30` | Worker cleanup scan interval |
| `dependency_install_timeout` | `300` | Initial dependency `uv sync` timeout |
| `dependency_start_timeout` | `15` | Dependency readiness timeout |
| `dependency_dbus_timeout` | `1` | One dependency D-Bus query timeout |
| `dependency_stop_timeout` | `5` | Graceful dependency stop timeout |
| `dbus_control_enabled` | `true` | Export the pivot control service |
| `dbus_control_bus` | `session` | `session` or `system` |
| `dbus_control_service` | `org.pivot.Control` | Well-known control service name |
| `dbus_control_start_timeout` | `5` | Control service startup timeout |

All numeric limits and intervals must be positive. `stimulus_max_pending`, `executor_max_output_bytes`, `max_rounds`, and `max_workers` must be positive integers.

## Logging

```toml
[logging]
display_level = "info"
storage_level = "debug"
```

Accepted levels are `debug`, `info`, `warn`, and `error`. Preferred environment variables are `PIVOT_LOG_DISPLAY_LEVEL` and `PIVOT_LOG_STORAGE_LEVEL`. The aliases `PIVOT_LOG_CONSOLE_LEVEL` and `PIVOT_LOG_FILE_LEVEL` remain supported.

Terminal logs are human-readable. `logs/pivot.log` uses JSON Lines, rotates at 5 MiB, retains three backups, and includes correlation, activation, and Agent identifiers when available. Dependency stdout and stderr go to `logs/dependencies/<id>.log`.

## Example production profile

```toml
provider = "edge"
main_model_group = "interactive"
max_rounds = 10
max_workers = 4
llm_timeout = 60
capability_timeout = 8
executor_timeout = 10
executor_max_output_bytes = 262144
event_poll_interval = 0.25
event_max_wait = 1800
stimulus_max_pending = 500
stimulus_retention_seconds = 259200
stimulus_priority_aging_seconds = 5
agent_worker_retention_seconds = 300
agent_worker_cleanup_interval = 30
dependency_install_timeout = 300
dependency_start_timeout = 20
dependency_dbus_timeout = 1
dependency_stop_timeout = 5
dbus_control_enabled = true
dbus_control_bus = "session"
dbus_control_service = "org.pivot.Control"
dbus_control_start_timeout = 5

[model_groups.interactive]
description = "Prefer the on-device model and fall back to cloud"
capabilities = ["text", "tool_use"]
cost = "medium"
providers = ["edge", "cloud"]

[logging]
display_level = "info"
storage_level = "debug"
```

Environment overrides are useful for deployment-specific scalar values, but provider secrets still belong in the instance credentials file rather than ordinary configuration or logs. Structured tables such as `model_groups` remain in `config.toml`.
