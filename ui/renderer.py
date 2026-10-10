"""Render agent events as a polished, compact terminal transcript."""

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
from tools.base import ToolResult
from ui.presentation import (
    PresentationAdapter,
    PresentationStore,
    ToolPresenterRegistry,
)
from ui.view_model import (
    AssistantCommentary,
    AssistantTextDelta,
    RunCompleted as PresentationRunCompleted,
    RunStarted as PresentationRunStarted,
    ToolCompleted as PresentationToolCompleted,
    ToolRequested as PresentationToolRequested,
)


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
    call_id: str | None
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
        output_limit: int = 600,
        output_lines: int = 3,
        live_factory: type[Live] = Live,
        presentation_store: PresentationStore | None = None,
    ) -> None:
        self.console = console
        self.verbose = verbose
        self.output_limit = output_limit
        self.output_lines = output_lines
        self.live_factory = live_factory
        if presentation_store is None:
            presenters = ToolPresenterRegistry(
                output_limit=output_limit,
                output_lines=output_lines,
            )
            presentation_store = PresentationStore(
                adapter=PresentationAdapter(presenters=presenters)
            )
        self.presentation_store = presentation_store
        self.tool_presenters = self.presentation_store.adapter.presenters
        self._status: Status | None = None
        self._live_tool: Live | None = None
        self._tool_request: Mapping[str, Any] | None = None
        self._tool_request_printed = False
        self._exploration: _ExplorationGroup | None = None
        self._live_exploration: Live | None = None
        self._answer_text = ""
        self._answer_block_id: str | None = None
        self._visible_answer_text = ""
        self._streamed_answer = False
        self._has_answer_stream = False
        self._answer_blocks_written = 0
        self._run_started_at: float | None = None
        self._tool_started_at: float | None = None
        self._activity_handler: Callable[[str | None, float | None], None] | None = None
        self._output_handler: Callable[[str], None] | None = None
        self._render_handler = None
        self._stream_render_handler = None
        self._block_handler = None

    def set_render_handler(self, handler) -> None:
        """Preserve source renderables for width-dependent transcript layout."""
        self._render_handler = handler

    def set_stream_render_handler(self, handler) -> None:
        """Replace one cumulative Markdown preview instead of appending snapshots."""
        self._stream_render_handler = handler
    def set_block_handler(self, handler) -> None:
        """Upsert one stable transcript block with compact/expanded views."""

        self._block_handler = handler

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
        # Build the new semantic presentation state in parallel with the legacy
        # renderer. Rendering switches to this state in the next migration slice.
        self.presentation_store.handle(event)
        presentation_events = self.presentation_store.last_events
        for presentation_event in presentation_events:
            if isinstance(presentation_event, AssistantTextDelta):
                self._answer_block_id = presentation_event.block_id
                self._render_answer_text_delta(
                    presentation_event.text,
                    block_id=presentation_event.block_id,
                )
        if event.type is EventType.RUN_STARTED:
            self._run_started_at = time.monotonic()
            started = next(
                (
                    item
                    for item in presentation_events
                    if isinstance(item, PresentationRunStarted)
                ),
                None,
            )
            if self._block_handler is not None and started is not None:
                self._block_handler(
                    f"prompt:{started.context.turn_id}",
                    (
                        Text.assemble(
                            ("› ", f"bold {ACCENT}"),
                            (started.prompt, "default"),
                        ),
                    ),
                    None,
                    {},
                )
            return
        if event.type is EventType.MODEL_STARTED:
            self._answer_text = ""
            self._visible_answer_text = ""
            self._streamed_answer = False
            self._has_answer_stream = False
            self._answer_blocks_written = 0
            step = event.data.get("step", 1)
            self._start_status(f"Thinking · step {step}")
            return
        if event.type is EventType.MODEL_DELTA:
            return
        if event.type is EventType.MODEL_COMPLETED:
            self._stop_status()
            for presentation_event in presentation_events:
                if isinstance(presentation_event, AssistantCommentary):
                    self._render_commentary(
                        presentation_event.text,
                        block_id=presentation_event.block_id,
                    )
            return
        if event.type is EventType.TOOL_REQUESTED:
            tool = str(event.data.get("tool", "unknown"))
            requested = next(
                (
                    item
                    for item in presentation_events
                    if isinstance(item, PresentationToolRequested)
                ),
                None,
            )
            if tool in self.EXPLORATION_TOOLS and self.console.is_terminal:
                self._start_exploration_tool(event.data, requested=requested)
            else:
                self._finish_exploration()
                self._start_tool(event.data)
            return
        if event.type is EventType.TOOL_STARTED:
            if self._tool_started_at is None:
                self._tool_started_at = time.monotonic()
            return
        if event.type is EventType.TOOL_COMPLETED:
            completed = next(
                (
                    item
                    for item in presentation_events
                    if isinstance(item, PresentationToolCompleted)
                ),
                None,
            )
            if not self._complete_exploration_tool(
                event.data,
                completed=completed,
            ):
                self._finish_tool()
                self._render_tool_result(event.data, completed=completed)
            self._tool_started_at = None
            return
        if event.type is EventType.RUN_COMPLETED:
            self._finish_exploration()
            completion = next(
                (
                    item
                    for item in presentation_events
                    if isinstance(item, PresentationRunCompleted)
                ),
                None,
            )
            if completion is None:
                self._render_completion(
                    str(event.data.get("answer", "")),
                    int(event.data.get("steps", 0)),
                )
            else:
                self._render_completion(
                    completion.answer,
                    completion.steps,
                    duration_ms=completion.duration_ms,
                    block_id=completion.answer_block_id,
                )
            self._answer_text = ""
            self._visible_answer_text = ""
            self._answer_block_id = None
            return
        if event.type is EventType.RUN_FAILED:
            self._finish_streaming_preview()
            self.close()
            error = str(event.data.get("error", "运行失败"))
            self._print(Text.assemble(("✕ ", "bold red"), (error, "red")))
        if event.type is EventType.RUN_CANCELLED:
            self.close()
            remaining = self._answer_text[len(self._visible_answer_text):]
            if self._block_handler is not None and self._answer_block_id is not None:
                self._upsert_answer_block(
                    self._answer_block_id,
                    self._answer_text,
                )
            elif remaining.strip():
                if not self._streamed_answer:
                    self._print(Text("✦ Mini Agent", style=f"bold {ACCENT}"))
                self._print(Markdown(remaining))
            self._print(Text("  ■ 当前任务已取消", style="yellow"))
            self._answer_text = ""
            self._visible_answer_text = ""
            self._answer_block_id = None

    def toggle_verbose(self) -> bool:
        self.verbose = not self.verbose
        return self.verbose

    def answer_renderables(
        self,
        answer: str,
        *,
        leading_blank: bool = True,
    ) -> tuple[Any, ...]:
        """Build the shared Markdown view used by live and resumed answers."""

        renderables: list[Any] = []
        if leading_blank:
            renderables.append(Text(""))
        renderables.extend(
            [
                Text("✦ Mini Agent", style=f"bold {ACCENT}"),
                Markdown(answer),
            ]
        )
        return tuple(renderables)

    def close(self) -> None:
        self._stop_status()
        self._finish_tool()
        self._finish_exploration()

    def _render_answer_text_delta(self, text: str, *, block_id: str) -> None:
        if not text:
            return
        self._answer_text += text
        if not self._has_answer_stream:
            self._has_answer_stream = True
            self._stop_status()
            self._finish_exploration()
            self._start_status("Responding")
        if self._block_handler is not None:
            self._upsert_answer_block(block_id, self._answer_text)
            return
        self._write_stable_answer()

    def _render_commentary(self, text: str, *, block_id: str) -> None:
        self._finish_exploration()
        commentary = Table.grid(padding=0, expand=True)
        commentary.add_column(width=2, no_wrap=True)
        commentary.add_column(ratio=1)
        commentary.add_row(Text("• ", style=MUTED), Markdown(text))
        renderable = Group(Text(""), commentary, Text(""))
        if self._block_handler is not None:
            self._block_handler(block_id, (renderable,), None, {})
        else:
            self._print(renderable)

    def _render_completion(
        self,
        answer: str,
        steps: int,
        *,
        duration_ms: int | None = None,
        block_id: str | None = None,
    ) -> None:
        self._stop_status()
        renderables: list[Any] = []
        if self._stream_render_handler is not None and self._has_answer_stream:
            self._publish_streaming_preview(answer, final=True)
        elif self._streamed_answer:
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
            renderables.extend(self.answer_renderables(answer))

        elapsed = (
            self._format_duration(duration_ms)
            if duration_ms is not None
            else self._elapsed(self._run_started_at)
        )
        meta = f"Done in {elapsed} · {steps} step{'s' if steps != 1 else ''}"
        if self._block_handler is not None and block_id is not None:
            self._upsert_answer_block(block_id, answer, meta=meta)
            return
        renderables.append(Text(f"  ✓ {meta}", style=MUTED))
        self._print(Group(*renderables))

    def _upsert_answer_block(
        self,
        block_id: str,
        answer: str,
        *,
        meta: str | None = None,
    ) -> None:
        renderables: list[Any] = [
            Text(""),
            Text("✦ Mini Agent", style=f"bold {ACCENT}"),
            Markdown(answer),
        ]
        if meta is not None:
            renderables.append(Text(f"  ✓ {meta}", style=MUTED))
        self._block_handler(
            block_id,
            (Group(*renderables),),
            None,
            {},
        )

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
        if self._stream_render_handler is not None:
            self._publish_streaming_preview(visible_text, final=False)
            return
        # A permanent terminal transcript cannot safely append independently
        # parsed Markdown suffixes: lists, quotes, tables, and indented code
        # may continue across a blank line. Non-interactive output therefore
        # waits for RUN_COMPLETED and renders the complete document once.
        return

    def _publish_streaming_preview(self, text: str, *, final: bool) -> None:
        if self._stream_render_handler is None:
            return
        renderables = list(self.answer_renderables(text))
        self._stream_render_handler(tuple(renderables), {}, final)
        self._streamed_answer = True
        self._answer_blocks_written = 1

    def _finish_streaming_preview(self) -> None:
        if self._stream_render_handler is not None and self._answer_text.strip():
            self._publish_streaming_preview(self._answer_text, final=True)

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

    def _start_exploration_tool(
        self,
        data: Mapping[str, Any],
        *,
        requested: PresentationToolRequested | None = None,
    ) -> None:
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
            _ExplorationEntry(
                requested.call_id if requested is not None else None,
                tool,
                title,
                details,
                now,
            )
        )
        self._tool_started_at = now
        if self._activity_handler is not None:
            self._activity_handler(self._tool_activity(data), now)
        self._refresh_exploration()

    def _complete_exploration_tool(
        self,
        data: Mapping[str, Any],
        *,
        completed: PresentationToolCompleted | None = None,
    ) -> bool:
        if self._exploration is None or self._exploration.active is None:
            return False
        result = data.get("result", {})
        if not isinstance(result, Mapping):
            return False
        entry = self._exploration.active
        if completed is not None and entry.call_id not in {None, completed.call_id}:
            return False
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
            result = self.tool_presenters.result(
                ToolResult(entry.tool, {}, bool(entry.success), entry.output),
                expanded=True,
            )
            detail_lines = tuple(
                Text(
                    f"      │ {detail}",
                    style=MUTED if entry.success else "red",
                )
                for detail in result.preview
            )
            if self._block_handler is not None and entry.call_id is not None:
                self._block_handler(
                    f"tool-result:{entry.call_id}",
                    (line,),
                    (line, *detail_lines) if detail_lines else None,
                    {},
                )
            else:
                self._print(line)
                if self.verbose:
                    for detail in detail_lines:
                        self._print(detail)
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
        request = self.tool_presenters.request(tool, args)
        if request.kind == "shell" and request.subject:
            return "", [f"$ {request.subject}", *request.details]
        return request.subject, list(request.details)

    def _render_tool_result(
        self,
        data: Mapping[str, Any],
        *,
        completed: PresentationToolCompleted | None = None,
    ) -> None:
        result = data.get("result", {})
        if not isinstance(result, Mapping):
            return
        tool = str(result.get("tool", ""))
        success = bool(result.get("success"))
        output = str(result.get("output", ""))
        if tool == "model_protocol":
            self._finish_exploration()
            self._print(
                Text("• 模型响应格式异常，正在重试…", style="yellow")
            )
            self._print(Text(f"  {output}", style=MUTED))
            return
        args = result.get("args", {})
        args = args if isinstance(args, Mapping) else {}
        raw_result = ToolResult(tool, args, success, output)
        if completed is None:
            presented = self.tool_presenters.result(
                raw_result,
                expanded=self.verbose,
            )
            for renderable in self._tool_result_renderables(
                tool,
                success,
                presented.summary,
                presented.preview,
                self._elapsed(self._tool_started_at),
            ):
                self._print(renderable)
            return

        elapsed = (
            self._format_duration(completed.duration_ms)
            if completed.duration_ms is not None
            else self._elapsed(self._tool_started_at)
        )
        compact = completed.result
        compact_renderables = self._tool_result_renderables(
            tool,
            success,
            compact.summary,
            compact.preview,
            elapsed,
        )
        expanded = self.tool_presenters.result(raw_result, expanded=True)
        expanded_renderables = self._tool_result_renderables(
            tool,
            success,
            expanded.summary,
            expanded.preview,
            elapsed,
        )
        if self._block_handler is not None:
            self._block_handler(
                f"tool-result:{completed.call_id}",
                compact_renderables,
                expanded_renderables if compact.truncated else None,
                {},
            )
            return
        renderables = expanded_renderables if self.verbose else compact_renderables
        for renderable in renderables:
            self._print(renderable)

    @staticmethod
    def _tool_result_renderables(
        tool: str,
        success: bool,
        summary: str,
        details: tuple[str, ...],
        elapsed: str,
    ) -> tuple[Text, ...]:
        marker = "└" if success else "└ ✕"
        style = "green" if success else "red"
        if success and tool == "shell" and not summary.startswith("Exited with code 0"):
            marker = "└ !"
            style = "yellow"
        suffix = f" · {elapsed}" if elapsed != "0ms" else ""
        renderables = [Text(f"  {marker} {summary}{suffix}", style=style)]
        for detail in details:
            detail_style = MUTED if success else "red"
            if success and tool == "edit_file":
                if detail.startswith("+"):
                    detail_style = "green"
                elif detail.startswith("-"):
                    detail_style = "red"
                elif detail.startswith("@@"):
                    detail_style = ACCENT
            renderables.append(Text(f"    │ {detail}", style=detail_style))
        return tuple(renderables)

    def _tool_result_summary(
        self, tool: str, output: str, success: bool
    ) -> tuple[str, list[str]]:
        result = self.tool_presenters.result(
            ToolResult(tool, {}, success, output),
            expanded=self.verbose,
        )
        return result.summary, list(result.preview)

    @staticmethod
    def _one_line(value: str, limit: int) -> str:
        value = " ".join(value.split())
        return value if len(value) <= limit else value[: limit - 1] + "…"

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
    def _format_duration(duration_ms: int) -> str:
        if duration_ms < 1000:
            return f"{duration_ms}ms"
        seconds = duration_ms / 1000
        if seconds < 60:
            return f"{seconds:.1f}s"
        return f"{int(seconds // 60)}m {int(seconds % 60)}s"
