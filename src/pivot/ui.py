"""Terminal rendering helpers for the pivot CLI."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from .models import CapabilityDescriptor, EventDescriptor


ASCII_LOGO = r"""
 ____  _            _
|  _ \(_)_   _____ | |_
| |_) | \ \ / / _ \| __|
|  __/| |\ V / (_) | |_
|_|   |_| \_/ \___/ \__|
""".strip("\n")


@dataclass(frozen=True, slots=True)
class RuntimeSummary:
    """Safe, user-facing runtime metadata displayed at startup."""

    model: str
    endpoint: str
    session_id: str
    capabilities: tuple[CapabilityDescriptor, ...]
    events: tuple[EventDescriptor, ...]


def safe_endpoint(endpoint: str | None) -> str:
    """Remove credentials, query strings, and fragments from an endpoint."""

    if not endpoint:
        return "provider default"
    parsed = urlsplit(endpoint)
    hostname = parsed.hostname or ""
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port is not None:
        hostname = f"{hostname}:{port}"
    return urlunsplit((parsed.scheme, hostname, parsed.path, "", ""))


def render_banner(summary: RuntimeSummary) -> str:
    """Render the logo and a compact runtime summary."""

    capabilities = ", ".join(f"{item.kind}:{item.name}" for item in summary.capabilities) or "none"
    events = ", ".join(item.name for item in summary.events) or "none"
    return "\n".join(
        (
            ASCII_LOGO,
            f"Model        : {summary.model}",
            f"Endpoint     : {summary.endpoint}",
            f"Conversation : {summary.session_id}",
            f"Capabilities : {capabilities}",
            f"Events       : {events}",
        )
    )


__all__ = ["ASCII_LOGO", "RuntimeSummary", "render_banner", "safe_endpoint"]
