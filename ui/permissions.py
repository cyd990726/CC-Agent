"""Interactive, session-scoped tool permission handling."""

from collections.abc import Callable, Mapping
from typing import Any

from prompt_toolkit import Application
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


PERMISSION_OPTIONS = (
    ("y", "允许一次", "仅执行这一次"),
    ("a", "本次会话", "后续同类操作不再询问"),
    ("n", "拒绝", "不执行此操作"),
)

PERMISSION_STYLE = Style.from_dict(
    {
        "option": "#d4d4d8",
        "option.description": "#71717a",
        "option.cursor": "#22d3ee bold",
        "option.selected": "bg:#164e63 #ecfeff bold",
        "help": "#71717a",
        "help.key": "#a1a1aa bold",
    }
)


class SessionPermissionHandler:
    """Ask before sensitive tools and remember session-level approvals."""

    def __init__(
        self,
        console: Console,
        *,
        protected_tools: frozenset[str] = frozenset(
            {"edit_file", "write_file", "shell", "web_search", "fetch_url"}
        ),
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
        answer = self._ask("选择权限")
        normalized = answer.strip().lower()
        if normalized in {"a", "always"}:
            self.allowed_for_session.add(tool_name)
            return True
        return normalized in {"y", "yes"}

    def _prompt(self, _message: str) -> str:
        return self._select_permission()

    def _select_permission(self, *, input=None, output=None) -> str:
        selected = [len(PERMISSION_OPTIONS) - 1]

        def option_fragments() -> FormattedText:
            fragments: list[tuple[str, str]] = []
            for index, (_value, label, description) in enumerate(
                PERMISSION_OPTIONS
            ):
                active = index == selected[0]
                fragments.extend(
                    [
                        (
                            "class:option.cursor",
                            "  ❯ " if active else "    ",
                        ),
                        (
                            "class:option.selected" if active else "class:option",
                            f" {label:<8} ",
                        ),
                        ("class:option.description", f"  {description}"),
                        ("", "\n" if index < len(PERMISSION_OPTIONS) - 1 else ""),
                    ]
                )
            return FormattedText(fragments)

        help_text = FormattedText(
            [
                ("class:help", "    "),
                ("class:help.key", "↑↓"),
                ("class:help", " 选择   "),
                ("class:help.key", "Enter"),
                ("class:help", " 确认   "),
                ("class:help.key", "Esc"),
                ("class:help", " 拒绝"),
            ]
        )
        choices = FormattedTextControl(option_fragments)
        bindings = KeyBindings()

        def move(event, offset: int) -> None:
            selected[0] = (selected[0] + offset) % len(PERMISSION_OPTIONS)
            event.app.invalidate()

        @bindings.add("up")
        @bindings.add("left")
        @bindings.add("s-tab")
        def select_previous(event) -> None:
            move(event, -1)

        @bindings.add("down")
        @bindings.add("right")
        @bindings.add("tab")
        def select_next(event) -> None:
            move(event, 1)

        @bindings.add("enter")
        def confirm(event) -> None:
            event.app.exit(result=PERMISSION_OPTIONS[selected[0]][0])

        @bindings.add("escape")
        @bindings.add("c-d")
        def deny(event) -> None:
            event.app.exit(result="n")

        @bindings.add("c-c")
        def interrupt(event) -> None:
            event.app.exit(exception=KeyboardInterrupt)

        for key, _label, _description in PERMISSION_OPTIONS:
            bindings.add(key)(
                lambda event, value=key: event.app.exit(result=value)
            )

        application = Application(
            layout=Layout(
                HSplit(
                    [
                        Window(choices, height=len(PERMISSION_OPTIONS)),
                        Window(height=1),
                        Window(FormattedTextControl(help_text), height=1),
                    ]
                )
            ),
            key_bindings=bindings,
            style=PERMISSION_STYLE,
            full_screen=False,
            erase_when_done=True,
            input=input,
            output=output,
        )
        return application.run()

    def _render_request(self, tool_name: str, args: Mapping[str, Any]) -> None:
        labels = {
            "edit_file": "修改文件",
            "write_file": "写入文件",
            "shell": "执行命令",
            "web_search": "搜索网络",
            "fetch_url": "访问网页",
        }
        details = Table.grid(padding=(0, 2))
        details.add_column(style="bright_black", justify="right", no_wrap=True)
        details.add_column(style="white", overflow="fold")
        for name, value in args.items():
            if name in {"content", "old_text", "new_text"} and isinstance(value, str):
                rendered = f"{len(value):,} 个字符"
            else:
                rendered = str(value)
            details.add_row(name, rendered)

        body = Table.grid()
        body.add_row(Text(labels.get(tool_name, tool_name), style="bold"))
        body.add_row(details)
        self.console.print()
        self.console.print(
            Panel.fit(
                body,
                title="[bold yellow] 权限确认 [/]",
                border_style="bright_black",
                box=box.ROUNDED,
                padding=(0, 2),
            )
        )
