"""Tool contract and dispatcher."""

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


class ToolError(RuntimeError):
    """A user-visible tool execution error."""

# 基类定义所有tools的共性
class Tool(ABC):
    """Base interface implemented by every tool."""

    name: str
    description: str
    args_schema: Mapping[str, Any]

    @abstractmethod
    def run(self, args: Mapping[str, Any]) -> str:
        """Execute the tool and return a textual observation."""

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "args": dict(self.args_schema),
        }


@dataclass(frozen=True)
class ToolResult:
    tool: str
    args: Mapping[str, Any]
    success: bool
    output: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "args": dict(self.args),
            "success": self.success,
            "output": self.output,
        }


class ToolExecutor:
    """Register tools by name and turn failures into model observations."""

    def __init__(self, tools: Sequence[Tool]) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            if not tool.name:
                raise ValueError("tool name cannot be empty")
            if tool.name in self._tools:
                raise ValueError(f"duplicate tool name: {tool.name}")
            self._tools[tool.name] = tool

    def describe(self) -> list[dict[str, Any]]:
        return [tool.describe() for tool in self._tools.values()]

    def execute(self, tool_name: str, args: Mapping[str, Any]) -> ToolResult:
        tool = self._tools.get(tool_name)
        if tool is None:
            available = ", ".join(self._tools) or "none"
            return ToolResult(
                tool=tool_name,
                args=dict(args),
                success=False,
                output=f"unknown tool {tool_name!r}; available tools: {available}",
            )
        try:
            output = tool.run(args)
            return ToolResult(tool_name, dict(args), True, str(output))
        except Exception as exc:  # A failed action is data the model can recover from.
            return ToolResult(
                tool_name, dict(args), False, f"{type(exc).__name__}: {exc}"
            )


def require_string(args: Mapping[str, Any], name: str) -> str:
    value = args.get(name)
    if not isinstance(value, str) or not value:
        raise ToolError(f"{name} must be a non-empty string")
    return value
