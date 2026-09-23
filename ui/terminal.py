"""Persistent terminal REPL for Mini Agent."""

import asyncio
from collections.abc import Callable
from io import StringIO
from pathlib import Path

from prompt_toolkit import Application
from prompt_toolkit.application import get_app
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import ANSI, FormattedText
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.layout import (
    ConditionalContainer,
    HSplit,
    Layout,
    VSplit,
    Window,
)
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import TextArea
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from agent.runtime import AgentRuntime, AgentRuntimeError
from agent.permissions import PermissionMode
from model.llm import ModelError
from ui.permissions import SessionPermissionHandler
from ui.logo import LOGO_ANIMATION_FRAMES, render_logo
from ui.renderer import TerminalRenderer


COMMANDS = (
    ("/plan", "切换只读计划模式"),
    ("/permissions", "调整工具权限级别"),
    ("/clear", "清空终端"),
    ("/status", "查看当前配置"),
    ("/history", "查看本次会话任务"),
    ("/verbose", "展开或折叠工具输出"),
    ("/exit", "退出程序"),
)


class SlashCommandCompleter(Completer):
    """Offer slash commands only while editing the first input token."""

    def get_completions(self, document: Document, complete_event):
        value = document.text_before_cursor
        if not value.startswith("/") or any(character.isspace() for character in value):
            return
        normalized = value.lower()
        for command, description in COMMANDS:
            if command.startswith(normalized):
                yield Completion(
                    command,
                    start_position=-len(value),
                    display=command,
                    display_meta=description,
                )


class SlashCommandLexer(Lexer):
    """Color a slash command in the input field."""

    def lex_document(self, document: Document):
        def get_line(line_number: int):
            line = document.lines[line_number]
            if line.startswith("/"):
                command, separator, remainder = line.partition(" ")
                return [
                    ("class:input-field.command", command),
                    ("", separator + remainder),
                ]
            return [("", line)]

        return get_line


def _command_key_bindings(cycle_mode: Callable[[], None]) -> KeyBindings:
    bindings = KeyBindings()

    @bindings.add("tab")
    def complete_command(event) -> None:
        buffer = event.app.current_buffer
        if buffer.complete_state is None:
            buffer.start_completion(select_first=True)
        state = buffer.complete_state
        if state is not None and state.completions:
            index = state.complete_index or 0
            buffer.apply_completion(state.completions[index])

    @bindings.add("s-tab")
    def switch_mode(event) -> None:
        cycle_mode()
        event.app.invalidate()

    @bindings.add("enter")
    def submit_input(event) -> None:
        event.app.exit(result=event.app.current_buffer.text)

    @bindings.add("escape", "enter")
    @bindings.add("c-j")
    def insert_newline(event) -> None:
        event.app.current_buffer.insert_text("\n")

    @bindings.add("c-d")
    def exit_on_empty_input(event) -> None:
        buffer = event.app.current_buffer
        if buffer.text:
            buffer.delete()
        else:
            event.app.exit(exception=EOFError)

    return bindings


INPUT_STYLE = Style.from_dict(
    {
        "input-frame.border": "#52525b",
        "input-field": "#f4f4f5",
        "input-field.prompt": "bold ansicyan",
        "input-field.command": "bold #22d3ee",
        "session-status": "#71717a",
        "session-status.model": "#22d3ee bold",
        "session-status.separator": "#3f3f46",
        "session-status.path": "#a78bfa",
        "session-status.mode.accept": "#4ade80 bold",
        "session-status.mode.danger": "#ef4444 bold",
        "session-status.mode.plan": "#facc15 bold",
        "completion-panel.border": "#52525b",
        "completion-panel.title": "#71717a bold",
        "completion-panel.item": "#d4d4d8",
        "completion-panel.current": "bg:#164e63 #ecfeff bold",
        "completion-panel.match": "#67e8f9 bold",
    }
)


