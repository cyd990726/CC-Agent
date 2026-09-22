"""Render agent events as a compact terminal transcript."""

import json
import re
from collections.abc import Mapping
from typing import Any

from rich.console import Console
from rich.markdown import Markdown
from rich.status import Status
from rich.text import Text

from agent.events import AgentEvent, EventType


class _JsonStringFieldStreamer:
    """Extract and incrementally decode one JSON string field."""

    def __init__(self, field: str) -> None:
        self._pattern = re.compile(rf'"{re.escape(field)}"\s*:\s*"')
        self._buffer = ""
        self._started = False
        self._escaped = False
        self._unicode_digits: str | None = None
        self.finished = False

    def feed(self, chunk: str) -> str:
        if self.finished:
            return ""
        if not self._started:
            self._buffer += chunk
            match = self._pattern.search(self._buffer)
            if match is None:
                return ""
            chunk = self._buffer[match.end() :]
            self._buffer = ""
            self._started = True

        output: list[str] = []
        for character in chunk:
            if self._unicode_digits is not None:
                self._unicode_digits += character
                if len(self._unicode_digits) == 4:
                    codepoint = int(self._unicode_digits, 16)
                    output.append(
                        chr(codepoint)
                        if not 0xD800 <= codepoint <= 0xDFFF
                        else "�"
                    )
                    self._unicode_digits = None
                    self._escaped = False
                continue
            if self._escaped:
                if character == "u":
                    self._unicode_digits = ""
                    continue
                output.append(
                    {
                        '"': '"',
                        "\\": "\\",
                        "/": "/",
                        "b": "\b",
                        "f": "\f",
                        "n": "\n",
                        "r": "\r",
                        "t": "\t",
                    }.get(character, character)
                )
                self._escaped = False
                continue
            if character == "\\":
                self._escaped = True
            elif character == '"':
                self.finished = True
                break
            else:
                output.append(character)
        return "".join(output)


class TerminalRenderer:
    """Translate runtime events into human-readable terminal output."""

    def __init__(
        self,
        console: Console,
        *,
        verbose: bool = False,
        output_limit: int = 1200,
    ) -> None:
        self.console = console
        self.verbose = verbose
        self.output_limit = output_limit
        self._status: Status | None = None
        self._answer_stream = _JsonStringFieldStreamer("final_answer")
        self._streamed_answer = False

    def __call__(self, event: AgentEvent) -> None:
        if event.type is EventType.RUN_STARTED:
            return
        if event.type is EventType.MODEL_STARTED:
            self._answer_stream = _JsonStringFieldStreamer("final_answer")
            self._streamed_answer = False
            self._start_status("Agent 正在思考...")
            return
        if event.type is EventType.MODEL_DELTA:
            text = self._answer_stream.feed(str(event.data.get("delta", "")))
            if text:
                if not self._streamed_answer:
                    self._stop_status()
                    self.console.print("\n[bold green]●[/] ", end="")
                    self._streamed_answer = True
                self.console.print(
                    text,
                    end="",
                    markup=False,
                    highlight=False,
                    soft_wrap=True,
                )
            return
        if event.type is EventType.MODEL_COMPLETED:
            self._stop_status()
            response = event.data.get("response", {})
            if isinstance(response, Mapping) and not self._streamed_answer:
                thought = response.get("thought")
                if isinstance(thought, str) and thought.strip():
                    self.console.print(Text(f"● {thought.strip()}", style="dim"))
            return
        if event.type is EventType.TOOL_REQUESTED:
            self._render_tool_request(event.data)
            return
        if event.type is EventType.TOOL_COMPLETED:
            self._render_tool_result(event.data)
            return
        if event.type is EventType.RUN_COMPLETED:
            self._stop_status()
            answer = str(event.data.get("answer", ""))
            if self._streamed_answer:
                self.console.print("\n[bold green]✓ 完成[/]")
                return
            self.console.print()
            self.console.print("[bold green]✓ 完成[/]")
            self.console.print(Markdown(answer))
            return
        if event.type is EventType.RUN_FAILED:
            self._stop_status()
            self.console.print(f"[bold red]✗ {event.data.get('error', '运行失败')}[/]")

    def toggle_verbose(self) -> bool:
        self.verbose = not self.verbose
        return self.verbose

    def close(self) -> None:
        self._stop_status()

    def _start_status(self, message: str) -> None:
        self._stop_status()
        self._status = self.console.status(message, spinner="dots")
        self._status.start()

    def _stop_status(self) -> None:
        if self._status is not None:
            self._status.stop()
            self._status = None

    def _render_tool_request(self, data: Mapping[str, Any]) -> None:
        tool = str(data.get("tool", "unknown"))
        args = data.get("args", {})
        self.console.print(f"\n[bold cyan]◆ {tool}[/]")
        if not isinstance(args, Mapping):
            return
        for name, value in args.items():
            if name == "content" and isinstance(value, str) and not self.verbose:
                rendered = f"<{len(value)} characters>"
            else:
                rendered = self._truncate(self._format_value(value))
            self.console.print(Text(f"  {name}: {rendered}", style="dim"))

    def _render_tool_result(self, data: Mapping[str, Any]) -> None:
        result = data.get("result", {})
        if not isinstance(result, Mapping):
            return
        success = bool(result.get("success"))
        output = str(result.get("output", ""))
        style = "green" if success else "red"
        marker = "✓" if success else "✗"
        summary = self._summarize_output(str(result.get("tool", "")), output)
        self.console.print(Text(f"  {marker} {summary}", style=style))

    def _summarize_output(self, tool: str, output: str) -> str:
        if tool == "shell":
            try:
                parsed = json.loads(output)
            except (json.JSONDecodeError, TypeError):
                pass
            else:
                if isinstance(parsed, dict):
                    exit_code = parsed.get("exit_code")
                    stdout = str(parsed.get("stdout", "")).strip()
                    stderr = str(parsed.get("stderr", "")).strip()
                    details = "\n".join(part for part in (stdout, stderr) if part)
                    prefix = f"exit code: {exit_code}"
                    if details:
                        return self._truncate(f"{prefix}\n{details}")
                    return prefix
        return self._truncate(output or "完成")

    def _truncate(self, value: str) -> str:
        if self.verbose or len(value) <= self.output_limit:
            return value
        omitted = len(value) - self.output_limit
        return f"{value[:self.output_limit]}\n… 已省略 {omitted} 个字符（/verbose 查看完整输出）"

    @staticmethod
    def _format_value(value: Any) -> str:
        if isinstance(value, str):
            return value
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            return repr(value)
