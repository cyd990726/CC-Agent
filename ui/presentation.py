"""Normalize runtime events into platform-neutral presentation events."""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from agent.events import AgentEvent, EventType
from tools.base import ToolResult
from ui.view_model import (
    AssistantCommentary,
    AssistantTextDelta,
    EventContext,
    ModelStarted,
    NoticeAdded,
    PermissionChoiceViewModel,
    PermissionModeChanged,
    PermissionRequestViewModel,
    PermissionRequested,
    PermissionResolved,
    PresentationEvent,
    RunCancelled,
    RunCompleted,
    RunFailed,
    RunStarted,
    SessionPresentation,
    TaskQueued,
    ToolCompleted,
    ToolRequested,
    ToolRequestView,
    ToolResultView,
    ToolStarted,
    reduce_presentation,
)


class JsonStringFieldStreamer:
    """Incrementally decode one JSON string field from arbitrary chunks."""

    def __init__(self, field_name: str) -> None:
        self._pattern = re.compile(rf'"{re.escape(field_name)}"\s*:\s*"')
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
                                + codepoint
                                - 0xDC00
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
                            chr(codepoint)
                            if not 0xDC00 <= codepoint <= 0xDFFF
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


class TextDeltaBuffer:
    """Coalesce tiny model deltas before updating presentation state."""

    def __init__(self, frame_interval_ms: int = 33) -> None:
        if frame_interval_ms < 0:
            raise ValueError("frame_interval_ms cannot be negative")
        self.frame_interval_ms = frame_interval_ms
        self._chunks: list[str] = []
        self._last_flush_ms: int | None = None
        self._has_flushed = False

    def reset(self, now_ms: int) -> None:
        self._chunks.clear()
        self._last_flush_ms = now_ms
        self._has_flushed = False

    def push(self, text: str, now_ms: int) -> str:
        if not text:
            return ""
        self._chunks.append(text)
        elapsed = (
            self.frame_interval_ms
            if self._last_flush_ms is None
            else now_ms - self._last_flush_ms
        )
        if not self._has_flushed or "\n" in text or elapsed >= self.frame_interval_ms:
            return self.flush(now_ms)
        return ""

    def flush(self, now_ms: int) -> str:
        text = "".join(self._chunks)
        self._chunks.clear()
        self._last_flush_ms = now_ms
        if text:
            self._has_flushed = True
        return text


@dataclass(frozen=True)
class ToolRequestPresentation:
    kind: str
    subject: str
    details: tuple[str, ...] = ()


@dataclass(frozen=True)
class ToolResultPresentation:
    summary: str
    metrics: Mapping[str, str | int | float | bool | None] = field(
        default_factory=dict
    )
    preview: tuple[str, ...] = ()
    truncated: bool = False
    output_ref: str | None = None


class ToolPresenter(Protocol):
    def present_request(
        self,
        args: Mapping[str, Any],
    ) -> ToolRequestPresentation:
        ...

    def present_result(
        self,
        result: ToolResult,
        *,
        expanded: bool = False,
    ) -> ToolResultPresentation:
        ...


class _BasePresenter:
    kind = "generic"

    def __init__(self, *, output_limit: int, output_lines: int) -> None:
        self.output_limit = output_limit
        self.output_lines = output_lines

    def present_request(
        self,
        args: Mapping[str, Any],
    ) -> ToolRequestPresentation:
        details = tuple(
            f"{name}: {_format_value(value)}" for name, value in args.items()
        )
        return ToolRequestPresentation(self.kind, "", details)

    def present_result(
        self,
        result: ToolResult,
        *,
        expanded: bool = False,
    ) -> ToolResultPresentation:
        return ToolResultPresentation(
            summary="Done" if result.success else "Failed",
            preview=self._preview(result.output, expanded=expanded),
            truncated=self._is_truncated(result.output, expanded=expanded),
        )

    def _preview(self, value: str, *, expanded: bool = False) -> tuple[str, ...]:
        if not value:
            return ()
        lines = value.splitlines()
        if expanded:
            return tuple(lines)
        character_clipped = len(value) > self.output_limit
        visible = value[: self.output_limit].splitlines()[: self.output_lines]
        omitted_lines = max(0, len(lines) - len(visible))
        if omitted_lines:
            noun = "line" if omitted_lines == 1 else "lines"
            visible.append(
                f"… {omitted_lines:,} more {noun} · /verbose to expand"
            )
        elif character_clipped:
            visible.append("… output truncated · /verbose to expand")
        return tuple(visible)

    def _is_truncated(self, value: str, *, expanded: bool = False) -> bool:
        if expanded:
            return False
        return (
            len(value) > self.output_limit
            or len(value.splitlines()) > self.output_lines
        )


