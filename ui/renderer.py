"""Render agent events as a polished, compact terminal transcript."""

import json
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from rich.console import Console, ConsoleOptions, Group, RenderResult
from rich.live import Live
from rich.markdown import Markdown
from rich.status import Status
from rich.table import Table
from rich.text import Text

from agent.events import AgentEvent, EventType


ACCENT = "bright_cyan"
MUTED = "bright_black"


class _ElapsedStatus:
    """Status text that updates its elapsed time whenever Rich refreshes it."""

    def __init__(
        self,
        renderer: "TerminalRenderer",
        message: str,
        started_at: float,
    ) -> None:
        self.renderer = renderer
        self.message = message
        self.started_at = started_at

    def __rich__(self) -> Text:
        return Text(
            f"{self.message} · {self.renderer._elapsed(self.started_at)}",
            style=MUTED,
        )


class _ToolProgress:
    """A live tool line whose marker blinks and whose elapsed time advances."""

    def __init__(
        self,
        renderer: "TerminalRenderer",
        data: Mapping[str, Any],
        started_at: float,
    ) -> None:
        self.renderer = renderer
        self.data = data
        self.started_at = started_at

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        # A one-second cycle keeps the marker calm while still making it clear
        # that the tool is alive. Live is responsible for scheduling redraws.
        phase = int((time.monotonic() - self.started_at) * 2) % 2
        marker = "●" if phase == 0 else " "
        yield self.renderer._tool_request_renderable(
            self.data,
            marker=marker,
            elapsed=self.renderer._elapsed(self.started_at),
        )


@dataclass
class _ExplorationEntry:
    tool: str
    title: str
    details: list[str]
    started_at: float
    success: bool | None = None
    output: str = ""


@dataclass
class _ExplorationGroup:
    renderer: "TerminalRenderer"
    entries: list[_ExplorationEntry] = field(default_factory=list)
    committed_entries: int = 0

    @property
    def active(self) -> _ExplorationEntry | None:
        if self.entries and self.entries[-1].success is None:
            return self.entries[-1]
        return None

    def __rich__(self) -> Group:
        return self.renderer._exploration_renderable(self)


