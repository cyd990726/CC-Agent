"""Persistent terminal REPL for Mini Agent."""

from collections.abc import Callable
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

from agent.runtime import AgentRuntime, AgentRuntimeError
from model.llm import ModelError
from ui.renderer import TerminalRenderer


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
        self._prompt = prompt or (lambda message: Prompt.ask(message))

    def run(self) -> int:
        self._show_header()
        while True:
            try:
                task = self._prompt("[bold cyan]❯[/]").strip()
            except EOFError:
                self.console.print()
                return 0
            except KeyboardInterrupt:
                self.console.print("\n[dim]使用 /exit 退出。[/]")
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
            self.console.print(
                "/help  查看帮助\n"
                "/clear 清空屏幕\n"
                "/status 查看当前配置\n"
                "/history 查看任务历史\n"
                "/verbose 切换完整工具输出\n"
                "/exit 退出程序"
            )
        elif normalized == "/clear":
            self.console.clear()
            self._show_header()
        elif normalized == "/status":
            self._show_status()
        elif normalized == "/history":
            if not self.history:
                self.console.print("[dim]本次会话还没有任务。[/]")
            else:
                for index, task in enumerate(self.history, 1):
                    self.console.print(f"{index}. {task}")
        elif normalized == "/verbose":
            enabled = self.renderer.toggle_verbose()
            label = "开启" if enabled else "关闭"
            self.console.print(f"完整工具输出已{label}。")
        else:
            self.console.print(f"[yellow]未知命令：{command}。使用 /help 查看帮助。[/]")
        return False

    def _show_header(self) -> None:
        self.console.print(
            Panel.fit(
                f"[bold]Mini Agent[/]\n"
                f"Model: {self.model_name}\n"
                f"Workspace: {self.workspace}",
                border_style="cyan",
            )
        )
        self.console.print("[dim]输入任务开始，使用 /help 查看命令。[/]\n")

    def _show_status(self) -> None:
        table = Table(show_header=False, box=None)
        table.add_row("Model", self.model_name)
        table.add_row("Workspace", str(self.workspace))
        table.add_row("Verbose", "on" if self.renderer.verbose else "off")
        table.add_row("Tasks", str(len(self.history)))
        self.console.print(table)
