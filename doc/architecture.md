# pivot architecture

The runtime follows a one-way dependency flow from models/configuration to adapters and then to orchestration. Provider responses are converted at the boundary, so session code never imports LiteLLM types. Workspace scripts are optional runtime extensions: one broken script is logged and skipped while the rest of the workspace remains usable. Runtime modules emit English diagnostics through the central logging configuration, with independent terminal and rotating-file thresholds.

The runtime provides in-process FIFO waiter delivery and an isolated event supervisor. Event scripts expose `-l` for JSON descriptors and `-p` for one JSON sensor poll; `EventSupervisor` evaluates the returned payload through `EventPool` and isolates script failures. A DBus/API data source belongs inside `environment/event` and can replace the sample poll without changing the framework boundary. Measure scripts execute through the workspace's dedicated `environment/measure` uv project.

Every conversation has a canonical UUID. Its JSON-lines transcript is stored atomically at `memory/<uuid>/history.jsonl` and restored when the UUID is resumed. The CLI can run one request or maintain a dynamic terminal conversation, while keeping model responses on stdout and runtime logs on stderr and disk.

The example measure workspace contains `virtual_sensor.py`, which starts a private `dbus-daemon`, exports `org.pivot.VirtualSensor` at `/org/pivot/VirtualSensor`, and serves `Read(feature)` through `dbus-next`. The pivot process only invokes the isolated client script, so the framework never imports the sensor dependency.