class _ReadFilePresenter(_BasePresenter):
    kind = "file_read"

    def present_request(self, args: Mapping[str, Any]) -> ToolRequestPresentation:
        details: list[str] = []
        if "offset" in args or "limit" in args:
            offset = args.get("offset", 1)
            limit = args.get("limit")
            if isinstance(limit, int):
                details.append(f"lines {offset}-{offset + limit - 1}")
            else:
                details.append(f"from line {offset}")
        return ToolRequestPresentation(
            self.kind,
            str(args.get("path", "")),
            tuple(details),
        )

    def present_result(
        self,
        result: ToolResult,
        *,
        expanded: bool = False,
    ) -> ToolResultPresentation:
        if not result.success:
            return super().present_result(result, expanded=expanded)
        range_match = re.match(r"^\[lines (\d+)-(\d+) of (\d+)\]", result.output)
        if range_match:
            start, end, total = range_match.groups()
            return ToolResultPresentation(
                summary=f"Read lines {start}-{end} of {total}",
                metrics={
                    "start_line": int(start),
                    "end_line": int(end),
                    "total_lines": int(total),
                },
                preview=(
                    self._preview(result.output, expanded=True)
                    if expanded
                    else ()
                ),
                truncated=bool(result.output) and not expanded,
            )
        if result.output == "[file is empty: 0 lines]":
            return ToolResultPresentation(
                summary="File is empty",
                metrics={"line_count": 0, "bytes": 0},
            )
        lines = result.output.splitlines()
        size = len(result.output.encode("utf-8"))
        return ToolResultPresentation(
            summary=f"Read {len(lines):,} lines · {_format_bytes(size)}",
            metrics={"line_count": len(lines), "bytes": size},
            preview=(
                self._preview(result.output, expanded=True) if expanded else ()
            ),
            truncated=bool(result.output) and not expanded,
        )


class _WriteFilePresenter(_BasePresenter):
    kind = "file_write"

    def present_request(self, args: Mapping[str, Any]) -> ToolRequestPresentation:
        content = args.get("content")
        details = (
            (f"{len(content):,} characters",) if isinstance(content, str) else ()
        )
        return ToolRequestPresentation(
            self.kind,
            str(args.get("path", "")),
            details,
        )

    def present_result(
        self,
        result: ToolResult,
        *,
        expanded: bool = False,
    ) -> ToolResultPresentation:
        if not result.success:
            return super().present_result(result, expanded=expanded)
        return ToolResultPresentation(summary=result.output or "File written")


class _EditFilePresenter(_BasePresenter):
    kind = "file_edit"

    def present_request(self, args: Mapping[str, Any]) -> ToolRequestPresentation:
        old_text = str(args.get("old_text", ""))
        new_text = str(args.get("new_text", ""))
        return ToolRequestPresentation(
            self.kind,
            str(args.get("path", "")),
            (f"{len(old_text):,} → {len(new_text):,} characters",),
        )

    def present_result(
        self,
        result: ToolResult,
        *,
        expanded: bool = False,
    ) -> ToolResultPresentation:
        if not result.success:
            return super().present_result(result, expanded=expanded)
        lines = result.output.splitlines()
        subject = lines[0].removeprefix("updated ") if lines else "file"
        diff = lines[1:]
        if len(diff) >= 2 and diff[0].startswith("--- ") and diff[1].startswith(
            "+++ "
        ):
            diff = diff[2:]
        added = sum(line.startswith("+") for line in diff)
        removed = sum(line.startswith("-") for line in diff)
        return ToolResultPresentation(
            summary=f"Updated {subject} · +{added} / -{removed} lines",
            metrics={"added_lines": added, "removed_lines": removed},
            preview=tuple(diff) if expanded else (),
            truncated=bool(diff) and not expanded,
        )


