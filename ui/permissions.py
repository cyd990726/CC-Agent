"""Interactive, session-scoped tool permission handling."""

from collections.abc import Callable, Mapping
from typing import Any

from rich.console import Console
from rich.prompt import Prompt


class SessionPermissionHandler:
    """Ask before sensitive tools and remember session-level approvals."""

    def __init__(
        self,
        console: Console,
        *,
        protected_tools: frozenset[str] = frozenset({"write_file", "shell"}),
        ask: Callable[[str], str] | None = None,
    ) -> None:
        self.console = console
        self.protected_tools = protected_tools
        self.allowed_for_session: set[str] = set()
        self._ask = ask or self._prompt

    def __call__(self, tool_name: str, args: Mapping[str, Any]) -> bool:
        if (
            tool_name not in self.protected_tools
            or tool_name in self.allowed_for_session
        ):
            return True

        self.console.print("\n[bold yellow]Agent 请求执行敏感操作：[/]")
        self._render_request(tool_name, args)
        answer = self._ask("允许？[y] 一次 / [n] 拒绝 / [a] 本次会话始终允许")
        normalized = answer.strip().lower()
        if normalized == "a":
            self.allowed_for_session.add(tool_name)
            return True
        return normalized == "y"

    @staticmethod
    def _prompt(message: str) -> str:
        return Prompt.ask(message, choices=["y", "n", "a"], default="n")

    def _render_request(self, tool_name: str, args: Mapping[str, Any]) -> None:
        self.console.print(f"  [bold]{tool_name}[/]")
        for name, value in args.items():
            if name == "content" and isinstance(value, str):
                rendered = f"<{len(value)} characters>"
            else:
                rendered = repr(value)
            self.console.print(f"  [dim]{name}:[/] {rendered}")

