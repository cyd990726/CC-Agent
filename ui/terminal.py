"""Persistent terminal REPL for Mini Agent."""

from collections.abc import Callable
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.shortcuts import CompleteStyle
from prompt_toolkit.styles import Style
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from agent.runtime import AgentRuntime, AgentRuntimeError
from model.llm import ModelError
from ui.renderer import TerminalRenderer


COMMANDS = (
    ("/help", "查看命令"),
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


def _command_key_bindings() -> KeyBindings:
    bindings = KeyBindings()

    @bindings.add("tab")
    def select_next(event) -> None:
        buffer = event.app.current_buffer
        if buffer.complete_state is None:
            buffer.start_completion(select_first=True)
        else:
            buffer.complete_next()

    @bindings.add("s-tab")
    def select_previous(event) -> None:
        buffer = event.app.current_buffer
        if buffer.complete_state is None:
            buffer.start_completion(select_last=True)
        else:
            buffer.complete_previous()

    return bindings


INPUT_STYLE = Style.from_dict(
    {
        "prompt": "bold ansicyan",
        "completion-menu.completion": "bg:#1f2937 #d1d5db",
        "completion-menu.completion.current": "bg:ansicyan #000000 bold",
        "completion-menu.meta.completion": "bg:#1f2937 #6b7280",
        "completion-menu.meta.completion.current": "bg:ansicyan #000000",
        "scrollbar.background": "bg:#111827",
        "scrollbar.button": "bg:#4b5563",
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
        self._prompt_session: PromptSession[str] | None = None
        self._prompt = prompt or self._read_input

    def run(self) -> int:
        self._show_header()
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
        if normalized == "/help":
            self._show_help()
        elif normalized == "/clear":
            self.console.clear()
            self._show_header()
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
                f"[yellow]未知命令：{command}。使用 /help 查看帮助。[/]"
            )
        return False

    def _show_header(self) -> None:
        workspace = self._display_path(self.workspace)
        body = Text()
        body.append("Model      ", style="bright_black")
        body.append(self.model_name, style="bold")
        body.append("\nWorkspace  ", style="bright_black")
        body.append(workspace)
        self.console.print(
            Panel(
                body,
                title="[bold bright_cyan] ✦ Mini Agent [/]",
                subtitle="[bright_black]/help for commands[/]",
                subtitle_align="right",
                border_style="bright_black",
                box=box.ROUNDED,
                padding=(1, 2),
                expand=False,
            )
        )
        self.console.print(
            "[bright_black]Describe a task, ask a question, or review some code.[/]\n"
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

    def _show_help(self) -> None:
        table = Table(box=None, show_header=False, padding=(0, 2))
        table.add_column(style="bold bright_cyan", no_wrap=True)
        table.add_column(style="bright_black")
        for command, description in COMMANDS:
            table.add_row(command, description)
        self.console.print(
            Panel.fit(
                table,
                title="[bold]Commands[/]",
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
        if self._prompt_session is None:
            self._prompt_session = PromptSession(
                completer=SlashCommandCompleter(),
                complete_while_typing=True,
                complete_style=CompleteStyle.COLUMN,
                history=InMemoryHistory(),
                key_bindings=_command_key_bindings(),
                reserve_space_for_menu=len(COMMANDS),
                style=INPUT_STYLE,
            )
        return self._prompt_session.prompt([("class:prompt", "❯ ")])