class _CountPresenter(_BasePresenter):
    def __init__(
        self,
        kind: str,
        subject_key: str,
        verb: str,
        empty_output: str,
        *,
        output_limit: int,
        output_lines: int,
    ) -> None:
        super().__init__(
            output_limit=output_limit,
            output_lines=output_lines,
        )
        self.kind = kind
        self.subject_key = subject_key
        self.verb = verb
        self.empty_output = empty_output

    def present_request(self, args: Mapping[str, Any]) -> ToolRequestPresentation:
        subject = str(args.get(self.subject_key, ""))
        path = args.get("path", ".")
        details = (f"in {path}",)
        return ToolRequestPresentation(self.kind, subject, details)

    def present_result(
        self,
        result: ToolResult,
        *,
        expanded: bool = False,
    ) -> ToolResultPresentation:
        if not result.success:
            return super().present_result(result, expanded=expanded)
        if result.output == self.empty_output:
            return ToolResultPresentation(summary=self.empty_output.capitalize())
        count = len(result.output.splitlines())
        return ToolResultPresentation(
            summary=f"{self.verb} {count:,}",
            metrics={"count": count},
            preview=self._preview(result.output, expanded=expanded),
            truncated=self._is_truncated(result.output, expanded=expanded),
        )


class _ListFilesPresenter(_CountPresenter):
    def present_request(self, args: Mapping[str, Any]) -> ToolRequestPresentation:
        return ToolRequestPresentation(
            self.kind,
            str(args.get("path", ".")),
            (f"depth {args.get('depth', 1)}",),
        )


class _SearchPresenter(_CountPresenter):
    def present_request(self, args: Mapping[str, Any]) -> ToolRequestPresentation:
        query = _one_line(str(args.get("query", "")), 72)
        detail = f"in {args.get('path', '.')}"
        glob = args.get("glob")
        if glob:
            detail += f" · {glob}"
        return ToolRequestPresentation(
            self.kind,
            json.dumps(query, ensure_ascii=False),
            (detail,),
        )


class _ShellPresenter(_BasePresenter):
    kind = "shell"

    def present_request(self, args: Mapping[str, Any]) -> ToolRequestPresentation:
        command = _one_line(str(args.get("command", "")), 100)
        return ToolRequestPresentation(self.kind, command)

    def present_result(
        self,
        result: ToolResult,
        *,
        expanded: bool = False,
    ) -> ToolResultPresentation:
        if not result.success:
            return super().present_result(result, expanded=expanded)
        try:
            parsed = json.loads(result.output)
        except (json.JSONDecodeError, TypeError):
            parsed = None
        if not isinstance(parsed, dict):
            return ToolResultPresentation(
                summary="Completed",
                preview=self._preview(result.output, expanded=expanded),
                truncated=self._is_truncated(
                    result.output,
                    expanded=expanded,
                ),
            )
        exit_code = parsed.get("exit_code")
        stdout = str(parsed.get("stdout", "")).strip()
        stderr = str(parsed.get("stderr", "")).strip()
        details = "\n".join(part for part in (stdout, stderr) if part)
        return ToolResultPresentation(
            summary=f"Exited with code {exit_code}",
            metrics={"exit_code": exit_code},
            preview=self._preview(details, expanded=expanded),
            truncated=self._is_truncated(details, expanded=expanded),
        )


class _WebPresenter(_BasePresenter):
    def __init__(
        self,
        kind: str,
        subject_key: str,
        *,
        output_limit: int,
        output_lines: int,
    ) -> None:
        super().__init__(
            output_limit=output_limit,
            output_lines=output_lines,
        )
        self.kind = kind
        self.subject_key = subject_key

    def present_request(self, args: Mapping[str, Any]) -> ToolRequestPresentation:
        return ToolRequestPresentation(
            self.kind,
            _one_line(str(args.get(self.subject_key, "")), 100),
        )


