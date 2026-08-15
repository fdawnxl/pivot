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

    provider: str
    model: str
    endpoint: str
    agent_id: str
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
    """Render the logo and runtime summary inside a terminal-friendly border."""

    capabilities = ", ".join(f"{item.kind}:{item.name}" for item in summary.capabilities) or "none"
    events = ", ".join(item.name for item in summary.events) or "none"
    return render_box(
        "\n".join(
            (
                ASCII_LOGO,
                "",
                f"Provider     : {summary.provider}",
                f"Model        : {summary.model}",
                f"Endpoint     : {summary.endpoint}",
                f"Main Agent  : {summary.agent_id}",
                f"Capabilities : {capabilities}",
                f"Events       : {events}",
            )
        ),
        title="PIVOT",
    )


def render_box(content: str, *, title: str | None = None, width: int | None = None) -> str:
    """Wrap header content in a compact terminal box."""

    lines = content.splitlines() or [""]
    inner_width = max(len(line) for line in lines)
    if width is not None:
        inner_width = max(inner_width, width)
    label = f" {title} " if title else ""
    if label:
        inner_width = max(inner_width, len(label) + 2)
        top = "╭─" + label + "─" * max(0, inner_width - len(label) - 2) + "╮"
    else:
        top = "╭" + "─" * (inner_width + 2) + "╮"
    body = [f"│ {line.ljust(inner_width)} │" for line in lines]
    return "\n".join([top, *body, "╰" + "─" * (inner_width + 2) + "╯"])


__all__ = [
    "ASCII_LOGO",
    "RuntimeSummary",
    "render_banner",
    "render_box",
    "safe_endpoint",
]
