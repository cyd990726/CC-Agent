"""Events emitted while an agent run is in progress."""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any


class EventType(str, Enum):
    """Kinds of observable runtime activity."""

    RUN_STARTED = "run_started"
    MODEL_STARTED = "model_started"
    MODEL_COMPLETED = "model_completed"
    TOOL_REQUESTED = "tool_requested"
    TOOL_STARTED = "tool_started"
    TOOL_COMPLETED = "tool_completed"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"


@dataclass(frozen=True)
class AgentEvent:
    """A single immutable notification from the runtime."""

    type: EventType
    data: Mapping[str, Any]