class ToolPresenterRegistry:
    """Describe tool calls without coupling tools to a UI framework."""

    def __init__(
        self,
        *,
        output_limit: int = 1200,
        output_lines: int = 10,
    ) -> None:
        options = {
            "output_limit": output_limit,
            "output_lines": output_lines,
        }
        self._presenters: dict[str, ToolPresenter] = {
            "read_file": _ReadFilePresenter(**options),
            "write_file": _WriteFilePresenter(**options),
            "edit_file": _EditFilePresenter(**options),
            "find_files": _CountPresenter(
                "file_find", "pattern", "Found", "no files", **options
            ),
            "list_files": _ListFilesPresenter(
                "file_list", "path", "Listed", "empty directory", **options
            ),
            "search": _SearchPresenter(
                "search", "query", "Found", "no matches", **options
            ),
            "shell": _ShellPresenter(**options),
            "web_search": _WebPresenter(
                "web_search", "query", **options
            ),
            "fetch_url": _WebPresenter("web_fetch", "url", **options),
        }
        self._fallback = _BasePresenter(**options)

    def request(
        self,
        tool_name: str,
        args: Mapping[str, Any],
    ) -> ToolRequestPresentation:
        return self._presenters.get(tool_name, self._fallback).present_request(args)

    def result(
        self,
        result: ToolResult,
        *,
        expanded: bool = False,
    ) -> ToolResultPresentation:
        return self._presenters.get(result.tool, self._fallback).present_result(
            result,
            expanded=expanded,
        )


