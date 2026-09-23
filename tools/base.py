"""Tool contract and dispatcher."""

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from agent.permissions import PermissionMode


READ_ONLY_TOOL_NAMES = frozenset(
    {"read_file", "find_files", "list_files", "search", "web_search", "fetch_url"}
)


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

    def set_full_access(self, enabled: bool) -> None:
        """Let tools with workspace boundaries expand them in Full Access mode."""

    def requires_full_access(self, args: Mapping[str, Any]) -> bool:
        """Return whether this call targets a resource outside its normal scope."""

        return False

    def set_temporary_full_access(self, enabled: bool) -> None:
        """Grant a one-call scope expansion after user approval."""


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

    def __init__(
        self,
        tools: Sequence[Tool],
        *,
        permission_handler: Callable[[str, Mapping[str, Any]], bool] | None = None,
        allowed_tools: frozenset[str] | None = None,
        permission_mode: PermissionMode = PermissionMode.ASK,
    ) -> None:
        self._tools: dict[str, Tool] = {}
        self._permission_handler = permission_handler
        self._allowed_tools = allowed_tools
        for tool in tools:
            if not tool.name:
                raise ValueError("tool name cannot be empty")
            if tool.name in self._tools:
                raise ValueError(f"duplicate tool name: {tool.name}")
            self._tools[tool.name] = tool
        self._permission_mode = PermissionMode.ASK
        self.set_permission_mode(permission_mode)

    def describe(self) -> list[dict[str, Any]]:
        return [
            tool.describe()
            for name, tool in self._tools.items()
            if self._allowed_tools is None or name in self._allowed_tools
        ]

    def set_allowed_tools(self, allowed_tools: frozenset[str] | None) -> None:
        """Restrict or restore the registered tools available to the agent."""

        self._allowed_tools = allowed_tools

    def set_permission_mode(self, mode: PermissionMode) -> None:
        """Apply a permission mode to tools and the interactive approval handler."""

        self._permission_mode = mode
        for tool in self._tools.values():
            tool.set_full_access(mode is PermissionMode.FULL)
        set_mode = getattr(self._permission_handler, "set_mode", None)
        if set_mode is not None:
            set_mode(mode)

    def execute(self, tool_name: str, args: Mapping[str, Any]) -> ToolResult:
        if self._allowed_tools is not None and tool_name not in self._allowed_tools:
            return ToolResult(
                tool_name,
                dict(args),
                False,
                f"tool {tool_name!r} is disabled by the current execution policy",
            )
        tool = self._tools.get(tool_name)
        if tool is None:
            available = ", ".join(self._tools) or "none"
            return ToolResult(
                tool=tool_name,
                args=dict(args),
                success=False,
                output=f"unknown tool {tool_name!r}; available tools: {available}",
            )
        external_access = tool.requires_full_access(args)
        if (
            external_access
            and self._permission_mode is not PermissionMode.FULL
            and self._permission_handler is None
        ):
            return ToolResult(
                tool_name,
                dict(args),
                False,
                "path must stay inside the workspace unless user approval grants "
                "outside access",
            )
        if self._permission_handler is not None:
            try:
                allowed = self._permission_handler(tool_name, args)
            except EOFError:
                allowed = False
            if not allowed:
                return ToolResult(
                    tool_name,
                    dict(args),
                    False,
                    "execution denied by user",
                )
        temporary_access = (
            external_access and self._permission_mode is not PermissionMode.FULL
        )
        if temporary_access:
            tool.set_temporary_full_access(True)
        try:
            output = tool.run(args)
            return ToolResult(tool_name, dict(args), True, str(output))
        except Exception as exc:  # A failed action is data the model can recover from.
            return ToolResult(
                tool_name, dict(args), False, f"{type(exc).__name__}: {exc}"
            )
        finally:
            if temporary_access:
                tool.set_temporary_full_access(False)


def require_string(args: Mapping[str, Any], name: str) -> str:
    value = args.get(name)
    if not isinstance(value, str) or not value:
        raise ToolError(f"{name} must be a non-empty string")
    return value
