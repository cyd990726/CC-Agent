"""Interactive, session-scoped tool permission handling."""

import difflib
from collections.abc import Callable, Mapping
from pathlib import Path
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

from agent.permissions import PermissionMode


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
        workspace: Path | None = None,
        mode: PermissionMode = PermissionMode.ASK,
        ask: Callable[[str], str] | None = None,
    ) -> None:
        self.console = console
        self.protected_tools = protected_tools
        self.workspace = workspace.resolve() if workspace is not None else None
        self.mode = mode
        self.allowed_for_session: set[str] = set()
        self._ask = ask or self._prompt

    def set_mode(self, mode: PermissionMode) -> None:
        self.mode = mode
        self.allowed_for_session.clear()

    def __call__(self, tool_name: str, args: Mapping[str, Any]) -> bool:
        if self.mode is PermissionMode.FULL:
            return True
        external_path = self._targets_external_path(args)
        if tool_name in self.allowed_for_session:
            return True
        if not external_path and tool_name not in self.protected_tools:
            return True
        if (
            not external_path
            and self.mode is PermissionMode.APPROVE
            and tool_name in {
                "edit_file",
                "write_file",
            }
        ):
            return True

        self._render_request(tool_name, args)
        answer = self._ask("选择权限")
        normalized = answer.strip().lower()
        if normalized in {"a", "always"}:
            self.allowed_for_session.add(tool_name)
            return True
        return normalized in {"y", "yes"}

    def _targets_external_path(self, args: Mapping[str, Any]) -> bool:
        value = args.get("path")
        if self.workspace is None or not isinstance(value, str) or not value:
            return False
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = self.workspace / path
        try:
            path.resolve().relative_to(self.workspace)
            return False
        except (OSError, RuntimeError, ValueError):
            return True

    def _prompt(self, _message: str) -> str:
        return self._select_permission()

    def select_mode(self, *, input=None, output=None) -> PermissionMode | None:
        """Show a keyboard-navigable permission mode selector."""

        options = (
            (
                PermissionMode.ASK,
                "Ask for approval",
                "修改文件、运行 Shell 或访问网络前需确认；访问工作区外路径也需确认。",
            ),
            (
                PermissionMode.APPROVE,
                "Approve for me",
                "工作区内文件修改自动批准；运行命令、联网和访问工作区外路径需确认。",
            ),
            (
                PermissionMode.FULL,
                "Full Access",
                "操作无需确认；文件工具可访问工作区外路径。",
            ),
        )
        selected = [next(i for i, item in enumerate(options) if item[0] is self.mode)]

        def option_fragments() -> FormattedText:
            fragments: list[tuple[str, str]] = [
                ("class:help", "  权限设置\n\n")
            ]
            for index, (mode, label, description) in enumerate(options):
                active = index == selected[0]
                current = "（当前）" if mode is self.mode else ""
                fragments.extend(
                    [
                        (
                            "class:option.cursor",
                            "  ❯ " if active else "    ",
                        ),
                        (
                            "class:option.selected" if active else "class:option",
                            f"{index + 1}. {label}{current}\n",
                        ),
                        ("class:option.description", f"      {description}\n"),
                    ]
                )
            return FormattedText(fragments)

        bindings = KeyBindings()

        def move(event, offset: int) -> None:
            selected[0] = (selected[0] + offset) % len(options)
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
            event.app.exit(result=options[selected[0]][0])

        @bindings.add("escape")
        @bindings.add("c-c")
        def cancel(event) -> None:
            event.app.exit(result=None)

        help_text = FormattedText(
            [
                ("class:help", "\n  "),
                ("class:help.key", "↑↓"),
                ("class:help", " 选择   "),
                ("class:help.key", "Enter"),
                ("class:help", " 确认   "),
                ("class:help.key", "Esc"),
                ("class:help", " 取消"),
            ]
        )
        application = Application(
            layout=Layout(
                HSplit(
                    [
                        Window(
                            FormattedTextControl(option_fragments),
                            height=len(options) * 2 + 2,
                        ),
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
            if name in {"content", "old_text", "new_text"}:
                continue
            details.add_row(Text(name, style="bright_black"), Text(str(value)))

        body = Table.grid()
        body.add_row(Text(labels.get(tool_name, tool_name), style="bold"))
        body.add_row(details)
        preview = self._change_preview(tool_name, args)
        if preview:
            body.add_row(self._style_diff(preview))
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

    def _change_preview(self, tool_name: str, args: Mapping[str, Any]) -> str:
        path_value = args.get("path")
        if not isinstance(path_value, str):
            return ""
        path = Path(path_value).expanduser()
        if self.workspace is not None and not path.is_absolute():
            path = self.workspace / path
        try:
            path = path.resolve()
        except (OSError, RuntimeError):
            return ""
        display_path = path_value

        if tool_name == "edit_file":
            old_text = args.get("old_text")
            new_text = args.get("new_text")
            if not isinstance(old_text, str) or not isinstance(new_text, str):
                return ""
            if self.workspace is None:
                return (
                    "Replacement preview (workspace boundary unavailable; "
                    "target file not read):\n"
                    + self._snippet_diff(old_text, new_text, display_path)
                )
            try:
                path.relative_to(self.workspace)
            except ValueError:
                return (
                    "External target; existing file contents are not read "
                    "before approval. Proposed replacement:\n"
                    + self._snippet_diff(old_text, new_text, display_path)
                )
            try:
                old_content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                return (
                    "Unable to read the workspace file for a full diff. "
                    "Proposed replacement:\n"
                    + self._snippet_diff(old_text, new_text, display_path)
                )
            matches = old_content.count(old_text)
            if matches != 1:
                return (
                    f"Cannot create the full diff: old text matches {matches} "
                    "times; the edit requires exactly one match."
                )
            updated = old_content.replace(old_text, new_text, 1)
            diff = difflib.unified_diff(
                old_content.splitlines(keepends=True),
                updated.splitlines(keepends=True),
                fromfile=f"a/{display_path}",
                tofile=f"b/{display_path}",
            )
            return "".join(diff)

        if tool_name != "write_file" or not isinstance(args.get("content"), str):
            return ""
        if self.workspace is None:
            return "File contents are not read before approval without a workspace boundary."
        try:
            path.relative_to(self.workspace)
        except ValueError:
            return (
                f"External file target: {display_path}. Existing contents "
                "are not read before approval."
            )
        new_content = str(args["content"])
        old_content = ""
        existed = False
        if path.is_file():
            existed = True
            try:
                old_content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                return (
                    f"Unable to read existing file for a diff ({exc}); "
                    "the write will replace its contents."
                )
        if existed and old_content == new_content:
            return (
                f"Contents are unchanged for {display_path}; "
                "the file will still be written."
            )
        if not existed and not new_content:
            return f"Create empty file: {display_path}"
        old_lines = old_content.splitlines(keepends=True)
        new_lines = new_content.splitlines(keepends=True)
        diff = difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"a/{display_path}" if existed else "/dev/null",
            tofile=f"b/{display_path}",
        )
        return "".join(diff)

    @staticmethod
    def _snippet_diff(old_text: str, new_text: str, path: str) -> str:
        old_lines = old_text.splitlines(keepends=True)
        new_lines = new_text.splitlines(keepends=True)
        return "".join(
            difflib.unified_diff(
                old_lines,
                new_lines,
                fromfile=f"a/{path} (snippet)",
                tofile=f"b/{path} (snippet)",
            )
        )

    @staticmethod
    def _style_diff(diff: str) -> Text:
        rendered = Text(no_wrap=False)
        for line in diff.splitlines(keepends=True):
            if line.startswith("+++") or line.startswith("---"):
                style = "bold cyan"
            elif line.startswith("+"):
                style = "green"
            elif line.startswith("-"):
                style = "red"
            elif line.startswith("@@"):
                style = "bright_cyan"
            else:
                style = "white"
            rendered.append(line, style=style)
        return rendered
