# pivot architecture

The runtime follows a one-way dependency flow from models/configuration to adapters and then to orchestration. Provider responses are converted at the boundary, so session code never imports LiteLLM types. Workspace scripts are optional runtime extensions: one broken script is logged and skipped while the rest of the workspace remains usable.

The first implementation provides in-process event registration and FIFO waiter delivery. A future DBus bridge can report into the same `EventPool.report` API without changing session code. Likewise, a future event supervisor can load scripts in its own uv project and send only validated notifications to the framework.
