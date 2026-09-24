"""Core agent runtime package."""

from typing import Any

__all__ = [
    "AgentEvent",
    "AgentRuntime",
    "AgentRuntimeError",
    "AgentState",
    "EventType",
    "MaxStepsExceeded",
]


def __getattr__(name: str) -> Any:
    if name in {"AgentEvent", "EventType"}:
        from .events import AgentEvent, EventType

        return {"AgentEvent": AgentEvent, "EventType": EventType}[name]
    if name in {"AgentRuntime", "AgentRuntimeError", "MaxStepsExceeded"}:
        from .runtime import AgentRuntime, AgentRuntimeError, MaxStepsExceeded

        return {
            "AgentRuntime": AgentRuntime,
            "AgentRuntimeError": AgentRuntimeError,
            "MaxStepsExceeded": MaxStepsExceeded,
        }[name]
    if name == "AgentState":
        from .state import AgentState

        return AgentState
    raise AttributeError(name)