class TerminalApp:
    """Run multiple independent agent tasks in one terminal session."""

    def __init__(
        self,
        runtime: AgentRuntime,
        renderer: TerminalRenderer,
        console: Console,
        *,
        model_name: str,
        workspace: Path,
        plan_mode: bool = False,
        permission_handler: SessionPermissionHandler | None = None,
        prompt: Callable[[str], str] | None = None,
    ) -> None:
        self.runtime = runtime
        self.renderer = renderer
        self.console = console
        self.model_name = model_name
        self.workspace = workspace
        self.plan_mode = plan_mode
        self.permission_handler = permission_handler
        self._permission_mode_before_plan = (
            self._current_permission_mode() if plan_mode else None
        )
        self.history: list[str] = []
        self._input_history = InMemoryHistory()
        self._prompt = prompt or self._read_input
        self._logo_animation_frame = 0
        self._show_session_header = True

    def run(self) -> int:
        while True:
            try:
                task = self._prompt("❯ ").strip()
            except EOFError:
                self.console.print()
                return 0
            except KeyboardInterrupt:
                self.console.print("\n[bright_black]已取消输入 · /exit 退出[/]")
                continue

            if not task:
                continue
            if task.startswith("/"):
                if self._handle_command(task):
                    return 0
                continue

            self.history.append(task)
            self.run_task(task)

    def run_task(self, task: str) -> bool:
        try:
            self.runtime.run(task, on_event=self.renderer)
            return True
        except KeyboardInterrupt:
            self.renderer.close()
            self.console.print("\n[bold yellow]当前任务已中断。[/]")
        except (AgentRuntimeError, ModelError, OSError, ValueError):
            self.renderer.close()
        return False

    def _handle_command(self, command: str) -> bool:
        normalized = command.strip().lower()
        if normalized in {"/exit", "/quit"}:
            return True
        if normalized == "/plan":
            if self.plan_mode:
                mode = self._permission_mode_before_plan or PermissionMode.ASK
                self.plan_mode = False
                self._permission_mode_before_plan = None
                self.runtime.set_permission_mode(mode)
                self.runtime.set_plan_mode(False)
                self.console.print(
                    f"[bright_black]计划模式已关闭，恢复为"
                    f"{self._permission_mode_label(mode)}。[/]"
                )
            else:
                self._permission_mode_before_plan = self._current_permission_mode()
                self.plan_mode = True
                self.runtime.set_plan_mode(True)
                self.console.print("[bright_black]计划模式已开启（只读）。[/]")
        elif normalized == "/permissions":
            if self.permission_handler is not None:
                mode = self.permission_handler.select_mode()
                if mode is not None:
                    mode_changed = mode is not self.runtime.permission_mode
                    plan_was_active = self.plan_mode
                    self.runtime.set_permission_mode(mode)
                    if plan_was_active:
                        self.plan_mode = False
                        self._permission_mode_before_plan = None
                        self.runtime.set_plan_mode(False)
                    if mode_changed or plan_was_active:
                        self.console.print(
                            f"[bright_black]权限级别已切换为："
                            f"{self._permission_mode_label(mode)}。"
                            f"{'计划模式已关闭。' if plan_was_active else ''}[/]"
                        )
        elif normalized == "/clear":
            self.console.clear()
        elif normalized == "/status":
            self._show_status()
        elif normalized == "/history":
            if not self.history:
                self.console.print("[bright_black]本次会话还没有任务。[/]")
            else:
                for index, task in enumerate(self.history, 1):
                    self.console.print(
                        Text.assemble(
                            (f"{index:>2}  ", "bright_black"),
                            (task, "default"),
                        )
                    )
        elif normalized == "/verbose":
            enabled = self.renderer.toggle_verbose()
            label = "开启" if enabled else "关闭"
            self.console.print(f"[bright_black]完整工具输出已{label}。[/]")
        else:
            self.console.print(
                f"[yellow]未知命令：{command}。输入 / 查看可用命令。[/]"
            )
        return False

    def _header_content(self, frame: int = 0) -> Panel:
        body = Text()
        body.append("MINI AGENT", style="bold bright_cyan")
        body.append(
            "\nYour compact coding companion",
            style="bright_black",
        )
        body.append(f"\n\nModel       {self.model_name}", style="bright_black")
        body.append(
            f"\nDirectory  {self._display_path(self.workspace)}",
            style="bright_black",
        )
        if self.plan_mode:
            body.append("\nMode       Plan · read-only", style="bold yellow")
        elif self.permission_handler is not None:
            body.append(
                "\nPermissions  "
                f"{self._permission_mode_label(self.permission_handler.mode)}",
                style="bright_black",
            )
        content: Text | Table = body
        if self.console.width >= 56:
            content = Table.grid(padding=(0, 3))
            content.add_column(no_wrap=True)
            content.add_column(vertical="middle")
            content.add_row(
                render_logo(
                    color=self.console.color_system is not None,
                    frame=frame,
                ),
                body,
            )
        return Panel(
            content,
            subtitle="",
            subtitle_align="right",
            border_style="bright_black",
            box=box.ROUNDED,
            padding=(1, 2),
            expand=False,
        )

    def _show_status(self) -> None:
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column(style="bright_black")
        table.add_column()
        table.add_row("Model", self.model_name)
        table.add_row("Workspace", self._display_path(self.workspace))
        table.add_row("Mode", "Plan · read-only" if self.plan_mode else "Normal")
        if not self.plan_mode and self.permission_handler is not None:
            table.add_row(
                "Permissions",
                self._permission_mode_label(self.permission_handler.mode),
            )
        table.add_row("Tool output", "expanded" if self.renderer.verbose else "compact")
        table.add_row("Tasks", str(len(self.history)))
        self.console.print(
            Panel.fit(
                table,
                title="[bold]Session[/]",
                border_style="bright_black",
                box=box.ROUNDED,
            )
        )

    @staticmethod
    def _display_path(path: Path) -> str:
        try:
            relative = path.relative_to(Path.home())
        except (ValueError, RuntimeError):
            return str(path)
        return "~" if str(relative) == "." else f"~/{relative}"

    @staticmethod
    def _permission_mode_label(mode: PermissionMode) -> str:
        return {
            PermissionMode.ASK: "Ask for approval",
            PermissionMode.APPROVE: "Approve for me",
            PermissionMode.FULL: "Full Access",
        }[mode]

    def _read_input(self, _message: str) -> str:
        show_header = self._show_session_header
        if show_header:
            self._refresh_header_frame()
        input_field = TextArea(
            prompt=FormattedText([("class:input-field.prompt", "› ")]),
            multiline=True,
            wrap_lines=True,
            completer=SlashCommandCompleter(),
            lexer=SlashCommandLexer(),
            history=self._input_history,
            style="class:input-field",
        )

        def refresh_command_completions(buffer) -> None:
            value = buffer.document.text_before_cursor
            if value.startswith("/") and not any(char.isspace() for char in value):
                buffer.complete_state = None
                buffer.start_completion()
            else:
                buffer.complete_state = None

        input_field.buffer.on_text_changed += refresh_command_completions
        input_field.window.height = lambda: self._input_height(input_field.text)
        status = Window(
            height=1,
            content=FormattedTextControl(self._status_fragments),
            style="class:session-status",
        )
        header = ConditionalContainer(
            content=Window(
                content=FormattedTextControl(self._header_fragments),
                height=lambda: self._header_height,
                dont_extend_height=True,
            ),
            filter=Condition(lambda: self._show_session_header),
        )
        command_panel = ConditionalContainer(
            content=Window(
                content=FormattedTextControl(
                    lambda: self._completion_panel_fragments(input_field)
                ),
                height=lambda: self._completion_panel_height(input_field),
                dont_extend_height=True,
            ),
            filter=Condition(lambda: self._has_completions(input_field)),
        )
        bordered_input = HSplit(
            [
                Window(
                    height=1,
                    content=FormattedTextControl(
                        lambda: self._border_fragments("╭", "╮")
                    ),
                    style="class:input-frame.border",
                ),
                VSplit(
                    [
                        Window(
                            width=1,
                            char="│",
                            style="class:input-frame.border",
                        ),
                        input_field,
                        Window(
                            width=1,
                            char="│",
                            style="class:input-frame.border",
                        ),
                    ],
                ),
                Window(
                    height=1,
                    content=FormattedTextControl(
                        lambda: self._border_fragments("╰", "╯")
                    ),
                    style="class:input-frame.border",
                ),
            ]
        )
        container = HSplit(
            [
                header,
                bordered_input,
                command_panel,
                status,
            ]
        )
        application: Application[str] = Application(
            layout=Layout(container, focused_element=input_field),
            key_bindings=_command_key_bindings(self._cycle_mode),
            style=INPUT_STYLE,
            full_screen=False,
        )

        if self.console.is_terminal and show_header:
            async def animate_logo() -> None:
                while True:
                    await asyncio.sleep(0.5)
                    self._logo_animation_frame = (
                        self._logo_animation_frame + 1
                    ) % len(LOGO_ANIMATION_FRAMES)
                    self._refresh_header_frame()
                    application.invalidate()

            def start_animation() -> None:
                application.create_background_task(animate_logo())

            try:
                return application.run(pre_run=start_animation)
            finally:
                self._show_session_header = False
        try:
            return application.run()
        finally:
            self._show_session_header = False

    @staticmethod
    def _has_completions(input_field: TextArea) -> bool:
        state = input_field.buffer.complete_state
        return state is not None and bool(state.completions)

    @staticmethod
    def _completion_panel_height(input_field: TextArea) -> int:
        state = input_field.buffer.complete_state
        return len(state.completions) + 2 if state is not None else 0

    @staticmethod
    def _completion_panel_fragments(input_field: TextArea) -> FormattedText:
        state = input_field.buffer.complete_state
        if state is None or not state.completions:
            return FormattedText([])

        terminal_width = get_app().output.get_size().columns
        panel_width = min(52, terminal_width)
        inner_width = max(1, panel_width - 2)
        title = "─ Commands "
        top = "╭" + title + "─" * max(0, inner_width - get_cwidth(title)) + "╮"
        bottom = "╰" + "─" * inner_width + "╯"
        current = state.complete_index
        query = input_field.buffer.document.text_before_cursor
        match_length = len(query)
        fragments: list[tuple[str, str]] = [
            ("class:completion-panel.border", top + "\n")
        ]
        for index, completion in enumerate(state.completions):
            marker = "›" if index == current else " "
            label = completion.display_text
            meta = completion.display_meta_text
            content = TerminalApp._fit_cells(
                f" {marker} {label:<10} {meta}", inner_width
            )
            style = (
                "class:completion-panel.current"
                if index == current
                else "class:completion-panel.item"
            )
            fragments.extend(
                [
                    ("class:completion-panel.border", "│"),
                    (style, content[: get_cwidth(f" {marker} ")]),
                    (
                        "class:completion-panel.match",
                        completion.display_text[:match_length],
                    ),
                    (
                        style,
                        content[
                            get_cwidth(f" {marker} ")
                            + len(completion.display_text[:match_length]) :
                        ],
                    ),
                    ("class:completion-panel.border", "│\n"),
                ]
            )
        fragments.append(("class:completion-panel.border", bottom))
        return FormattedText(fragments)

    @staticmethod
    def _fit_cells(text: str, width: int) -> str:
        result: list[str] = []
        used = 0
        for character in text:
            character_width = get_cwidth(character)
            if used + character_width > width:
                break
            result.append(character)
            used += character_width
        return "".join(result) + " " * (width - used)

    @staticmethod
    def _input_height(text: str) -> int:
        size = get_app().output.get_size()
        return TerminalApp._measure_input_height(text, size.columns, size.rows)

    @staticmethod
    def _measure_input_height(text: str, columns: int, rows: int) -> int:
        content_width = max(1, columns - 4)
        visual_lines = sum(
            max(1, (get_cwidth(line) + content_width - 1) // content_width)
            for line in text.split("\n")
        )
        available_lines = max(1, rows - 6)
        return min(visual_lines, available_lines)

    @staticmethod
    def _border_fragments(left: str, right: str) -> FormattedText:
        width = get_app().output.get_size().columns
        return FormattedText(
            [
                (
                    "class:input-frame.border",
                    left + "─" * max(0, width - 2) + right,
                )
            ]
        )

    def _status_fragments(self) -> FormattedText:
        path = self._display_path(self.workspace)
        permission_mode = self._current_permission_mode()
        left_width = (
            2
            + get_cwidth(self.model_name)
            + get_cwidth("  ·  ")
            + get_cwidth(path)
        )
        if self.plan_mode:
            modes = [("plan mode on", "class:session-status.mode.plan")]
        elif permission_mode is PermissionMode.FULL:
            modes = [("full access on", "class:session-status.mode.danger")]
        elif permission_mode is PermissionMode.APPROVE:
            modes = [("accept edits on", "class:session-status.mode.accept")]
        else:
            modes = []
        mode_width = sum(get_cwidth(label) for label, _style in modes)
        fragments = [
            ("class:session-status", "  "),
            ("class:session-status.model", self.model_name),
            ("class:session-status.separator", "  ·  "),
            ("class:session-status.path", path),
        ]
        if modes:
            padding = " " * max(
                2, self.console.width - left_width - mode_width
            )
            fragments.append(("class:session-status", padding))
            for label, style in modes:
                fragments.append((style, label))
        return FormattedText(fragments)

    def _cycle_mode(self) -> None:
        """Cycle Ask → Accept edits → Full Access → Plan → Ask."""

        if self.plan_mode:
            self.plan_mode = False
            mode = PermissionMode.ASK
            self._permission_mode_before_plan = None
        elif self._current_permission_mode() is PermissionMode.ASK:
            mode = PermissionMode.APPROVE
        elif self._current_permission_mode() is PermissionMode.APPROVE:
            mode = PermissionMode.FULL
        else:
            self._permission_mode_before_plan = self._current_permission_mode()
            self.plan_mode = True
            mode = self._current_permission_mode()

        self.runtime.set_permission_mode(mode)
        self.runtime.set_plan_mode(self.plan_mode)

    def _current_permission_mode(self) -> PermissionMode:
        mode = getattr(self.runtime, "permission_mode", None)
        if isinstance(mode, PermissionMode):
            return mode
        if self.permission_handler is not None:
            return self.permission_handler.mode
        return PermissionMode.ASK

    def _header_fragments(self) -> ANSI:
        return ANSI(self._header_markup)

    def _refresh_header_frame(self) -> None:
        output = StringIO()
        header_console = Console(
            file=output,
            width=self.console.width,
            force_terminal=self.console.color_system is not None,
            color_system=self.console.color_system,
        )
        header_console.print(self._header_content(self._logo_animation_frame))
        self._header_markup = output.getvalue()
        self._header_height = max(1, self._header_markup.count("\n"))