class PresentationAdapter:
    """Translate legacy runtime events into typed presentation events."""

    def __init__(
        self,
        *,
        session_id: str | None = None,
        presenters: ToolPresenterRegistry | None = None,
        id_factory: Callable[[], str] | None = None,
        wall_clock_ms: Callable[[], int] | None = None,
        monotonic_ms: Callable[[], int] | None = None,
        frame_interval_ms: int = 33,
    ) -> None:
        self.session_id = session_id or uuid.uuid4().hex
        self.presenters = presenters or ToolPresenterRegistry()
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)
        self._wall_clock_ms = wall_clock_ms or (lambda: int(time.time() * 1000))
        self._monotonic_ms = monotonic_ms or (
            lambda: int(time.monotonic() * 1000)
        )
        self._sequence = 0
        self._turn_id: str | None = None
        self._turn_started_ms: int | None = None
        self._answer_block_id: str | None = None
        self._answer_stream = JsonStringFieldStreamer("final_answer")
        self._delta_buffer = TextDeltaBuffer(frame_interval_ms)
        self._pending_call_ids: list[str] = []
        self._call_started_ms: dict[str, int] = {}
        self._queued_turn_ids: list[str] = []

    def queue_task(self, prompt: str) -> TaskQueued:
        turn_id = self._new_id("turn")
        self._queued_turn_ids.append(turn_id)
        return TaskQueued(
            self._context(turn_id, self._wall_clock_ms()),
            prompt,
        )

    def request_permission(
        self,
        tool_name: str,
        args: Mapping[str, Any],
        choices: tuple[PermissionChoiceViewModel, ...],
    ) -> PermissionRequested | None:
        if self._turn_id is None:
            return None
        call_id = self._event_call_id({})
        if call_id is None:
            return None
        request = self.presenters.request(tool_name, args)
        request_id = self._new_id("permission")
        return PermissionRequested(
            self._context(self._turn_id, self._wall_clock_ms()),
            PermissionRequestViewModel(
                request_id=request_id,
                call_id=call_id,
                kind=request.kind,
                title=f"Allow {tool_name}?",
                risk=None,
                preview=(request.subject, *request.details),
                choices=choices,
            ),
        )

    def resolve_permission(
        self,
        request_id: str,
        call_id: str,
        *,
        allowed: bool,
    ) -> PermissionResolved | None:
        if self._turn_id is None:
            return None
        return PermissionResolved(
            self._context(self._turn_id, self._wall_clock_ms()),
            request_id,
            call_id,
            allowed,
        )

    def permission_mode_changed(self, mode: str) -> PermissionModeChanged:
        return PermissionModeChanged(
            self._context(self._turn_id or "", self._wall_clock_ms()),
            mode,
        )

    def translate(self, event: AgentEvent) -> tuple[PresentationEvent, ...]:
        wall_now = self._wall_clock_ms()
        monotonic_now = self._monotonic_ms()

        if event.type is EventType.RUN_STARTED:
            provided = self._provided_id(event.data, "turn_id")
            if provided is not None:
                turn_id = provided
                self._queued_turn_ids = [
                    item for item in self._queued_turn_ids if item != turn_id
                ]
            elif self._queued_turn_ids:
                turn_id = self._queued_turn_ids.pop(0)
            else:
                turn_id = self._new_id("turn")
            self._turn_id = turn_id
            self._turn_started_ms = monotonic_now
            self._answer_block_id = f"answer:{turn_id}"
            self._answer_stream = JsonStringFieldStreamer("final_answer")
            self._delta_buffer.reset(monotonic_now)
            self._pending_call_ids.clear()
            self._call_started_ms.clear()
            return (
                RunStarted(
                    self._context(turn_id, wall_now),
                    str(event.data.get("task", "")),
                ),
            )

        turn_id = self._turn_id
        if turn_id is None:
            return ()

        if event.type is EventType.MODEL_STARTED:
            self._answer_stream = JsonStringFieldStreamer("final_answer")
            self._delta_buffer.reset(monotonic_now)
            return (
                ModelStarted(
                    self._context(turn_id, wall_now),
                    int(event.data.get("step", 0)),
                ),
            )

        if event.type is EventType.MODEL_DELTA:
            text = self._answer_stream.feed(str(event.data.get("delta", "")))
            flushed = self._delta_buffer.push(text, monotonic_now)
            return self._text_events(turn_id, wall_now, flushed)

        events = list(self._flush_text(turn_id, wall_now, monotonic_now))

        if event.type is EventType.MODEL_COMPLETED:
            response = event.data.get("response", {})
            if isinstance(response, Mapping) and "final_answer" not in response:
                thought = response.get("thought")
                if isinstance(thought, str) and thought.strip():
                    events.append(
                        AssistantCommentary(
                            self._context(turn_id, wall_now),
                            self._new_id("commentary"),
                            thought.strip(),
                        )
                    )
            return tuple(events)

        if event.type is EventType.TOOL_REQUESTED:
            tool_name = str(event.data.get("tool", "unknown"))
            args = event.data.get("args", {})
            args = args if isinstance(args, Mapping) else {}
            call_id = self._provided_id(event.data, "call_id") or self._new_id(
                "call"
            )
            self._pending_call_ids.append(call_id)
            request = self.presenters.request(tool_name, args)
            events.append(
                ToolRequested(
                    self._context(turn_id, wall_now),
                    call_id,
                    tool_name,
                    ToolRequestView(
                        request.kind,
                        request.subject,
                        request.details,
                    ),
                )
            )
            return tuple(events)

        if event.type is EventType.TOOL_STARTED:
            call_id = self._event_call_id(event.data)
            if call_id is not None:
                self._call_started_ms[call_id] = monotonic_now
                events.append(
                    ToolStarted(
                        self._context(turn_id, wall_now),
                        call_id,
                    )
                )
            return tuple(events)

        if event.type is EventType.TOOL_COMPLETED:
            raw_result = event.data.get("result", {})
            if not isinstance(raw_result, Mapping):
                return tuple(events)
            tool_name = str(raw_result.get("tool", "unknown"))
            if tool_name == "model_protocol":
                events.append(
                    NoticeAdded(
                        self._context(turn_id, wall_now),
                        self._new_id("notice"),
                        "warning",
                        "Model response format was invalid; retrying",
                        str(raw_result.get("output", "")) or None,
                    )
                )
                return tuple(events)
            call_id = self._event_call_id(raw_result)
            if call_id is None:
                return tuple(events)
            args = raw_result.get("args", {})
            args = args if isinstance(args, Mapping) else {}
            result = ToolResult(
                tool=tool_name,
                args=dict(args),
                success=bool(raw_result.get("success")),
                output=str(raw_result.get("output", "")),
            )
            presented = self.presenters.result(result)
            started = self._call_started_ms.pop(call_id, None)
            duration = None if started is None else max(0, monotonic_now - started)
            events.append(
                ToolCompleted(
                    self._context(turn_id, wall_now),
                    call_id,
                    result.success,
                    ToolResultView(
                        summary=presented.summary,
                        metrics=dict(presented.metrics),
                        preview=presented.preview,
                        truncated=presented.truncated,
                        output_ref=presented.output_ref,
                    ),
                    duration,
                )
            )
            self._discard_call(call_id)
            return tuple(events)

        duration = self._duration(monotonic_now)
        if event.type is EventType.RUN_COMPLETED:
            events.append(
                RunCompleted(
                    self._context(turn_id, wall_now),
                    self._answer_block_id or f"answer:{turn_id}",
                    str(event.data.get("answer", "")),
                    int(event.data.get("steps", 0)),
                    duration,
                )
            )
            self._finish_turn()
        elif event.type is EventType.RUN_FAILED:
            events.append(
                RunFailed(
                    self._context(turn_id, wall_now),
                    self._new_id("error"),
                    str(event.data.get("error", "Run failed")),
                    duration,
                )
            )
            self._finish_turn()
        elif event.type is EventType.RUN_CANCELLED:
            events.append(
                RunCancelled(
                    self._context(turn_id, wall_now),
                    self._new_id("cancelled"),
                    duration,
                )
            )
            self._finish_turn()
        return tuple(events)

    def _flush_text(
        self,
        turn_id: str,
        wall_now: int,
        monotonic_now: int,
    ) -> tuple[PresentationEvent, ...]:
        return self._text_events(
            turn_id,
            wall_now,
            self._delta_buffer.flush(monotonic_now),
        )

    def _text_events(
        self,
        turn_id: str,
        wall_now: int,
        text: str,
    ) -> tuple[PresentationEvent, ...]:
        if not text:
            return ()
        return (
            AssistantTextDelta(
                self._context(turn_id, wall_now),
                self._answer_block_id or f"answer:{turn_id}",
                text,
            ),
        )

    def _context(self, turn_id: str, occurred_at_ms: int) -> EventContext:
        self._sequence += 1
        return EventContext(
            session_id=self.session_id,
            turn_id=turn_id,
            sequence=self._sequence,
            occurred_at_ms=occurred_at_ms,
        )

    def _event_call_id(self, data: Mapping[str, Any]) -> str | None:
        supplied = self._provided_id(data, "call_id")
        if supplied is not None:
            return supplied
        return self._pending_call_ids[0] if self._pending_call_ids else None

    def _discard_call(self, call_id: str) -> None:
        self._pending_call_ids = [
            item for item in self._pending_call_ids if item != call_id
        ]

    def _duration(self, monotonic_now: int) -> int | None:
        if self._turn_started_ms is None:
            return None
        return max(0, monotonic_now - self._turn_started_ms)

    def _finish_turn(self) -> None:
        self._turn_id = None
        self._turn_started_ms = None
        self._answer_block_id = None
        self._pending_call_ids.clear()
        self._call_started_ms.clear()

    def _new_id(self, prefix: str) -> str:
        return f"{prefix}:{self._id_factory()}"

    @staticmethod
    def _provided_id(data: Mapping[str, Any], name: str) -> str | None:
        value = data.get(name)
        return value if isinstance(value, str) and value else None


