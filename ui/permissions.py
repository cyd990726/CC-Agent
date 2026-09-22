"""Interactive, session-scoped tool permission handling."""

from collections.abc import Callable, Mapping
from typing import Any

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.text import Text


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

        self._render_request(tool_name, args)
        answer = self._ask("允许执行？ [y] 一次  [a] 本次会话  [n] 拒绝")
        normalized = answer.strip().lower()
        if normalized == "a":
            self.allowed_for_session.add(tool_name)
            return True
        return normalized == "y"

    @staticmethod
    def _prompt(message: str) -> str:
        return Prompt.ask(message, choices=["y", "n", "a"], default="n")

    def _render_request(self, tool_name: str, args: Mapping[str, Any]) -> None:
        body = Text()
        labels = {"write_file": "Write file", "shell": "Run command"}
        body.append(labels.get(tool_name, tool_name), style="bold")
        for name, value in args.items():
            if name == "content" and isinstance(value, str):
                rendered = f"{len(value):,} characters"
            else:
                rendered = str(value)
            body.append(f"\n{name:<9}", style="bright_black")
            body.append(rendered)
        self.console.print()
        self.console.print(
            Panel.fit(
                body,
                title="[bold yellow] Permission required [/]",
                border_style="yellow",
                box=box.ROUNDED,
                padding=(0, 1),
            )
        )
