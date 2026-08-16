"""pivot: a layered agent framework for edge and embodied systems."""

from .actions import ACTION_TOOL, ActionDetector, ActionKind, ActionRequest
from .activation import ActivationState, AgentCancelled, CancellationToken
from .config import PivotConfig
from .control import ControlError, PivotControl
from .dbus_control import ControlDBusService
from .dependencies import (
    DependencyDBus,
    DependencyManager,
    DependencyState,
    DependencyStatus,
)
from .executors import (
    ExecutorDescriptor,
    ExecutorError,
    ExecutorRegistry,
    ShellExecutor,
)
from .lease import RuntimeLeaseError
from .runtime import PivotClient
from .stimuli import (
    OutputEnvelope,
    StimulusDelivery,
    StimulusEnvelope,
    StimulusError,
    StimulusKind,
    StimulusState,
)

__all__ = [
    "ACTION_TOOL",
    "ActionDetector",
    "ActionKind",
    "ActionRequest",
    "AgentCancelled",
    "ActivationState",
    "CancellationToken",
    "ControlDBusService",
    "ControlError",
    "DependencyManager",
    "DependencyDBus",
    "DependencyState",
    "DependencyStatus",
    "ExecutorError",
    "ExecutorDescriptor",
    "ExecutorRegistry",
    "PivotClient",
    "PivotConfig",
    "PivotControl",
    "RuntimeLeaseError",
    "ShellExecutor",
    "OutputEnvelope",
    "StimulusEnvelope",
    "StimulusDelivery",
    "StimulusError",
    "StimulusKind",
    "StimulusState",
]
__version__ = "0.1.0"
