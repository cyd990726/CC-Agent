"""Persistent terminal REPL for Mini Agent."""

import asyncio
import queue
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Any

from prompt_toolkit import Application
from prompt_toolkit.buffer import CompletionState
from prompt_toolkit.application import get_app
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import ANSI, FormattedText, to_formatted_text
from prompt_toolkit.formatted_text.utils import split_lines
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.layout import (
    ConditionalContainer,
    Dimension,
    HSplit,
    Layout,
    VSplit,
    Window,
)
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.mouse_events import MouseEvent, MouseEventType
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import TextArea
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from agent.runtime import AgentRuntime, AgentRuntimeError
from agent.cancellation import CancellationToken, RunCancelled
from agent.permissions import PermissionMode
from model.llm import ModelError
from ui.permissions import (
    PERMISSION_LABELS, PERMISSION_OPTIONS, SessionPermissionHandler, permission_choices,
)
from ui.logo import render_logo
from ui.renderer import TerminalRenderer
from ui.transcript import TranscriptBlock
from ui.selection import TranscriptSelectionControl, copy_to_clipboard


COMMANDS = (
    ("/plan", "切换只读计划模式"),
    ("/permissions", "调整工具权限级别"),
    ("/clear", "清空终端"),
    ("/status", "查看当前配置"),
    ("/history", "查看本次会话任务"),
    ("/verbose", "展开或折叠工具输出"),
    ("/exit", "退出程序"),
)


@dataclass
class _PermissionRequest:
    tool_name: str
    args: dict[str, Any]
    ready: threading.Event
    answer: str = "n"


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


