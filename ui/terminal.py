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
from model.llm import ModelError
from ui.logo import LOGO_ANIMATION_FRAMES, render_logo
from ui.renderer import TerminalRenderer


COMMANDS = (
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


def _command_key_bindings() -> KeyBindings:
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
    def select_previous(event) -> None:
        buffer = event.app.current_buffer
        if buffer.complete_state is None:
            buffer.start_completion(select_last=True)
        else:
            buffer.complete_previous()

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
        prompt: Callable[[str], str] | None = None,
    ) -> None:
        self.runtime = runtime
        self.renderer = renderer
        self.console = console
        self.model_name = model_name
        self.workspace = workspace
        self.history: list[str] = []
        self._input_history = InMemoryHistory()
        self._prompt = prompt or self._read_input
        self._logo_animation_frame = 0

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
        if normalized == "/clear":
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

    def _read_input(self, _message: str) -> str:
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
        header = Window(
            content=FormattedTextControl(self._header_fragments),
            height=lambda: self._header_height,
            dont_extend_height=True,
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
            key_bindings=_command_key_bindings(),
            style=INPUT_STYLE,
            full_screen=False,
        )

        if self.console.is_terminal:
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

            return application.run(pre_run=start_animation)
        return application.run()

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
        return FormattedText(
            [
                ("class:session-status", "  "),
                ("class:session-status.model", self.model_name),
                ("class:session-status.separator", "  ·  "),
                ("class:session-status.path", self._display_path(self.workspace)),
            ]
        )

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
