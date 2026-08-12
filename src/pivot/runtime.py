"""Runtime assembly independent from any terminal client."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .capabilities import CapabilityRegistry
from .capabilities.discovery import register_workspace_capabilities
from .config import PivotConfig
from .dependencies import DependencyManager
from .events import EventPool, EventScriptRunner, EventService, EventSupervisor, load_event_scripts_isolated
from .llm import LiteLLMClient
from .memory import TextMemory
from .session import CancellationToken, ConversationSession, ProgressCallback, SessionManager

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Runtime:
    """Fully assembled pivot services, usable without the CLI."""

    config: PivotConfig
    registry: CapabilityRegistry
    events: EventPool
    event_service: EventService
    sessions: SessionManager
    dependencies: DependencyManager | None = None

    def close(self) -> None:
        """Release runtime-owned external processes."""

        if self.dependencies is not None:
            self.dependencies.close()


def build_runtime(config: PivotConfig) -> Runtime:
    """Build capability, event, LLM, memory, and session services."""

    dependencies = DependencyManager(
        config.workspace_path,
        install_timeout=config.dependency_install_timeout,
        start_timeout=config.dependency_start_timeout,
        dbus_timeout=config.dependency_dbus_timeout,
        stop_timeout=config.dependency_stop_timeout,
    )
    try:
        dependencies.start_all()
        registry = CapabilityRegistry()
        environment_root = config.workspace_path / "environment"
        register_workspace_capabilities(
            config.workspace_path,
            registry,
            environment_root,
            timeout=config.capability_timeout,
        )
        event_pool = EventPool()
        event_runner = EventScriptRunner(
            environment_root / "event",
            workspace=config.workspace_path,
            timeout=config.capability_timeout,
        )
        for event in load_event_scripts_isolated(str(config.workspace_path / "events"), event_runner):
            try:
                event_pool.register(event)
            except Exception as exc:
                LOGGER.warning("Unable to register workspace event %s: %s", event.name, exc)
        event_service = EventService(
            event_pool,
            EventSupervisor(event_pool, event_runner),
            poll_interval=config.event_poll_interval,
            max_wait=config.event_max_wait,
        )
        manager = SessionManager(
            llm=LiteLLMClient(
                config.provider.model,
                api_base=config.provider.api_base,
                api_key=config.provider.api_key,
                timeout=config.llm_timeout,
            ),
            capabilities=registry,
            memory=TextMemory(config.workspace_path / "memory"),
            events=event_pool,
            event_service=event_service,
            max_rounds=config.max_rounds,
        )
        LOGGER.info(
            "Runtime assembly completed dependencies=%d capabilities=%d events=%d",
            len(dependencies.descriptors()),
            len(registry.descriptors()),
            len(event_pool.descriptors()),
        )
        return Runtime(config, registry, event_pool, event_service, manager, dependencies)
    except BaseException:
        dependencies.close()
        raise


class PivotClient:
    """Provider-neutral application facade for non-CLI integrations."""

    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime

    @classmethod
    def open(cls, *, workspace_path: str | None = None) -> "PivotClient":
        """Load configuration and create a reusable pivot client."""

        return cls(build_runtime(PivotConfig.load(workspace_path=workspace_path)))

    def create_session(self) -> ConversationSession:
        """Create a new isolated conversation."""

        return self.runtime.sessions.create()

    def get_session(self, session_id: str) -> ConversationSession:
        """Load or create a canonical UUID conversation."""

        return self.runtime.sessions.get(session_id)

    def run(
        self,
        session_id: str,
        user_input: str,
        *,
        progress: ProgressCallback | None = None,
        cancellation: CancellationToken | None = None,
    ) -> str:
        """Run one conversation turn without involving terminal UI code."""

        return self.runtime.sessions.run(session_id, user_input, progress=progress, cancellation=cancellation)

    def sessions(self) -> tuple[ConversationSession, ...]:
        """List sessions currently managed by this client."""

        return self.runtime.sessions.sessions()

    def close(self) -> None:
        """Release dependency processes owned by this client runtime."""

        self.runtime.close()

    def __enter__(self) -> "PivotClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


__all__ = ["PivotClient", "Runtime", "build_runtime"]