class PresentationStore:
    """Own the current snapshot while the legacy renderer is migrated."""

    def __init__(
        self,
        *,
        adapter: PresentationAdapter | None = None,
        permission_mode: str = "ask",
    ) -> None:
        self.adapter = adapter or PresentationAdapter()
        self._lock = threading.RLock()
        self.state = SessionPresentation.empty(
            self.adapter.session_id,
            permission_mode=permission_mode,
        )
        self.last_events: tuple[PresentationEvent, ...] = ()

    def handle(self, event: AgentEvent) -> SessionPresentation:
        with self._lock:
            return self._apply(self.adapter.translate(event))

    def queue_task(self, prompt: str) -> str:
        with self._lock:
            event = self.adapter.queue_task(prompt)
            self._apply((event,))
            return event.context.turn_id

    def request_permission(
        self,
        tool_name: str,
        args: Mapping[str, Any],
        choices: tuple[PermissionChoiceViewModel, ...],
    ) -> PermissionRequestViewModel | None:
        with self._lock:
            event = self.adapter.request_permission(tool_name, args, choices)
            if event is None:
                return None
            self._apply((event,))
            return event.request

    def resolve_permission(
        self,
        request_id: str,
        call_id: str,
        *,
        allowed: bool,
    ) -> None:
        with self._lock:
            event = self.adapter.resolve_permission(
                request_id,
                call_id,
                allowed=allowed,
            )
            if event is not None:
                self._apply((event,))

    def set_permission_mode(self, mode: str) -> None:
        with self._lock:
            self._apply((self.adapter.permission_mode_changed(mode),))

    def _apply(
        self,
        events: tuple[PresentationEvent, ...],
    ) -> SessionPresentation:
        self.last_events = events
        for normalized in events:
            self.state = reduce_presentation(self.state, normalized)
        return self.state


def _one_line(value: str, limit: int) -> str:
    value = " ".join(value.split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _format_bytes(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def _format_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return repr(value)
