"""Render agent events as a polished, compact terminal transcript."""

import json
import re
import time
from collections.abc import Mapping
from typing import Any

from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.status import Status
from rich.text import Text

from agent.events import AgentEvent, EventType


ACCENT = "bright_cyan"
MUTED = "bright_black"


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
    """Translate runtime events into a readable terminal conversation."""

    TOOL_LABELS = {
        "read_file": "Read",
        "edit_file": "Edit",
        "write_file": "Write",
        "find_files": "Find",
        "list_files": "List",
        "search": "Search",
        "shell": "Bash",
    }

    def __init__(
        self,
        console: Console,
        *,
        verbose: bool = False,
        output_limit: int = 1200,
        output_lines: int = 10,
        live_factory: type[Live] = Live,
    ) -> None:
        self.console = console
        self.verbose = verbose
        self.output_limit = output_limit
        self.output_lines = output_lines
        self.live_factory = live_factory
        self._status: Status | None = None
        self._live_answer: Live | None = None
        self._answer_stream = _JsonStringFieldStreamer("final_answer")
        self._answer_text = ""
        self._visible_answer_text = ""
        self._streamed_answer = False
        self._has_answer_stream = False
        self._run_started_at: float | None = None
        self._tool_started_at: float | None = None

    def __call__(self, event: AgentEvent) -> None:
        if event.type is EventType.RUN_STARTED:
            self._run_started_at = time.monotonic()
            return
        if event.type is EventType.MODEL_STARTED:
            self._answer_stream = _JsonStringFieldStreamer("final_answer")
            self._answer_text = ""
            self._visible_answer_text = ""
            self._streamed_answer = False
            self._has_answer_stream = False
            step = event.data.get("step", 1)
            self._start_status(f"Thinking · step {step}")
            return
        if event.type is EventType.MODEL_DELTA:
            self._render_answer_delta(str(event.data.get("delta", "")))
            return
        if event.type is EventType.MODEL_COMPLETED:
            self._stop_status()
            response = event.data.get("response", {})
            if isinstance(response, Mapping) and not self._has_answer_stream:
                thought = response.get("thought")
                if isinstance(thought, str) and thought.strip():
                    self.console.print(Text(f"  {thought.strip()}", style=MUTED))
            return
        if event.type is EventType.TOOL_REQUESTED:
            self._render_tool_request(event.data)
            return
        if event.type is EventType.TOOL_STARTED:
            self._tool_started_at = time.monotonic()
            return
        if event.type is EventType.TOOL_COMPLETED:
            self._render_tool_result(event.data)
            return
        if event.type is EventType.RUN_COMPLETED:
            self._render_completion(event.data)
            return
        if event.type is EventType.RUN_FAILED:
            self.close()
            error = str(event.data.get("error", "运行失败"))
            self.console.print(Text.assemble(("✕ ", "bold red"), (error, "red")))

    def toggle_verbose(self) -> bool:
        self.verbose = not self.verbose
        return self.verbose

    def close(self) -> None:
        self._stop_status()
        self._stop_live_answer()

    def _render_answer_delta(self, delta: str) -> None:
        text = self._answer_stream.feed(delta)
        if not text:
            return
        self._answer_text += text
        self._has_answer_stream = True
        if self._status is not None:
            self._stop_status()
        self._update_live_answer()

    def _render_completion(self, data: Mapping[str, Any]) -> None:
        self._stop_status()
        self._stop_live_answer()
        answer = str(data.get("answer", ""))
        self.console.print()
        self.console.print(f"[bold {ACCENT}]✦ Mini Agent[/]")
        self.console.print(Markdown(answer))
        self._streamed_answer = self._has_answer_stream

        elapsed = self._elapsed(self._run_started_at)
        steps = data.get("steps", 0)
        meta = f"Done in {elapsed} · {steps} step{'s' if steps != 1 else ''}"
        self.console.print(Text(f"  ✓ {meta}", style=MUTED))

    def _start_status(self, message: str) -> None:
        self._stop_status()
        self._status = self.console.status(
            Text(message, style=MUTED), spinner="dots", spinner_style=ACCENT
        )
        self._status.start()

    def _stop_status(self) -> None:
        if self._status is not None:
            self._status.stop()
            self._status = None

    def _update_live_answer(self) -> None:
        visible_text = self._stable_streaming_text(self._answer_text)
        if not visible_text:
            return
        if visible_text == self._visible_answer_text:
            return
        self._visible_answer_text = visible_text
        preview = Group(Text("✦ Mini Agent", style=f"bold {ACCENT}"), Markdown(visible_text))
        if self._live_answer is None:
            if not self.console.is_terminal:
                return
            self._live_answer = self.live_factory(
                preview,
                console=self.console,
                refresh_per_second=12,
                transient=True,
                vertical_overflow="visible",
            )
            self._live_answer.start()
            self._streamed_answer = True
            return
        self._live_answer.update(preview, refresh=True)

    def _stop_live_answer(self) -> None:
        if self._live_answer is not None:
            self._live_answer.stop()
            self._live_answer = None

    @staticmethod
    def _stable_streaming_text(text: str) -> str:
        last_newline = text.rfind("\n")
        if last_newline < 0:
            return ""
        return text[: last_newline + 1]

    def _render_tool_request(self, data: Mapping[str, Any]) -> None:
        tool = str(data.get("tool", "unknown"))
        args = data.get("args", {})
        args = args if isinstance(args, Mapping) else {}
        label = self.TOOL_LABELS.get(tool, tool)
        title, details = self._tool_description(tool, args)

        line = Text()
        line.append("● ", style=ACCENT)
        line.append(label, style="bold")
        if title:
            line.append(f" {title}")
        self.console.print(line)
        for detail in details:
            self.console.print(Text(f"  └ {detail}", style=MUTED))

    def _tool_description(
        self, tool: str, args: Mapping[str, Any]
    ) -> tuple[str, list[str]]:
        if tool in {"read_file", "edit_file", "write_file"}:
            path = str(args.get("path", ""))
            details: list[str] = []
            content = args.get("content")
            if tool == "write_file" and isinstance(content, str):
                details.append(f"{len(content):,} characters")
            if tool == "read_file" and ("offset" in args or "limit" in args):
                offset = args.get("offset", 1)
                limit = args.get("limit")
                if isinstance(limit, int):
                    details.append(f"lines {offset}-{offset + limit - 1}")
                else:
                    details.append(f"from line {offset}")
            if tool == "edit_file":
                old_text = str(args.get("old_text", ""))
                new_text = str(args.get("new_text", ""))
                details.append(f"{len(old_text):,} → {len(new_text):,} characters")
            return path, details
        if tool == "find_files":
            pattern = str(args.get("pattern", ""))
            return pattern, [f"in {args.get('path', '.')}"]
        if tool == "list_files":
            path = str(args.get("path", "."))
            return path, [f"depth {args.get('depth', 1)}"]
        if tool == "search":
            query = self._one_line(str(args.get("query", "")), 72)
            scope = str(args.get("path", "."))
            glob = args.get("glob")
            detail = f"in {scope}"
            if glob:
                detail += f" · {glob}"
            return json.dumps(query, ensure_ascii=False), [detail]
        if tool == "shell":
            command = self._one_line(str(args.get("command", "")), 100)
            return "", [f"$ {command}"]
        details = [
            f"{name}: {self._format_value(value)}" for name, value in args.items()
        ]
        return "", details

    def _render_tool_result(self, data: Mapping[str, Any]) -> None:
        result = data.get("result", {})
        if not isinstance(result, Mapping):
            return
        tool = str(result.get("tool", ""))
        success = bool(result.get("success"))
        output = str(result.get("output", ""))
        elapsed = self._elapsed(self._tool_started_at)
        summary, details = self._tool_result_summary(tool, output, success)
        marker = "└" if success else "└ ✕"
        style = "green" if success else "red"
        if success and tool == "shell" and not summary.startswith("Exited with code 0"):
            marker = "└ !"
            style = "yellow"
        suffix = f" · {elapsed}" if elapsed != "0ms" else ""
        self.console.print(Text(f"  {marker} {summary}{suffix}", style=style))
        for detail in details:
            detail_style = MUTED if success else "red"
            self.console.print(Text(f"    │ {detail}", style=detail_style))

    def _tool_result_summary(
        self, tool: str, output: str, success: bool
    ) -> tuple[str, list[str]]:
        if not success:
            return "Failed", self._preview_lines(output)
        if tool == "read_file":
            range_match = re.match(r"^\[lines (\d+)-(\d+) of (\d+)\]", output)
            if range_match:
                start, end, total = range_match.groups()
                details = self._preview_lines(output) if self.verbose else []
                return f"Read lines {start}-{end} of {total}", details
            if output == "[file is empty: 0 lines]":
                return "File is empty", []
            line_count = len(output.splitlines())
            size = len(output.encode("utf-8"))
            details = self._preview_lines(output) if self.verbose else []
            return f"Read {line_count:,} lines · {self._format_bytes(size)}", details
        if tool == "write_file":
            return output or "File written", []
        if tool == "edit_file":
            lines = output.splitlines()
            summary = lines[0].removeprefix("updated ") if lines else "file"
            return f"Updated {summary}", self._preview_lines("\n".join(lines[1:]))
        if tool == "find_files":
            if output == "no files":
                return "No files", []
            lines = output.splitlines()
            return f"Found {len(lines):,} files", self._preview_lines(output)
        if tool == "list_files":
            if output == "empty directory":
                return "Empty directory", []
            lines = output.splitlines()
            return f"Listed {len(lines):,} entries", self._preview_lines(output)
        if tool == "search":
            if output == "no matches":
                return "No matches", []
            lines = output.splitlines()
            return f"Found {len(lines):,} matches", self._preview_lines(output)
        if tool == "shell":
            return self._shell_summary(output)
        return "Done", self._preview_lines(output) if output else []

    def _shell_summary(self, output: str) -> tuple[str, list[str]]:
        try:
            parsed = json.loads(output)
        except (json.JSONDecodeError, TypeError):
            return "Completed", self._preview_lines(output)
        if not isinstance(parsed, dict):
            return "Completed", self._preview_lines(output)
        exit_code = parsed.get("exit_code")
        stdout = str(parsed.get("stdout", "")).strip()
        stderr = str(parsed.get("stderr", "")).strip()
        details = "\n".join(part for part in (stdout, stderr) if part)
        return f"Exited with code {exit_code}", self._preview_lines(details)

    def _preview_lines(self, value: str) -> list[str]:
        if not value:
            return []
        original = value.splitlines()
        if self.verbose:
            return original
        character_clipped = len(value) > self.output_limit
        clipped = self._truncate(value).splitlines()
        visible = clipped[: self.output_lines]
        omitted_lines = max(0, len(original) - len(visible))
        if omitted_lines:
            noun = "line" if omitted_lines == 1 else "lines"
            visible.append(f"… {omitted_lines:,} more {noun} · /verbose to expand")
        elif character_clipped:
            visible.append("… output truncated · /verbose to expand")
        return visible

    def _truncate(self, value: str) -> str:
        if self.verbose or len(value) <= self.output_limit:
            return value
        return value[: self.output_limit].rstrip()

    @staticmethod
    def _one_line(value: str, limit: int) -> str:
        value = " ".join(value.split())
        return value if len(value) <= limit else value[: limit - 1] + "…"

    @staticmethod
    def _format_bytes(size: int) -> str:
        if size < 1024:
            return f"{size} B"
        if size < 1024 * 1024:
            return f"{size / 1024:.1f} KB"
        return f"{size / (1024 * 1024):.1f} MB"

    @staticmethod
    def _elapsed(started_at: float | None) -> str:
        if started_at is None:
            return "0ms"
        seconds = max(0.0, time.monotonic() - started_at)
        if seconds < 1:
            return f"{seconds * 1000:.0f}ms"
        if seconds < 60:
            return f"{seconds:.1f}s"
        return f"{int(seconds // 60)}m {int(seconds % 60)}s"

    @staticmethod
    def _format_value(value: Any) -> str:
        if isinstance(value, str):
            return value
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            return repr(value)