class _JsonStringFieldStreamer:
    """Extract and incrementally decode one JSON string field."""

    def __init__(self, field: str) -> None:
        self._pattern = re.compile(rf'"{re.escape(field)}"\s*:\s*"')
        self._buffer = ""
        self._started = False
        self._escaped = False
        self._unicode_digits: str | None = None
        self._high_surrogate: int | None = None
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
                    try:
                        codepoint = int(self._unicode_digits, 16)
                    except ValueError:
                        codepoint = 0xFFFD
                    if self._high_surrogate is not None:
                        if 0xDC00 <= codepoint <= 0xDFFF:
                            combined = (
                                0x10000
                                + ((self._high_surrogate - 0xD800) << 10)
                                + codepoint - 0xDC00
                            )
                            output.append(chr(combined))
                            self._high_surrogate = None
                            self._unicode_digits = None
                            self._escaped = False
                            continue
                        output.append("�")
                        self._high_surrogate = None
                    if 0xD800 <= codepoint <= 0xDBFF:
                        self._high_surrogate = codepoint
                    else:
                        output.append(
                            chr(codepoint) if not 0xDC00 <= codepoint <= 0xDFFF else "�"
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
                if self._high_surrogate is not None:
                    output.append("�")
                    self._high_surrogate = None
                self.finished = True
                break
            else:
                if self._high_surrogate is not None:
                    output.append("�")
                    self._high_surrogate = None
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
    EXPLORATION_TOOLS = {"read_file", "search", "find_files", "list_files"}

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
        self._live_tool: Live | None = None
        self._tool_request: Mapping[str, Any] | None = None
        self._tool_request_printed = False
        self._exploration: _ExplorationGroup | None = None
        self._live_exploration: Live | None = None
        self._answer_stream = _JsonStringFieldStreamer("final_answer")
        self._answer_text = ""
        self._visible_answer_text = ""
        self._streamed_answer = False
        self._has_answer_stream = False
        self._answer_blocks_written = 0
        self._run_started_at: float | None = None
        self._tool_started_at: float | None = None
        self._activity_handler: Callable[[str | None, float | None], None] | None = None
        self._output_handler: Callable[[str], None] | None = None
        self._render_handler = None

    def set_render_handler(self, handler) -> None:
        """Preserve source renderables for width-dependent transcript layout."""
        self._render_handler = handler

    def set_activity_handler(
        self,
        handler: Callable[[str | None, float | None], None] | None,
    ) -> None:
        """Route transient activity into an owning interactive UI."""

        self._activity_handler = handler

    def set_output_handler(
        self,
        handler: Callable[[str], None] | None,
    ) -> None:
        """Route committed transcript output through the interactive UI."""

        self._output_handler = handler

    def _print(self, *objects: Any, **kwargs: Any) -> None:
        if self._render_handler is not None:
            frozen = tuple(
                obj.__rich__() if isinstance(obj, _ExplorationGroup) else obj
                for obj in objects
            )
            self._render_handler(frozen, kwargs)
            return
        if self._output_handler is None:
            self.console.print(*objects, **kwargs)
            return
        with self.console.capture() as capture:
            self.console.print(*objects, **kwargs)
        rendered = capture.get()
        if rendered:
            self._output_handler(rendered)

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
            self._answer_blocks_written = 0
            step = event.data.get("step", 1)
            self._start_status(f"Thinking · step {step}")
            return
        if event.type is EventType.MODEL_DELTA:
            self._render_answer_delta(str(event.data.get("delta", "")))
            return
        if event.type is EventType.MODEL_COMPLETED:
            self._stop_status()
            response = event.data.get("response", {})
            if isinstance(response, Mapping) and "final_answer" in response:
                self._finish_exploration()
            if isinstance(response, Mapping) and not self._has_answer_stream:
                thought = response.get("thought")
                if isinstance(thought, str) and thought.strip():
                    self._finish_exploration()
                    commentary = Table.grid(padding=0, expand=True)
                    commentary.add_column(width=2, no_wrap=True)
                    commentary.add_column(ratio=1)
                    commentary.add_row(Text("• ", style=MUTED), Markdown(thought.strip()))
                    self._print(Group(Text(""), commentary, Text("")))
            return
        if event.type is EventType.TOOL_REQUESTED:
            tool = str(event.data.get("tool", "unknown"))
            if tool in self.EXPLORATION_TOOLS and self.console.is_terminal:
                self._start_exploration_tool(event.data)
            else:
                self._finish_exploration()
                self._start_tool(event.data)
            return
        if event.type is EventType.TOOL_STARTED:
            if self._tool_started_at is None:
                self._tool_started_at = time.monotonic()
            return
        if event.type is EventType.TOOL_COMPLETED:
            if not self._complete_exploration_tool(event.data):
                self._finish_tool()
                self._render_tool_result(event.data)
            self._tool_started_at = None
            return
        if event.type is EventType.RUN_COMPLETED:
            self._render_completion(event.data)
            self._answer_text = ""
            self._visible_answer_text = ""
            return
        if event.type is EventType.RUN_FAILED:
            self.close()
            error = str(event.data.get("error", "运行失败"))
            self._print(Text.assemble(("✕ ", "bold red"), (error, "red")))
        if event.type is EventType.RUN_CANCELLED:
            self.close()
            remaining = self._answer_text[len(self._visible_answer_text):]
            if remaining.strip():
                if not self._streamed_answer:
                    self._print(Text("✦ Mini Agent", style=f"bold {ACCENT}"))
                self._print(Markdown(remaining))
            self._print(Text("  ■ 当前任务已取消", style="yellow"))
            self._answer_text = ""
            self._visible_answer_text = ""

    def toggle_verbose(self) -> bool:
        self.verbose = not self.verbose
        return self.verbose

    def close(self) -> None:
        self._stop_status()
        self._finish_tool()
        self._finish_exploration()

    def _render_answer_delta(self, delta: str) -> None:
        text = self._answer_stream.feed(delta)
        if not text:
            return
        self._answer_text += text
        if not self._has_answer_stream:
            self._has_answer_stream = True
            self._stop_status()
            self._finish_exploration()
            self._start_status("Responding")
        self._write_stable_answer()

    def _render_completion(self, data: Mapping[str, Any]) -> None:
        self._stop_status()
        answer = str(data.get("answer", ""))
        renderables: list[Any] = []
        if self._streamed_answer:
            committed = len(self._visible_answer_text)
            # Runtime trims the final answer. Streaming offsets refer to the
            # untrimmed JSON string, so leading whitespace must be accounted for.
            if answer == self._answer_text.strip():
                committed -= len(self._answer_text) - len(self._answer_text.lstrip())
            remaining = answer[max(0, committed) :]
            remaining = remaining.strip("\n")
            if remaining:
                if self._answer_blocks_written:
                    renderables.append(Text(""))
                renderables.append(Markdown(remaining))
                self._answer_blocks_written += 1
        else:
            renderables.extend(
                [
                    Text(""),
                    Text("✦ Mini Agent", style=f"bold {ACCENT}"),
                    Markdown(answer),
                ]
            )

        elapsed = self._elapsed(self._run_started_at)
        steps = data.get("steps", 0)
        meta = f"Done in {elapsed} · {steps} step{'s' if steps != 1 else ''}"
        renderables.append(Text(f"  ✓ {meta}", style=MUTED))
        self._print(Group(*renderables))

    def _start_status(self, message: str) -> None:
        self._stop_status()
        started_at = time.monotonic()
        if self._activity_handler is not None:
            self._activity_handler(message, started_at)
            return
        self._status = self.console.status(
            _ElapsedStatus(self, message, started_at),
            spinner="dots",
            spinner_style=ACCENT,
        )
        self._status.start()

    def _stop_status(self) -> None:
        if self._status is not None:
            self._status.stop()
            self._status = None
        if self._activity_handler is not None:
            self._activity_handler(None, None)

    def _write_stable_answer(self) -> None:
        visible_text = self._stable_streaming_text(self._answer_text)
        if not visible_text:
            return
        if visible_text == self._visible_answer_text:
            return
        new_text = visible_text[len(self._visible_answer_text) :].strip("\n")
        self._visible_answer_text = visible_text
        if not new_text:
            return
        renderables: list[Any] = []
        if not self._streamed_answer:
            renderables.extend(
                [
                    Text(""),
                    Text("✦ Mini Agent", style=f"bold {ACCENT}"),
                ]
            )
            self._streamed_answer = True
        elif self._answer_blocks_written:
            renderables.append(Text(""))
        # Commit each complete line exactly once. Re-rendering the accumulated
        # answer in a Live region leaks old frames into scrollback when a long
        # response exceeds the terminal height.
        renderables.append(Markdown(new_text))
        self._print(Group(*renderables))
        self._answer_blocks_written += 1

    @staticmethod
    def _stable_streaming_text(text: str) -> str:
        """Return complete Markdown blocks without splitting fenced code."""

        stable_end = 0
        offset = 0
        fence: str | None = None
        for line in text.splitlines(keepends=True):
            stripped = line.lstrip()
            marker = stripped[:3]
            if marker in {"```", "~~~"}:
                if fence is None:
                    fence = marker
                elif marker == fence:
                    fence = None
            offset += len(line)
            if fence is None and not line.strip():
                stable_end = offset
        return text[:stable_end]

    def _start_tool(self, data: Mapping[str, Any]) -> None:
        self._finish_tool()
        self._tool_request = data
        self._tool_started_at = time.monotonic()
        self._tool_request_printed = False
        if not self.console.is_terminal:
            self._print(self._tool_request_renderable(data))
            self._tool_request_printed = True
            return
        if self._activity_handler is not None:
            self._activity_handler(self._tool_activity(data), self._tool_started_at)
            return
        self._live_tool = self.live_factory(
            _ToolProgress(self, data, self._tool_started_at),
            console=self.console,
            refresh_per_second=12,
            transient=True,
            vertical_overflow="crop",
        )
        self._live_tool.start()

    def _finish_tool(self) -> None:
        if self._live_tool is not None:
            self._live_tool.stop()
            self._live_tool = None
        if self._tool_request is not None and not self._tool_request_printed:
            self._print(self._tool_request_renderable(self._tool_request))
        if self._tool_request is not None and self._activity_handler is not None:
            self._activity_handler(None, None)
        self._tool_request = None
        self._tool_request_printed = False

    def _start_exploration_tool(self, data: Mapping[str, Any]) -> None:
        self._finish_tool()
        now = time.monotonic()
        if self._exploration is None:
            self._exploration = _ExplorationGroup(self)
            if self._activity_handler is None:
                self._live_exploration = self.live_factory(
                    self._exploration,
                    console=self.console,
                    refresh_per_second=12,
                    transient=True,
                    vertical_overflow="crop",
                )
                self._live_exploration.start()
        tool = str(data.get("tool", "unknown"))
        args = data.get("args", {})
        args = args if isinstance(args, Mapping) else {}
        title, details = self._tool_description(tool, args)
        self._exploration.entries.append(
            _ExplorationEntry(tool, title, details, now)
        )
        self._tool_started_at = now
        if self._activity_handler is not None:
            self._activity_handler(self._tool_activity(data), now)
        self._refresh_exploration()

    def _complete_exploration_tool(self, data: Mapping[str, Any]) -> bool:
        if self._exploration is None or self._exploration.active is None:
            return False
        result = data.get("result", {})
        if not isinstance(result, Mapping):
            return False
        entry = self._exploration.active
        if str(result.get("tool", "")) != entry.tool:
            return False
        entry.success = bool(result.get("success"))
        entry.output = str(result.get("output", ""))
        if self._activity_handler is not None:
            self._activity_handler(None, None)
            self._commit_exploration_entries()
        self._refresh_exploration()
        return True

    def _refresh_exploration(self) -> None:
        if self._live_exploration is not None and self._exploration is not None:
            self._live_exploration.update(self._exploration, refresh=True)

    def _finish_exploration(self) -> None:
        if self._exploration is None:
            return
        if self._activity_handler is not None:
            self._activity_handler(None, None)
        if self._live_exploration is not None:
            self._live_exploration.stop()
            self._live_exploration = None
        if self._exploration.committed_entries:
            self._commit_exploration_entries()
        else:
            self._print(self._exploration)
        self._exploration = None

    def _commit_exploration_entries(self) -> None:
        """Commit newly completed exploration rows above the active prompt."""

        group = self._exploration
        if group is None:
            return
        completed = group.entries[group.committed_entries :]
        completed = [entry for entry in completed if entry.success is not None]
        if not completed:
            return
        for entry in completed:
            line = Text("• ", style=MUTED)
            if entry.success is False:
                line.append("✕ ", style="red")
            label = self.TOOL_LABELS.get(entry.tool, entry.tool)
            description = f"{label} {entry.title}".rstrip()
            if entry.details:
                description += f" {' · '.join(entry.details)}"
            line.append(description, style="red" if entry.success is False else None)
            if entry.success is False and entry.output:
                error = self._one_line(entry.output.splitlines()[0], 80)
                line.append(f" — {error}", style="red")
            self._print(line)
            if self.verbose and entry.success is True and entry.output:
                _summary, details = self._tool_result_summary(
                    entry.tool, entry.output, True
                )
                for detail in details:
                    self._print(Text(f"      │ {detail}", style=MUTED))
            group.committed_entries += 1

    def _tool_activity(self, data: Mapping[str, Any]) -> str:
        tool = str(data.get("tool", "unknown"))
        args = data.get("args", {})
        args = args if isinstance(args, Mapping) else {}
        label = self.TOOL_LABELS.get(tool, tool)
        title, _details = self._tool_description(tool, args)
        return f"{label} {title}".rstrip()

    def _exploration_renderable(self, group: _ExplorationGroup) -> Group:
        descriptions = self._exploration_descriptions(group.entries)
        lines: list[Text] = []
        for description, entry in descriptions:
            line = Text("• ", style=MUTED)
            if entry.success is False:
                line.append("✕ ", style="red")
            elif entry.success is None:
                phase = int((time.monotonic() - entry.started_at) * 2) % 2
                line.append("● " if phase == 0 else "  ", style=ACCENT)
            line.append(description, style="red" if entry.success is False else None)
            if entry.success is False and entry.output:
                error = self._one_line(entry.output.splitlines()[0], 80)
                line.append(f" — {error}", style="red")
            if entry.success is None:
                line.append(
                    f" · {self._elapsed(entry.started_at)}",
                    style=MUTED,
                )
            lines.append(line)
            if self.verbose and entry.success is True and entry.output:
                _summary, details = self._tool_result_summary(
                    entry.tool, entry.output, True
                )
                for detail in details:
                    lines.append(Text(f"      │ {detail}", style=MUTED))
        return Group(*lines)

    def _exploration_descriptions(
        self, entries: list[_ExplorationEntry]
    ) -> list[tuple[str, _ExplorationEntry]]:
        descriptions: list[tuple[str, _ExplorationEntry]] = []
        index = 0
        while index < len(entries):
            entry = entries[index]
            if (
                not self.verbose
                and entry.tool == "read_file"
                and entry.success is True
            ):
                paths = [entry.title]
                cursor = index + 1
                while (
                    cursor < len(entries)
                    and entries[cursor].tool == "read_file"
                    and entries[cursor].success is True
                ):
                    paths.append(entries[cursor].title)
                    cursor += 1
                descriptions.append((f"Read {', '.join(paths)}", entry))
                index = cursor
                continue
            label = self.TOOL_LABELS.get(entry.tool, entry.tool)
            description = f"{label} {entry.title}".rstrip()
            if entry.details:
                description += f" {' · '.join(entry.details)}"
            descriptions.append((description, entry))
            index += 1
        return descriptions

    def _tool_request_renderable(
        self,
        data: Mapping[str, Any],
        *,
        marker: str = "●",
        elapsed: str | None = None,
    ) -> Group:
        tool = str(data.get("tool", "unknown"))
        args = data.get("args", {})
        args = args if isinstance(args, Mapping) else {}
        label = self.TOOL_LABELS.get(tool, tool)
        title, details = self._tool_description(tool, args)

        line = Text()
        line.append(f"{marker} ", style=ACCENT)
        line.append(label, style="bold")
        if title:
            line.append(f" {title}")
        if elapsed is not None:
            line.append(f" · {elapsed}", style=MUTED)
        lines: list[Text] = [line]
        for detail in details:
            lines.append(Text(f"  └ {detail}", style=MUTED))
        return Group(*lines)

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
        if tool == "model_protocol":
            self._finish_exploration()
            self._print(Text("• 模型响应格式异常，正在重试…", style="yellow"))
            if self.verbose:
                self._print(Text(f"  {output}", style=MUTED))
            return
        elapsed = self._elapsed(self._tool_started_at)
        summary, details = self._tool_result_summary(tool, output, success)
        marker = "└" if success else "└ ✕"
        style = "green" if success else "red"
        if success and tool == "shell" and not summary.startswith("Exited with code 0"):
            marker = "└ !"
            style = "yellow"
        suffix = f" · {elapsed}" if elapsed != "0ms" else ""
        self._print(Text(f"  {marker} {summary}{suffix}", style=style))
        for detail in details:
            detail_style = MUTED if success else "red"
            if success and tool == "edit_file":
                if detail.startswith("+"):
                    detail_style = "green"
                elif detail.startswith("-"):
                    detail_style = "red"
                elif detail.startswith("@@"):
                    detail_style = ACCENT
            self._print(Text(f"    │ {detail}", style=detail_style))

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
            diff = lines[1:]
            # The request already names the file. Keep every hunk rather than
            # spending the ordinary log preview budget on diff headers/context.
            if len(diff) >= 2 and diff[0].startswith("--- ") and diff[1].startswith("+++ "):
                diff = diff[2:]
            added = sum(line.startswith("+") for line in diff)
            removed = sum(line.startswith("-") for line in diff)
            return f"Updated {summary} · +{added} / -{removed} lines", diff
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