class _TranscriptWindow(Window):
    """Route terminal wheel gestures into transcript navigation."""

    def __init__(
        self,
        *args: Any,
        on_scroll: Callable[[int], None],
        get_target_line: Callable[[], int],
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._on_scroll = on_scroll
        self._get_target_line = get_target_line
        self._scroll_delta = 0
        self._view_anchor: tuple[int, int] | None = None

    def scroll_rows(self, delta: int) -> None:
        if self._view_anchor is None:
            self._view_anchor = (self.vertical_scroll, self.vertical_scroll_2)
        self._scroll_delta += delta

    def follow_bottom(self) -> None:
        self._view_anchor = None
        self._scroll_delta = 0

    def _scroll(self, ui_content, width: int, height: int) -> None:
        """Scroll read-only content without consulting a synthetic cursor."""

        self.horizontal_scroll = 0
        self.vertical_scroll_2 = 0
        if width <= 0 or height <= 0 or ui_content.line_count <= 0:
            self.vertical_scroll = 0
            return

        target = min(
            max(0, self._get_target_line()),
            ui_content.line_count - 1,
        )
        target_height = ui_content.get_height_for_line(
            target, width, self.get_line_prefix
        )
        if target_height > height:
            self.vertical_scroll = target
            self.vertical_scroll_2 = target_height - height
            self._apply_row_scroll(ui_content, width)
            return

        used_height = 0
        first_visible = target
        for line_number in range(target, -1, -1):
            line_height = ui_content.get_height_for_line(
                line_number, width, self.get_line_prefix
            )
            if used_height + line_height > height:
                first_visible = line_number
                self.vertical_scroll_2 = used_height + line_height - height
                break
            used_height += line_height
            first_visible = line_number
        self.vertical_scroll = first_visible
        self._apply_row_scroll(ui_content, width)

    def _apply_row_scroll(self, content, width: int) -> None:
        if self._view_anchor is None:
            return
        bottom = (self.vertical_scroll, self.vertical_scroll_2)
        line, row = self._view_anchor
        line = min(line, content.line_count - 1)
        row = min(row, content.get_height_for_line(line, width, self.get_line_prefix) - 1)
        delta = self._scroll_delta
        self._scroll_delta = 0
        row += delta
        while row < 0 and line > 0:
            line -= 1
            row += content.get_height_for_line(line, width, self.get_line_prefix)
        while line < content.line_count - 1:
            line_height = content.get_height_for_line(line, width, self.get_line_prefix)
            if row < line_height:
                break
            row -= line_height
            line += 1
        anchor = min((line, max(0, row)), bottom)
        self.vertical_scroll, self.vertical_scroll_2 = anchor
        self._view_anchor = None if delta > 0 and anchor == bottom else anchor

    def _mouse_handler(self, mouse_event: MouseEvent):
        if mouse_event.event_type is MouseEventType.SCROLL_UP:
            self._on_scroll(-3)
            return None
        if mouse_event.event_type is MouseEventType.SCROLL_DOWN:
            self._on_scroll(3)
            return None
        return super()._mouse_handler(mouse_event)


INPUT_STYLE = Style.from_dict(
    {
        "input-frame.border": "#52525b",
        "transcript": "#d4d4d8",
        "input-field": "#f4f4f5",
        "input-field.prompt": "bold ansicyan",
        "input-field.command": "bold #22d3ee",
        "session-status": "#71717a",
        "session-status.model": "#22d3ee bold",
        "session-status.separator": "#3f3f46",
        "session-status.path": "#a78bfa",
        "session-status.activity": "#22d3ee",
        "session-status.mode.accept": "#4ade80 bold",
        "session-status.mode.danger": "#ef4444 bold",
        "session-status.mode.plan": "#facc15 bold",
        "completion-panel.border": "#52525b",
        "completion-panel.title": "#71717a bold",
        "completion-panel.item": "#d4d4d8",
        "completion-panel.current": "bg:#164e63 #ecfeff bold",
        "completion-panel.match": "#67e8f9 bold",
        "overlay": "bg:#18181b",
        "overlay.title": "#facc15 bold",
        "overlay.detail": "#a1a1aa",
        "overlay.cursor": "#22d3ee bold",
        "overlay.option": "#d4d4d8",
        "overlay.selected": "bg:#164e63 #ecfeff bold",
        "overlay.help": "#71717a",
        "approval.border": "#725844",
        "approval.title": "#d7a582 bold",
        "approval.selected": "#d7a582 bold",
        "approval.detail": "#a1a1aa",
        "approval.option": "#d4d4d8",
        "approval.help": "#71717a",
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
        self._prompt = prompt
        self._uses_terminal_prompt = prompt is None
        self._task_queue: queue.Queue[str | None] = queue.Queue()
        self._permission_requests: queue.Queue[_PermissionRequest] = queue.Queue()
        self._state_lock = threading.Lock()
        self._pending_tasks = 0
        self._invalidate_input: Callable[[], None] | None = None
        self._exit_application: Callable[[], None] | None = None
        self._ui_active = False
        self._transcript: list[str] = []
        self._transcript_blocks: list[TranscriptBlock] = []
        self._transcript_width = console.width
        self._transcript_styled: list[tuple[str, str]] = []
        self._transcript_lines = 1
        self._transcript_cursor_line: int | None = None
        self._active_permission: _PermissionRequest | None = None
        self._permission_selection = len(PERMISSION_OPTIONS) - 1
        self._mode_selector_open = False
        self._mode_selection = 0
        self._activity_message: str | None = None
        self._activity_started_at: float | None = None
        self._application: Application[Any] | None = None
        self._input_field: TextArea | None = None
        self._transcript_window: _TranscriptWindow | None = None
        self._selection_control: TranscriptSelectionControl | None = None
        self._copy_notice = ""
        self._exit_requested = False
        self._closing = False
        self._active_cancellation: CancellationToken | None = None

    def run(self) -> int:
        if not self._uses_terminal_prompt:
            return self._run_scripted()
        return self._run_interactive()

    def _run_scripted(self) -> int:
        worker = threading.Thread(target=self._task_worker, daemon=True)
        worker.start()
        try:
            while True:
                try:
                    task = self._prompt("❯ ").strip()
                except EOFError:
                    self.console.print()
                    break
                except KeyboardInterrupt:
                    self.console.print(
                        "\n[bright_black]已取消输入 · /exit 退出[/]"
                    )
                    continue
                if not task:
                    continue
                if task.startswith("/"):
                    if self._handle_command(task):
                        break
                    continue
                self.history.append(task)
                self._enqueue_task(task)
        finally:
            self._task_queue.put(None)
            worker.join()
        return 0

    def _run_interactive(self) -> int:
        self._refresh_header_frame()
        self._transcript = [self._header_markup]
        self._transcript_styled = list(
            to_formatted_text(ANSI(self._header_markup))
        )
        self._transcript_lines = self._fragment_line_count(
            self._transcript_styled
        )
        self._transcript_blocks = [TranscriptBlock((self._header_content(),))]
        self.renderer.set_activity_handler(self._update_activity)
        self.renderer.set_output_handler(self._write_transcript)
        self.renderer.set_render_handler(self._append_renderables)
        if self.permission_handler is not None:
            self.permission_handler.set_request_callback(
                self._request_permission
            )
        application = self._create_application()
        self._application = application
        worker = threading.Thread(target=self._task_worker, daemon=True)

        def start() -> None:
            loop = asyncio.get_running_loop()
            with self._state_lock:
                self._ui_active = True
                self._invalidate_input = lambda: loop.call_soon_threadsafe(
                    application.invalidate
                )
                self._exit_application = lambda: loop.call_soon_threadsafe(
                    application.exit
                )
            worker.start()
            application.create_background_task(self._refresh_ui())

        try:
            application.run(pre_run=start)
        finally:
            with self._state_lock:
                self._closing = True
                cancellation = self._active_cancellation
            if cancellation is not None:
                cancellation.cancel()
            self._deny_pending_permissions()
            self._task_queue.put(None)
            worker.join()
            with self._state_lock:
                self._ui_active = False
                self._invalidate_input = None
                self._exit_application = None
            self.renderer.set_activity_handler(None)
            self.renderer.set_output_handler(None)
            self.renderer.set_render_handler(None)
            if self.permission_handler is not None:
                self.permission_handler.set_request_callback(None)
            self._application = None
            transcript = "".join(self._transcript)
            if transcript:
                file = self.console.file
                file.write(transcript)
                file.flush()
        return 0

    def _deny_pending_permissions(self) -> None:
        if self._active_permission is not None:
            request = self._active_permission
            self._active_permission = None
            request.answer = "n"
            request.ready.set()
            self._permission_requests.task_done()
        while True:
            try:
                request = self._permission_requests.get_nowait()
            except queue.Empty:
                return
            request.answer = "n"
            request.ready.set()
            self._permission_requests.task_done()

    def _enqueue_task(self, task: str) -> None:
        with self._state_lock:
            was_busy = self._pending_tasks > 0
            self._pending_tasks += 1
            position = self._pending_tasks
        self._task_queue.put(task)
        if was_busy:
            self._print_message(
                f"[bright_black]  ↳ 已排队 · 第 {position} 个任务[/]"
            )

    def _task_worker(self) -> None:
        while True:
            task = self._task_queue.get()
            if task is None:
                self._task_queue.task_done()
                return
            with self._state_lock:
                cancellation = CancellationToken()
                self._active_cancellation = cancellation
                if self._closing:
                    cancellation.cancel()
            try:
                if self._ui_active:
                    self._print_message(
                        Text.assemble(
                            ("› ", "bold bright_cyan"),
                            (task, "default"),
                        )
                    )
                self.run_task(task, cancellation=cancellation)
            finally:
                with self._state_lock:
                    self._active_cancellation = None
                    if self._activity_message == "Cancelling":
                        self._activity_message = None
                        self._activity_started_at = None
                    self._pending_tasks -= 1
                    should_exit = (
                        self._pending_tasks == 0 and self._exit_requested
                    )
                    invalidate = self._invalidate_input
                    exit_application = self._exit_application
                self._task_queue.task_done()
                if should_exit and exit_application is not None:
                    exit_application()
                elif invalidate is not None:
                    invalidate()

    def _request_permission(
        self, tool_name: str, args: Mapping[str, Any]
    ) -> str:
        request = _PermissionRequest(
            tool_name,
            dict(args),
            threading.Event(),
        )
        with self._state_lock:
            if self._closing or (
                self._active_cancellation is not None
                and self._active_cancellation.event.is_set()
            ):
                return "n"
            self._permission_requests.put(request)
            invalidate = self._invalidate_input
        if invalidate is not None:
            invalidate()
        request.ready.wait()
        return request.answer

    def run_task(self, task: str, *, cancellation=None) -> bool:
        try:
            self.runtime.run(task, on_event=self.renderer, cancellation=cancellation)
            return True
        except RunCancelled:
            return False
        except KeyboardInterrupt:
            self.renderer.close()
            self._print_message("\n[bold yellow]当前任务已中断。[/]")
        except (AgentRuntimeError, ModelError, OSError, ValueError):
            self.renderer.close()
        except Exception as exc:
            # Keep the queue consumer alive even if an adapter or UI callback
            # fails outside the runtime's normal error handling.
            self.renderer.close()
            self._print_message(Text(f"任务异常：{type(exc).__name__}: {exc}", style="red"))
        return False

    def _handle_command(self, command: str) -> bool:
        normalized = command.strip().lower()
        if normalized in {"/exit", "/quit"}:
            with self._state_lock:
                busy = self._pending_tasks > 0
                self._exit_requested = self._ui_active and busy
            if self._ui_active and busy:
                self._print_message(
                    "[bright_black]当前任务完成后退出。[/]"
                )
                return False
            return True
        if normalized == "/plan":
            if self.plan_mode:
                mode = self._permission_mode_before_plan or PermissionMode.ASK
                self.plan_mode = False
                self._permission_mode_before_plan = None
                self.runtime.set_permission_mode(mode)
                self.runtime.set_plan_mode(False)
                self._print_message(
                    f"[bright_black]计划模式已关闭，恢复为"
                    f"{self._permission_mode_label(mode)}。[/]"
                )
            else:
                self._permission_mode_before_plan = self._current_permission_mode()
                self.plan_mode = True
                self.runtime.set_plan_mode(True)
                self._print_message(
                    "[bright_black]计划模式已开启（只读）。[/]"
                )
        elif normalized == "/permissions":
            if self._ui_active and self.permission_handler is not None:
                modes = list(PermissionMode)
                current = self._current_permission_mode()
                self._mode_selection = modes.index(current)
                self._mode_selector_open = True
                self._invalidate()
            elif self.permission_handler is not None:
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
                        self._print_message(
                            f"[bright_black]权限级别已切换为："
                            f"{self._permission_mode_label(mode)}。"
                            f"{'计划模式已关闭。' if plan_was_active else ''}[/]"
                        )
        elif normalized == "/clear":
            if self._selection_control is not None:
                self._selection_control.clear_selection()
            self._copy_notice = ""
            if self._ui_active:
                with self._state_lock:
                    self._transcript.clear()
                    self._transcript_blocks.clear()
                    self._transcript_styled.clear()
                    self._transcript_lines = 1
                    self._transcript_cursor_line = None
                if self._transcript_window is not None:
                    self._transcript_window.follow_bottom()
                self._invalidate()
            else:
                self.console.clear()
        elif normalized == "/status":
            self._show_status()
        elif normalized == "/history":
            if not self.history:
                self._print_message(
                    "[bright_black]本次会话还没有任务。[/]"
                )
            else:
                for index, task in enumerate(self.history, 1):
                    self._print_message(
                        Text.assemble(
                            (f"{index:>2}  ", "bright_black"),
                            (task, "default"),
                        )
                    )
        elif normalized == "/verbose":
            enabled = self.renderer.toggle_verbose()
            label = "开启" if enabled else "关闭"
            self._print_message(
                f"[bright_black]完整工具输出已{label}。[/]"
            )
        else:
            self._print_message(
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
        self._print_message(
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

    def _create_application(
        self, *, input=None, output=None
    ) -> Application[Any]:
        input_field = TextArea(
            prompt=FormattedText([("class:input-field.prompt", "› ")]),
            multiline=True,
            wrap_lines=True,
            completer=SlashCommandCompleter(),
            complete_while_typing=False,
            lexer=SlashCommandLexer(),
            history=self._input_history,
            style="class:input-field",
        )

        def refresh_command_completions(buffer) -> None:
            value = buffer.document.text_before_cursor
            if value.startswith("/") and not any(char.isspace() for char in value):
                buffer.complete_state = CompletionState(
                    buffer.document,
                    list(SlashCommandCompleter().get_completions(buffer.document, None)),
                )
            else:
                buffer.complete_state = None

        input_field.buffer.on_text_changed += refresh_command_completions
        input_field.buffer.on_cursor_position_changed += refresh_command_completions
        input_field.window.height = lambda: self._input_height(input_field.text)
        input_mouse_handler = input_field.window._mouse_handler

        def route_input_wheel(mouse_event: MouseEvent):
            if mouse_event.event_type is MouseEventType.SCROLL_UP:
                self._scroll_transcript(-3)
                return None
            if mouse_event.event_type is MouseEventType.SCROLL_DOWN:
                self._scroll_transcript(3)
                return None
            return input_mouse_handler(mouse_event)

        input_field.window._mouse_handler = route_input_wheel
        self._input_field = input_field

        transcript_control = TranscriptSelectionControl(
            self._transcript_fragments,
            on_start=self._start_selection,
            on_copy=self._copy_selection,
        )
        self._selection_control = transcript_control
        transcript = _TranscriptWindow(
            content=transcript_control,
            height=Dimension(weight=1),
            wrap_lines=True,
            always_hide_cursor=True,
            style="class:transcript",
            on_scroll=self._scroll_transcript,
            get_target_line=self._transcript_target_line,
        )
        self._transcript_window = transcript
        status = Window(
            height=1,
            content=FormattedTextControl(self._status_fragments),
            style="class:session-status",
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
        normal_bottom = ConditionalContainer(
            content=HSplit(
                [
                    Window(
                        height=1,
                        content=FormattedTextControl(
                            self._activity_fragments
                        ),
                    ),
                    bordered_input,
                    command_panel,
                    status,
                ]
            ),
            filter=Condition(lambda: not self._overlay_visible()),
        )
        permission_overlay = ConditionalContainer(
            content=Window(
                content=FormattedTextControl(self._permission_fragments),
                height=10,
            ),
            filter=Condition(lambda: self._active_permission is not None),
        )
        mode_overlay = ConditionalContainer(
            content=Window(
                content=FormattedTextControl(self._mode_fragments),
                height=lambda: len(PermissionMode) + 4,
                style="class:overlay",
            ),
            filter=Condition(lambda: self._mode_selector_open),
        )
        container = HSplit(
            [
                transcript,
                permission_overlay,
                mode_overlay,
                normal_bottom,
            ]
        )
        application: Application[Any] = Application(
            layout=Layout(container, focused_element=input_field),
            key_bindings=self._key_bindings(input_field),
            style=INPUT_STYLE,
            full_screen=True,
            mouse_support=True,
            min_redraw_interval=1 / 30,
            input=input,
            output=output,
        )
        return application

    def _key_bindings(self, input_field: TextArea) -> KeyBindings:
        bindings = KeyBindings()
        permission_visible = Condition(
            lambda: self._active_permission is not None
        )
        mode_visible = Condition(lambda: self._mode_selector_open)
        completing = Condition(
            lambda: not self._overlay_visible() and self._has_completions(input_field)
        )

        @bindings.add("up", filter=completing)
        def previous_completion(event) -> None:
            input_field.buffer.complete_previous()

        @bindings.add("down", filter=completing)
        def next_completion(event) -> None:
            input_field.buffer.complete_next()

        @bindings.add("escape", filter=completing)
        def dismiss_completions(event) -> None:
            input_field.buffer.cancel_completion()

        @bindings.add("tab")
        def complete_command(event) -> None:
            if self._overlay_visible():
                self._move_overlay(1)
                event.app.invalidate()
                return
            buffer = input_field.buffer
            if buffer.complete_state is None:
                buffer.start_completion(select_first=True)
            state = buffer.complete_state
            if state is not None and state.completions:
                index = state.complete_index or 0
                buffer.apply_completion(state.completions[index])

        @bindings.add("s-tab")
        def switch_mode(event) -> None:
            if self._overlay_visible():
                self._move_overlay(-1)
            else:
                self._cycle_mode()
            event.app.invalidate()

        @bindings.add("up", filter=permission_visible | mode_visible)
        @bindings.add("left", filter=permission_visible | mode_visible)
        def select_previous(event) -> None:
            self._move_overlay(-1)
            event.app.invalidate()

        @bindings.add("down", filter=permission_visible | mode_visible)
        @bindings.add("right", filter=permission_visible | mode_visible)
        def select_next(event) -> None:
            self._move_overlay(1)
            event.app.invalidate()

        @bindings.add("enter")
        def submit(event) -> None:
            if self._active_permission is not None:
                value = PERMISSION_OPTIONS[self._permission_selection][0]
                self._resolve_permission(value)
            elif self._mode_selector_open:
                self._confirm_mode()
            else:
                state = input_field.buffer.complete_state
                if state is not None and state.current_completion is not None:
                    input_field.buffer.apply_completion(state.current_completion)
                self._submit_input(input_field, event.app)
            event.app.invalidate()

        @bindings.add("escape", filter=permission_visible | mode_visible)
        def cancel_overlay(event) -> None:
            if self._active_permission is not None:
                self._resolve_permission("n")
            else:
                self._mode_selector_open = False
            event.app.layout.focus(input_field)
            event.app.invalidate()

        for key, _label, _description in PERMISSION_OPTIONS:
            bindings.add(key, filter=permission_visible)(
                lambda event, value=key: self._resolve_permission(value)
            )

        for index, (value, _label, _description) in enumerate(PERMISSION_OPTIONS, 1):
            bindings.add(str(index), filter=permission_visible)(
                lambda event, answer=value: self._resolve_permission(answer)
            )

        @bindings.add("escape", "enter")
        @bindings.add("c-j")
        def insert_newline(event) -> None:
            if not self._overlay_visible():
                input_field.buffer.insert_text("\n")

        @bindings.add("c-d")
        def exit_on_empty_input(event) -> None:
            if self._overlay_visible():
                return
            if input_field.text:
                input_field.buffer.delete()
            else:
                self._request_exit(event.app)

        @bindings.add("c-c")
        def cancel_current_input(event) -> None:
            if self._selection_control and self._selection_control.selected_text:
                self._copy_selection(self._selection_control.selected_text)
            elif self._cancel_current_task():
                pass
            elif self._active_permission is not None:
                self._resolve_permission("n")
            elif self._mode_selector_open:
                self._mode_selector_open = False
            elif input_field.text:
                input_field.buffer.reset()
            else:
                self._print_message(
                    "[bright_black]已取消输入 · /exit 退出[/]"
                )
            event.app.invalidate()

        @bindings.add("escape", filter=Condition(
            lambda: not self._overlay_visible() and not self._has_completions(input_field)
            and self._selection_control is not None
            and self._selection_control.snapshot is not None
        ))
        def clear_selection(event) -> None:
            self._selection_control.clear_selection()
            self._copy_notice = ""
            event.app.invalidate()

        @bindings.add("pageup")
        def scroll_transcript_up(event) -> None:
            page = max(1, event.app.output.get_size().rows // 2)
            self._scroll_transcript(-page)
            event.app.invalidate()

        @bindings.add(Keys.ScrollUp, eager=True)
        def scroll_transcript_wheel_up(event) -> None:
            self._scroll_transcript(-3)
            event.app.invalidate()

        @bindings.add("pagedown")
        def scroll_transcript_down(event) -> None:
            page = max(1, event.app.output.get_size().rows // 2)
            self._scroll_transcript(page)
            event.app.invalidate()

        @bindings.add(Keys.ScrollDown, eager=True)
        def scroll_transcript_wheel_down(event) -> None:
            self._scroll_transcript(3)
            event.app.invalidate()

        @bindings.add("c-end")
        def scroll_transcript_to_bottom(event) -> None:
            if self._selection_control is not None:
                self._selection_control.clear_selection()
            self._copy_notice = ""
            self._transcript_cursor_line = None
            if self._transcript_window is not None:
                self._transcript_window.follow_bottom()
            event.app.invalidate()

        return bindings

    def _start_selection(self) -> None:
        self._copy_notice = ""
        if self._transcript_window is not None:
            self._transcript_window.scroll_rows(0)

    def _copy_selection(self, text: str) -> None:
        try:
            application = self._application or get_app()
            application.clipboard.set_text(text)
            self._copy_notice = copy_to_clipboard(text, application.output)
        except OSError:
            self._copy_notice = "复制失败 · Ctrl+C 重试"
        self._invalidate()

    def _cancel_current_task(self) -> bool:
        with self._state_lock:
            cancellation = self._active_cancellation
            if cancellation is None:
                return False
            self._activity_message = "Cancelling"
            self._activity_started_at = time.monotonic()
        cancellation.cancel()
        with self._state_lock:
            if self._active_cancellation is cancellation:
                self._deny_pending_permissions()
        self._mode_selector_open = False
        self._invalidate()
        return True

    def _scroll_transcript(self, offset: int) -> None:
        window = self._transcript_window
        if window is not None and window.render_info is not None:
            window.scroll_rows(offset)
            self._invalidate()
            return
        with self._state_lock:
            last_line = max(0, self._transcript_lines - 1)
            current = (
                last_line
                if self._transcript_cursor_line is None
                else self._transcript_cursor_line
            )
            target = max(0, current + offset)
            self._transcript_cursor_line = (
                None if target >= last_line else target
            )
            invalidate = self._invalidate_input
        if invalidate is not None:
            invalidate()

    async def _refresh_ui(self) -> None:
        while True:
            await asyncio.sleep(0.1)
            changed = self._activate_permission_request()
            with self._state_lock:
                active = self._activity_message is not None
            if changed or active:
                self._invalidate()

    def _activate_permission_request(self) -> bool:
        if self._active_permission is not None:
            return False
        try:
            request = self._permission_requests.get_nowait()
        except queue.Empty:
            return False
        self._active_permission = request
        self._permission_selection = len(PERMISSION_OPTIONS) - 1
        if self._selection_control is not None:
            self._selection_control.clear_selection()
        self._copy_notice = ""
        if self.permission_handler is not None and self._ui_active:
            self._print_message(
                self.permission_handler.request_panel(request.tool_name, request.args)
            )
            self._transcript_cursor_line = None
            if self._transcript_window is not None:
                self._transcript_window.follow_bottom()
        return True

    def _resolve_permission(self, answer: str) -> None:
        request = self._active_permission
        if request is None:
            return
        request.answer = answer
        self._active_permission = None
        request.ready.set()
        self._permission_requests.task_done()
        self._activate_permission_request()
        if self._application is not None and self._input_field is not None:
            self._application.layout.focus(self._input_field)

    def _move_overlay(self, offset: int) -> None:
        if self._active_permission is not None:
            self._permission_selection = (
                self._permission_selection + offset
            ) % len(PERMISSION_OPTIONS)
        elif self._mode_selector_open:
            self._mode_selection = (
                self._mode_selection + offset
            ) % len(PermissionMode)

    def _confirm_mode(self) -> None:
        mode = list(PermissionMode)[self._mode_selection]
        self.runtime.set_permission_mode(mode)
        if self.permission_handler is not None:
            self.permission_handler.set_mode(mode)
        if self.plan_mode:
            self.plan_mode = False
            self._permission_mode_before_plan = None
            self.runtime.set_plan_mode(False)
        self._mode_selector_open = False
        self._print_message(
            f"[bright_black]权限级别已切换为："
            f"{self._permission_mode_label(mode)}。[/]"
        )

    def _submit_input(
        self, input_field: TextArea, application: Application[Any]
    ) -> None:
        value = input_field.text
        task = value.strip()
        if not task:
            return
        if self._selection_control is not None:
            self._selection_control.clear_selection()
        self._copy_notice = ""
        input_field.buffer.append_to_history()
        input_field.buffer.reset()
        self._transcript_cursor_line = None
        if self._transcript_window is not None:
            self._transcript_window.follow_bottom()
        if task.startswith("/"):
            self._print_message(
                Text.assemble(
                    ("› ", "bold bright_cyan"),
                    (task, "default"),
                )
            )
            if self._handle_command(task):
                application.exit()
            return
        self.history.append(task)
        self._enqueue_task(task)

    def _request_exit(self, application: Application[Any]) -> None:
        with self._state_lock:
            busy = self._pending_tasks > 0
            self._exit_requested = busy
        if busy:
            self._print_message(
                "[bright_black]当前任务完成后退出。[/]"
            )
        else:
            application.exit()

    def _overlay_visible(self) -> bool:
        return (
            self._active_permission is not None
            or self._mode_selector_open
        )

    def _permission_fragments(self) -> FormattedText:
        request = self._active_permission
        if request is None:
            return FormattedText([])
        width = max(1, get_app().output.get_size().columns)
        label = PERMISSION_LABELS.get(request.tool_name, request.tool_name)
        target = next((str(request.args[key]) for key in ("command", "path", "url", "query")
                       if key in request.args), request.tool_name)
        target = " ".join(target.split())
        fragments: list[tuple[str, str]] = []

        def line(style: str, value: str) -> None:
            fragments.append((style, self._truncate_cells(value, width) + "\n"))

        line("class:approval.border", "─" * width)
        line("class:approval.title", f"  是否允许{label}？")
        line("class:approval.detail", f"  {target}")
        line("", "")
        choices = permission_choices(request.tool_name)
        for index, (_value, option, _description) in enumerate(choices):
            active = index == self._permission_selection
            prefix = "  ❯ " if active else "    "
            line("class:approval.selected" if active else "class:approval.option",
                 f"{prefix}{index + 1}. {option}")
        line("class:approval.detail", f"    {choices[self._permission_selection][2]}")
        line("class:approval.help", "  ↑↓ 选择 · Enter 确认 · Esc 拒绝 · 1/2/3 快选")
        fragments.append(("class:approval.help", self._truncate_cells(
            "  完整操作见上方 · PageUp/PageDown 翻阅", width)))
        return FormattedText(fragments)

    def _mode_fragments(self) -> FormattedText:
        descriptions = {
            PermissionMode.ASK: "敏感操作前询问",
            PermissionMode.APPROVE: "自动批准工作区内文件修改",
            PermissionMode.FULL: "无需确认",
        }
        fragments: list[tuple[str, str]] = [
            ("class:overlay.title", "  权限设置\n\n")
        ]
        for index, mode in enumerate(PermissionMode):
            active = index == self._mode_selection
            label = self._permission_mode_label(mode)
            fragments.extend(
                [
                    ("class:overlay.cursor", "  ❯ " if active else "    "),
                    (
                        "class:overlay.selected" if active else "class:overlay.option",
                        f"{label:<18}",
                    ),
                    ("class:overlay.detail", f"  {descriptions[mode]}\n"),
                ]
            )
        fragments.append(
            ("class:overlay.help", "  ↑↓ 选择 · Enter 确认 · Esc 取消")
        )
        return FormattedText(fragments)

    def _transcript_fragments(self) -> FormattedText:
        with self._state_lock:
            width = get_app().output.get_size().columns
            if self._transcript_blocks and width != self._transcript_width:
                self._reflow_transcript(width)
            fragments = tuple(self._transcript_styled)
        return FormattedText(fragments)

    def _append_renderables(self, objects, options=None) -> None:
        block = TranscriptBlock(tuple(objects), options or {})
        with self._state_lock:
            self._transcript_blocks.append(block)
            text = block.render(self._transcript_width, self.console.color_system)
            styled = list(to_formatted_text(ANSI(text)))
            self._transcript.append(text)
            self._transcript_styled.extend(styled)
            self._transcript_lines += max(0, self._fragment_line_count(styled) - 1)
        self._invalidate()

    def _reflow_transcript(self, width: int) -> None:
        """Called under the state lock: publish one consistent resized snapshot."""
        window = self._transcript_window
        anchor = window._view_anchor if window is not None else None
        old_counts = [text.count("\n") for text in self._transcript]
        chunks = [block.render(width, self.console.color_system)
                  for block in self._transcript_blocks]
        # Keep the reader in the same source block when its rendered height changes.
        if anchor is not None:
            old_top = new_top = 0
            for old_count, chunk in zip(old_counts, chunks):
                new_count = chunk.count("\n")
                if anchor[0] < old_top + old_count:
                    fraction = (anchor[0] - old_top) / max(1, old_count)
                    window._view_anchor = (new_top + int(fraction * new_count), 0)
                    break
                old_top += old_count
                new_top += new_count
        self._transcript = chunks
        self._transcript_styled = list(to_formatted_text(ANSI("".join(chunks))))
        self._transcript_lines = self._fragment_line_count(self._transcript_styled)
        self._transcript_width = width

    def _transcript_target_line(self) -> int:
        with self._state_lock:
            last_line = max(0, self._transcript_lines - 1)
            return (
                last_line
                if self._transcript_cursor_line is None
                else min(
                    self._transcript_cursor_line,
                    last_line,
                )
            )

    def _write_transcript(self, text: str) -> None:
        if self._ui_active and self._transcript_blocks:
            self._append_renderables((Text.from_ansi(text),), {"end": ""})
            return
        styled = list(to_formatted_text(ANSI(text)))
        added_lines = self._fragment_line_count(styled)
        with self._state_lock:
            active = self._ui_active
            if active:
                self._transcript.append(text)
                self._transcript_styled.extend(styled)
                self._transcript_lines += max(0, added_lines - 1)
                invalidate = self._invalidate_input
            else:
                invalidate = None
        if not active:
            file = self.console.file
            file.write(text)
            file.flush()
            return
        if invalidate is not None:
            invalidate()

    @staticmethod
    def _fragment_line_count(
        fragments: list[tuple[str, str]],
    ) -> int:
        return max(1, sum(1 for _line in split_lines(fragments)))

    def _print_message(self, *objects: Any) -> None:
        if not self._ui_active:
            self.console.print(*objects)
            return
        self._append_renderables(objects)

    def _invalidate(self) -> None:
        with self._state_lock:
            invalidate = self._invalidate_input
        if invalidate is not None:
            invalidate()

    def _update_activity(
        self, message: str | None, started_at: float | None
    ) -> None:
        with self._state_lock:
            self._activity_message = message
            self._activity_started_at = started_at
            invalidate = self._invalidate_input
        if invalidate is not None:
            invalidate()

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
        # Keep at least half the screen available to the transcript, matching
        # the fixed-bottom layout used by Claude Code.
        available_lines = max(1, rows // 2 - 3)
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

    def _activity_fragments(self) -> FormattedText:
        width = get_app().output.get_size().columns
        if self._selection_control is not None and self._selection_control.snapshot is not None:
            message = self._copy_notice or "拖动选择文本"
            return FormattedText([("class:session-status", self._truncate_cells(
                f"  {message} · Ctrl+C 复制 · Esc 清除选区", width
            ))])
        with self._state_lock:
            message = self._activity_message
            started_at = self._activity_started_at
            browsing = self._transcript_cursor_line is not None or (
                self._transcript_window is not None
                and self._transcript_window._view_anchor is not None
            )
        if browsing:
            return FormattedText([("class:session-status", self._truncate_cells(
                "  正在查看历史 · Ctrl+End 返回最新内容", width
            ))])
        if message is None or started_at is None:
            return FormattedText([])

        frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        frame = frames[int(time.monotonic() * 10) % len(frames)]
        elapsed = TerminalRenderer._elapsed(started_at)
        label = self._truncate_cells(
            f"{frame} {message} · {elapsed}", max(1, width - 2)
        )
        return FormattedText(
            [
                ("class:session-status.activity", f"  {label}"),
            ]
        )

    @staticmethod
    def _truncate_cells(text: str, width: int) -> str:
        if get_cwidth(text) <= width:
            return text
        if width <= 1:
            return "…"[:width]
        result: list[str] = []
        used = 0
        for character in text:
            character_width = get_cwidth(character)
            if used + character_width > width - 1:
                break
            result.append(character)
            used += character_width
        return "".join(result) + "…"

    def _status_fragments(self) -> FormattedText:
        path = self._display_path(self.workspace)
        permission_mode = self._current_permission_mode()
        if self.plan_mode:
            modes = [("plan mode on", "class:session-status.mode.plan")]
        elif permission_mode is PermissionMode.FULL:
            modes = [("full access on", "class:session-status.mode.danger")]
        elif permission_mode is PermissionMode.APPROVE:
            modes = [("accept edits on", "class:session-status.mode.accept")]
        else:
            modes = []
        mode_width = sum(get_cwidth(label) for label, _style in modes)
        width = (
            get_app().output.get_size().columns
            if self._ui_active
            else self.console.width
        )
        fragments = [
            ("class:session-status", "  "),
            ("class:session-status.model", self.model_name),
            ("class:session-status.separator", "  ·  "),
            ("class:session-status.path", path),
        ]
        # Reserve the right-hand mode before fitting the model/path; never
        # let a long workspace push the permission indicator off screen.
        suffix = modes[0] if modes else None
        budget = max(0, width - (min(mode_width, width) + 1 if suffix else 0))
        fitted: list[tuple[str, str]] = []
        remaining = budget
        for style, value in fragments:
            clipped = self._truncate_cells(value, remaining)
            fitted.append((style, clipped))
            remaining -= get_cwidth(clipped)
        if suffix:
            label, style = suffix
            padding = max(0, width - min(mode_width, width) - (budget - remaining))
            fitted.append(("", " " * padding))
            fitted.append((style, self._truncate_cells(label, width)))
        return FormattedText(fitted)

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

    def _refresh_header_frame(self) -> None:
        output = StringIO()
        header_console = Console(
            file=output,
            width=self.console.width,
            force_terminal=self.console.color_system is not None,
            color_system=self.console.color_system,
        )
        header_console.print(self._header_content())
        self._header_markup = output.getvalue()
