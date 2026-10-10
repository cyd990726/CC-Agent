"""Platform-neutral presentation state for agent sessions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, TypeAlias


JsonScalar: TypeAlias = str | int | float | bool | None


class TurnPhase(str, Enum):
    QUEUED = "queued"
    THINKING = "thinking"
    WAITING_PERMISSION = "waiting_permission"
    RUNNING_TOOLS = "running_tools"
    RESPONDING = "responding"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class BlockStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class TextBlockViewModel:
    id: str
    role: str
    text: str
    streaming: bool = False


@dataclass(frozen=True)
class ToolBlockViewModel:
    id: str
    call_id: str
    tool_name: str
    kind: str
    subject: str
    details: tuple[str, ...]
    status: BlockStatus
    summary: str | None = None
    metrics: Mapping[str, JsonScalar] = field(default_factory=dict)
    preview: tuple[str, ...] = ()
    truncated: bool = False
    output_ref: str | None = None
    started_at_ms: int | None = None
    duration_ms: int | None = None


@dataclass(frozen=True)
class NoticeBlockViewModel:
    id: str
    level: str
    title: str
    detail: str | None = None


ViewBlock: TypeAlias = (
    TextBlockViewModel | ToolBlockViewModel | NoticeBlockViewModel
)


@dataclass(frozen=True)
class TurnViewModel:
    id: str
    prompt: str
    phase: TurnPhase
    blocks: tuple[ViewBlock, ...] = ()
    step: int = 0
    started_at_ms: int | None = None
    duration_ms: int | None = None


@dataclass(frozen=True)
class PermissionChoiceViewModel:
    id: str
    label: str
    description: str


@dataclass(frozen=True)
class PermissionRequestViewModel:
    request_id: str
    call_id: str
    kind: str
    title: str
    risk: str | None
    preview: tuple[str, ...]
    choices: tuple[PermissionChoiceViewModel, ...]


@dataclass(frozen=True)
class SessionPresentation:
    schema_version: int
    session_id: str
    turns: tuple[TurnViewModel, ...] = ()
    active_turn_id: str | None = None
    queued_turn_ids: tuple[str, ...] = ()
    permission_requests: tuple[PermissionRequestViewModel, ...] = ()
    permission_mode: str = "ask"
    last_sequence: int = 0

    @classmethod
    def empty(
        cls,
        session_id: str,
        *,
        permission_mode: str = "ask",
    ) -> "SessionPresentation":
        return cls(
            schema_version=1,
            session_id=session_id,
            permission_mode=permission_mode,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible snapshot without UI-framework objects."""

        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "turns": [_turn_to_dict(turn) for turn in self.turns],
            "active_turn_id": self.active_turn_id,
            "queued_turn_ids": list(self.queued_turn_ids),
            "permission_requests": [
                _permission_to_dict(request)
                for request in self.permission_requests
            ],
            "permission_mode": self.permission_mode,
            "last_sequence": self.last_sequence,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SessionPresentation":
        """Restore a snapshot produced by :meth:`to_dict`."""

        version = int(value.get("schema_version", 1))
        if version != 1:
            raise ValueError(f"unsupported presentation schema version: {version}")
        return cls(
            schema_version=version,
            session_id=str(value["session_id"]),
            turns=tuple(_turn_from_dict(item) for item in value.get("turns", ())),
            active_turn_id=_optional_string(value.get("active_turn_id")),
            queued_turn_ids=tuple(
                str(item) for item in value.get("queued_turn_ids", ())
            ),
            permission_requests=tuple(
                _permission_from_dict(item)
                for item in value.get("permission_requests", ())
            ),
            permission_mode=str(value.get("permission_mode", "ask")),
            last_sequence=int(value.get("last_sequence", 0)),
        )


@dataclass(frozen=True)
class TerminalViewState:
    follow_tail: bool = True
    anchor_block_id: str | None = None
    unseen_count: int = 0
    expanded_block_ids: frozenset[str] = frozenset()
    focused_block_id: str | None = None
    active_permission_id: str | None = None
    selected_permission_index: int = 0
    active_overlay: str | None = None
    command_palette_open: bool = False
    transcript_width: int = 80


@dataclass(frozen=True)
class EventContext:
    session_id: str
    turn_id: str
    sequence: int
    occurred_at_ms: int


@dataclass(frozen=True)
class TaskQueued:
    context: EventContext
    prompt: str


@dataclass(frozen=True)
class RunStarted:
    context: EventContext
    prompt: str


@dataclass(frozen=True)
class ModelStarted:
    context: EventContext
    step: int


@dataclass(frozen=True)
class AssistantCommentary:
    context: EventContext
    block_id: str
    text: str


@dataclass(frozen=True)
class AssistantTextDelta:
    context: EventContext
    block_id: str
    text: str


@dataclass(frozen=True)
class ToolRequestView:
    kind: str
    subject: str
    details: tuple[str, ...] = ()


@dataclass(frozen=True)
class ToolResultView:
    summary: str
    metrics: Mapping[str, JsonScalar] = field(default_factory=dict)
    preview: tuple[str, ...] = ()
    truncated: bool = False
    output_ref: str | None = None


@dataclass(frozen=True)
class ToolRequested:
    context: EventContext
    call_id: str
    tool_name: str
    request: ToolRequestView


@dataclass(frozen=True)
class ToolStarted:
    context: EventContext
    call_id: str


@dataclass(frozen=True)
class ToolCompleted:
    context: EventContext
    call_id: str
    success: bool
    result: ToolResultView
    duration_ms: int | None = None


@dataclass(frozen=True)
class PermissionRequested:
    context: EventContext
    request: PermissionRequestViewModel


@dataclass(frozen=True)
class PermissionResolved:
    context: EventContext
    request_id: str
    call_id: str
    allowed: bool


@dataclass(frozen=True)
class PermissionModeChanged:
    context: EventContext
    mode: str


@dataclass(frozen=True)
class NoticeAdded:
    context: EventContext
    block_id: str
    level: str
    title: str
    detail: str | None = None


@dataclass(frozen=True)
class RunCompleted:
    context: EventContext
    answer_block_id: str
    answer: str
    steps: int
    duration_ms: int | None = None


@dataclass(frozen=True)
class RunFailed:
    context: EventContext
    block_id: str
    error: str
    duration_ms: int | None = None


@dataclass(frozen=True)
class RunCancelled:
    context: EventContext
    block_id: str
    duration_ms: int | None = None


PresentationEvent: TypeAlias = (
    TaskQueued
    | RunStarted
    | ModelStarted
    | AssistantCommentary
    | AssistantTextDelta
    | ToolRequested
    | ToolStarted
    | ToolCompleted
    | PermissionRequested
    | PermissionResolved
    | PermissionModeChanged
    | NoticeAdded
    | RunCompleted
    | RunFailed
    | RunCancelled
)


@dataclass(frozen=True)
class ScrollAway:
    anchor_block_id: str | None


@dataclass(frozen=True)
class FollowTail:
    pass


@dataclass(frozen=True)
class ContentArrived:
    count: int = 1


@dataclass(frozen=True)
class ToggleBlock:
    block_id: str


@dataclass(frozen=True)
class SetExpandedBlocks:
    block_ids: frozenset[str]
    expanded: bool


@dataclass(frozen=True)
class FocusBlock:
    block_id: str | None


@dataclass(frozen=True)
class ShowPermission:
    request_id: str


@dataclass(frozen=True)
class SelectPermission:
    index: int


@dataclass(frozen=True)
class HidePermission:
    pass


@dataclass(frozen=True)
class SetOverlay:
    name: str | None


@dataclass(frozen=True)
class ResizeTerminal:
    width: int


UiAction: TypeAlias = (
    ScrollAway
    | FollowTail
    | ContentArrived
    | ToggleBlock
    | SetExpandedBlocks
    | FocusBlock
    | ShowPermission
    | SelectPermission
    | HidePermission
    | SetOverlay
    | ResizeTerminal
)


def reduce_presentation(
    state: SessionPresentation,
    event: PresentationEvent,
) -> SessionPresentation:
    """Apply one normalized event to the shared presentation snapshot."""

    context = event.context
    if context.session_id != state.session_id:
        return state
    if context.sequence <= state.last_sequence:
        return state

    if isinstance(event, TaskQueued):
        if _find_turn(state, context.turn_id) is not None:
            return replace(state, last_sequence=context.sequence)
        turn = TurnViewModel(
            id=context.turn_id,
            prompt=event.prompt,
            phase=TurnPhase.QUEUED,
        )
        return replace(
            state,
            turns=state.turns + (turn,),
            queued_turn_ids=state.queued_turn_ids + (context.turn_id,),
            last_sequence=context.sequence,
        )

    if isinstance(event, PermissionModeChanged):
        return replace(
            state,
            permission_mode=event.mode,
            last_sequence=context.sequence,
        )

    turn = _find_turn(state, context.turn_id)
    if isinstance(event, RunStarted):
        if turn is None:
            turn = TurnViewModel(
                id=context.turn_id,
                prompt=event.prompt,
                phase=TurnPhase.THINKING,
                started_at_ms=context.occurred_at_ms,
            )
            turns = state.turns + (turn,)
        elif _is_terminal(turn):
            return replace(state, last_sequence=context.sequence)
        else:
            turn = replace(
                turn,
                phase=TurnPhase.THINKING,
                started_at_ms=context.occurred_at_ms,
            )
            turns = _replace_turn(state.turns, turn)
        return replace(
            state,
            turns=turns,
            active_turn_id=context.turn_id,
            queued_turn_ids=tuple(
                item for item in state.queued_turn_ids if item != context.turn_id
            ),
            last_sequence=context.sequence,
        )

    if turn is None:
        return replace(state, last_sequence=context.sequence)
    if _is_terminal(turn):
        return replace(state, last_sequence=context.sequence)

    permission_requests = state.permission_requests
    if isinstance(event, ModelStarted):
        turn = replace(turn, phase=TurnPhase.THINKING, step=event.step)
    elif isinstance(event, AssistantCommentary):
        turn = _append_text(turn, event.block_id, "commentary", event.text, False)
    elif isinstance(event, AssistantTextDelta):
        turn = _append_text(turn, event.block_id, "assistant", event.text, True)
        turn = replace(turn, phase=TurnPhase.RESPONDING)
    elif isinstance(event, ToolRequested):
        if _find_tool_block(turn, event.call_id) is None:
            tool = ToolBlockViewModel(
                id=f"tool:{event.call_id}",
                call_id=event.call_id,
                tool_name=event.tool_name,
                kind=event.request.kind,
                subject=event.request.subject,
                details=event.request.details,
                status=BlockStatus.PENDING,
            )
            turn = replace(turn, blocks=turn.blocks + (tool,))
    elif isinstance(event, ToolStarted):
        if _find_tool_block(turn, event.call_id) is None:
            return replace(state, last_sequence=context.sequence)
        turn = _update_tool(
            turn,
            event.call_id,
            status=BlockStatus.RUNNING,
            started_at_ms=context.occurred_at_ms,
        )
        turn = replace(turn, phase=TurnPhase.RUNNING_TOOLS)
    elif isinstance(event, ToolCompleted):
        if _find_tool_block(turn, event.call_id) is None:
            return replace(state, last_sequence=context.sequence)
        turn = _update_tool(
            turn,
            event.call_id,
            status=(
                BlockStatus.SUCCEEDED if event.success else BlockStatus.FAILED
            ),
            summary=event.result.summary,
            metrics=dict(event.result.metrics),
            preview=event.result.preview,
            truncated=event.result.truncated,
            output_ref=event.result.output_ref,
            duration_ms=event.duration_ms,
        )
        turn = replace(
            turn,
            phase=_active_phase(turn, permission_requests),
        )
    elif isinstance(event, PermissionRequested):
        if _find_tool_block(turn, event.request.call_id) is None:
            return replace(state, last_sequence=context.sequence)
        if not any(
            item.request_id == event.request.request_id
            for item in permission_requests
        ):
            permission_requests = permission_requests + (event.request,)
        turn = replace(turn, phase=TurnPhase.WAITING_PERMISSION)
    elif isinstance(event, PermissionResolved):
        request = next(
            (
                item
                for item in permission_requests
                if item.request_id == event.request_id
                and item.call_id == event.call_id
            ),
            None,
        )
        if request is None:
            return replace(state, last_sequence=context.sequence)
        permission_requests = tuple(
            item
            for item in permission_requests
            if item.request_id != event.request_id
        )
        if event.allowed:
            turn = _update_tool(
                turn,
                event.call_id,
                status=BlockStatus.RUNNING,
                started_at_ms=context.occurred_at_ms,
            )
        else:
            turn = _update_tool(
                turn,
                event.call_id,
                status=BlockStatus.FAILED,
                summary="Execution denied by user",
            )
        turn = replace(
            turn,
            phase=_active_phase(turn, permission_requests),
        )
    elif isinstance(event, NoticeAdded):
        if not any(block.id == event.block_id for block in turn.blocks):
            notice = NoticeBlockViewModel(
                id=event.block_id,
                level=event.level,
                title=event.title,
                detail=event.detail,
            )
            turn = replace(turn, blocks=turn.blocks + (notice,))
    elif isinstance(event, RunCompleted):
        turn = _finalize_answer(turn, event.answer_block_id, event.answer)
        turn = replace(
            turn,
            phase=TurnPhase.COMPLETED,
            step=event.steps,
            duration_ms=event.duration_ms,
        )
        permission_requests = _without_turn_permissions(
            permission_requests, turn
        )
    elif isinstance(event, RunFailed):
        turn = _stop_streaming(turn)
        if not any(block.id == event.block_id for block in turn.blocks):
            turn = replace(
                turn,
                blocks=turn.blocks
                + (
                    NoticeBlockViewModel(
                        id=event.block_id,
                        level="error",
                        title="Run failed",
                        detail=event.error,
                    ),
                ),
            )
        turn = replace(
            turn,
            phase=TurnPhase.FAILED,
            duration_ms=event.duration_ms,
        )
        permission_requests = _without_turn_permissions(
            permission_requests, turn
        )
    elif isinstance(event, RunCancelled):
        blocks: list[ViewBlock] = []
        for block in _stop_streaming(turn).blocks:
            if isinstance(block, ToolBlockViewModel) and block.status in {
                BlockStatus.PENDING,
                BlockStatus.RUNNING,
            }:
                block = replace(block, status=BlockStatus.CANCELLED)
            blocks.append(block)
        if not any(block.id == event.block_id for block in blocks):
            blocks.append(
                NoticeBlockViewModel(
                    id=event.block_id,
                    level="warning",
                    title="Run cancelled",
                )
            )
        turn = replace(
            turn,
            phase=TurnPhase.CANCELLED,
            blocks=tuple(blocks),
            duration_ms=event.duration_ms,
        )
        permission_requests = _without_turn_permissions(
            permission_requests, turn
        )

    return replace(
        state,
        turns=_replace_turn(state.turns, turn),
        permission_requests=permission_requests,
        active_turn_id=(
            None
            if state.active_turn_id == turn.id and turn.phase in {
                TurnPhase.COMPLETED,
                TurnPhase.FAILED,
                TurnPhase.CANCELLED,
            }
            else state.active_turn_id
        ),
        last_sequence=context.sequence,
    )


def reduce_terminal(
    state: TerminalViewState,
    action: UiAction,
) -> TerminalViewState:
    """Apply one client-local action without changing shared run state."""

    if isinstance(action, ScrollAway):
        return replace(
            state,
            follow_tail=False,
            anchor_block_id=action.anchor_block_id,
        )
    if isinstance(action, FollowTail):
        return replace(
            state,
            follow_tail=True,
            anchor_block_id=None,
            unseen_count=0,
        )
    if isinstance(action, ContentArrived):
        if action.count < 0:
            raise ValueError("content count cannot be negative")
        if state.follow_tail:
            return state
        return replace(state, unseen_count=max(0, state.unseen_count + action.count))
    if isinstance(action, ToggleBlock):
        expanded = set(state.expanded_block_ids)
        if action.block_id in expanded:
            expanded.remove(action.block_id)
        else:
            expanded.add(action.block_id)
        return replace(state, expanded_block_ids=frozenset(expanded))
    if isinstance(action, SetExpandedBlocks):
        expanded = set(state.expanded_block_ids)
        if action.expanded:
            expanded.update(action.block_ids)
        else:
            expanded.difference_update(action.block_ids)
        return replace(state, expanded_block_ids=frozenset(expanded))
    if isinstance(action, FocusBlock):
        return replace(state, focused_block_id=action.block_id)
    if isinstance(action, ShowPermission):
        return replace(
            state,
            active_permission_id=action.request_id,
            selected_permission_index=0,
            active_overlay="permission",
        )
    if isinstance(action, SelectPermission):
        return replace(state, selected_permission_index=max(0, action.index))
    if isinstance(action, HidePermission):
        return replace(
            state,
            active_permission_id=None,
            selected_permission_index=0,
            active_overlay=None,
        )
    if isinstance(action, SetOverlay):
        return replace(state, active_overlay=action.name)
    if isinstance(action, ResizeTerminal):
        if action.width < 1:
            raise ValueError("terminal width must be at least 1")
        return replace(state, transcript_width=action.width)
    raise TypeError(f"unsupported UI action: {type(action).__name__}")


def _append_text(
    turn: TurnViewModel,
    block_id: str,
    role: str,
    text: str,
    streaming: bool,
) -> TurnViewModel:
    blocks = list(turn.blocks)
    for index, block in enumerate(blocks):
        if block.id != block_id:
            continue
        if not isinstance(block, TextBlockViewModel):
            return turn
        blocks[index] = replace(
            block,
            text=block.text + text,
            streaming=streaming,
        )
        return replace(turn, blocks=tuple(blocks))
    blocks.append(TextBlockViewModel(block_id, role, text, streaming))
    return replace(turn, blocks=tuple(blocks))


def _finalize_answer(
    turn: TurnViewModel,
    block_id: str,
    answer: str,
) -> TurnViewModel:
    blocks = list(turn.blocks)
    for index, block in enumerate(blocks):
        if block.id == block_id and isinstance(block, TextBlockViewModel):
            blocks[index] = replace(block, text=answer, streaming=False)
            return replace(turn, blocks=tuple(blocks))
    blocks.append(TextBlockViewModel(block_id, "assistant", answer, False))
    return replace(turn, blocks=tuple(blocks))


def _stop_streaming(turn: TurnViewModel) -> TurnViewModel:
    return replace(
        turn,
        blocks=tuple(
            replace(block, streaming=False)
            if isinstance(block, TextBlockViewModel) and block.streaming
            else block
            for block in turn.blocks
        ),
    )


def _active_phase(
    turn: TurnViewModel,
    permissions: tuple[PermissionRequestViewModel, ...],
) -> TurnPhase:
    call_ids = {
        block.call_id
        for block in turn.blocks
        if isinstance(block, ToolBlockViewModel)
    }
    if any(request.call_id in call_ids for request in permissions):
        return TurnPhase.WAITING_PERMISSION
    if any(
        isinstance(block, ToolBlockViewModel)
        and block.status in {BlockStatus.PENDING, BlockStatus.RUNNING}
        for block in turn.blocks
    ):
        return TurnPhase.RUNNING_TOOLS
    if any(
        isinstance(block, TextBlockViewModel) and block.streaming
        for block in turn.blocks
    ):
        return TurnPhase.RESPONDING
    return TurnPhase.THINKING


def _find_turn(
    state: SessionPresentation,
    turn_id: str,
) -> TurnViewModel | None:
    return next((turn for turn in state.turns if turn.id == turn_id), None)


def _is_terminal(turn: TurnViewModel) -> bool:
    return turn.phase in {
        TurnPhase.COMPLETED,
        TurnPhase.FAILED,
        TurnPhase.CANCELLED,
    }


def _replace_turn(
    turns: tuple[TurnViewModel, ...],
    replacement: TurnViewModel,
) -> tuple[TurnViewModel, ...]:
    return tuple(
        replacement if turn.id == replacement.id else turn for turn in turns
    )


def _find_tool_block(
    turn: TurnViewModel,
    call_id: str,
) -> ToolBlockViewModel | None:
    return next(
        (
            block
            for block in turn.blocks
            if isinstance(block, ToolBlockViewModel) and block.call_id == call_id
        ),
        None,
    )


def _update_tool(
    turn: TurnViewModel,
    call_id: str,
    **changes: Any,
) -> TurnViewModel:
    blocks = list(turn.blocks)
    for index, block in enumerate(blocks):
        if isinstance(block, ToolBlockViewModel) and block.call_id == call_id:
            blocks[index] = replace(block, **changes)
            return replace(turn, blocks=tuple(blocks))
    return turn


def _without_turn_permissions(
    permissions: tuple[PermissionRequestViewModel, ...],
    turn: TurnViewModel,
) -> tuple[PermissionRequestViewModel, ...]:
    call_ids = {
        block.call_id
        for block in turn.blocks
        if isinstance(block, ToolBlockViewModel)
    }
    return tuple(
        request for request in permissions if request.call_id not in call_ids
    )


def _turn_to_dict(turn: TurnViewModel) -> dict[str, Any]:
    return {
        "id": turn.id,
        "prompt": turn.prompt,
        "phase": turn.phase.value,
        "blocks": [_block_to_dict(block) for block in turn.blocks],
        "step": turn.step,
        "started_at_ms": turn.started_at_ms,
        "duration_ms": turn.duration_ms,
    }


def _turn_from_dict(value: Mapping[str, Any]) -> TurnViewModel:
    return TurnViewModel(
        id=str(value["id"]),
        prompt=str(value["prompt"]),
        phase=TurnPhase(str(value["phase"])),
        blocks=tuple(_block_from_dict(item) for item in value.get("blocks", ())),
        step=int(value.get("step", 0)),
        started_at_ms=_optional_int(value.get("started_at_ms")),
        duration_ms=_optional_int(value.get("duration_ms")),
    )


def _block_to_dict(block: ViewBlock) -> dict[str, Any]:
    if isinstance(block, TextBlockViewModel):
        return {
            "type": "text",
            "id": block.id,
            "role": block.role,
            "text": block.text,
            "streaming": block.streaming,
        }
    if isinstance(block, ToolBlockViewModel):
        return {
            "type": "tool",
            "id": block.id,
            "call_id": block.call_id,
            "tool_name": block.tool_name,
            "kind": block.kind,
            "subject": block.subject,
            "details": list(block.details),
            "status": block.status.value,
            "summary": block.summary,
            "metrics": dict(block.metrics),
            "preview": list(block.preview),
            "truncated": block.truncated,
            "output_ref": block.output_ref,
            "started_at_ms": block.started_at_ms,
            "duration_ms": block.duration_ms,
        }
    return {
        "type": "notice",
        "id": block.id,
        "level": block.level,
        "title": block.title,
        "detail": block.detail,
    }


def _block_from_dict(value: Mapping[str, Any]) -> ViewBlock:
    block_type = value.get("type")
    if block_type == "text":
        return TextBlockViewModel(
            id=str(value["id"]),
            role=str(value["role"]),
            text=str(value.get("text", "")),
            streaming=bool(value.get("streaming", False)),
        )
    if block_type == "tool":
        metrics = value.get("metrics", {})
        if not isinstance(metrics, Mapping):
            raise ValueError("tool block metrics must be an object")
        return ToolBlockViewModel(
            id=str(value["id"]),
            call_id=str(value["call_id"]),
            tool_name=str(value["tool_name"]),
            kind=str(value.get("kind", "generic")),
            subject=str(value.get("subject", "")),
            details=tuple(str(item) for item in value.get("details", ())),
            status=BlockStatus(str(value["status"])),
            summary=_optional_string(value.get("summary")),
            metrics=dict(metrics),
            preview=tuple(str(item) for item in value.get("preview", ())),
            truncated=bool(value.get("truncated", False)),
            output_ref=_optional_string(value.get("output_ref")),
            started_at_ms=_optional_int(value.get("started_at_ms")),
            duration_ms=_optional_int(value.get("duration_ms")),
        )
    if block_type == "notice":
        return NoticeBlockViewModel(
            id=str(value["id"]),
            level=str(value["level"]),
            title=str(value["title"]),
            detail=_optional_string(value.get("detail")),
        )
    raise ValueError(f"unknown presentation block type: {block_type!r}")


def _permission_to_dict(
    request: PermissionRequestViewModel,
) -> dict[str, Any]:
    return {
        "request_id": request.request_id,
        "call_id": request.call_id,
        "kind": request.kind,
        "title": request.title,
        "risk": request.risk,
        "preview": list(request.preview),
        "choices": [
            {
                "id": choice.id,
                "label": choice.label,
                "description": choice.description,
            }
            for choice in request.choices
        ],
    }


def _permission_from_dict(
    value: Mapping[str, Any],
) -> PermissionRequestViewModel:
    return PermissionRequestViewModel(
        request_id=str(value["request_id"]),
        call_id=str(value["call_id"]),
        kind=str(value.get("kind", "generic")),
        title=str(value["title"]),
        risk=_optional_string(value.get("risk")),
        preview=tuple(str(item) for item in value.get("preview", ())),
        choices=tuple(
            PermissionChoiceViewModel(
                id=str(item["id"]),
                label=str(item["label"]),
                description=str(item.get("description", "")),
            )
            for item in value.get("choices", ())
        ),
    )


def _optional_string(value: Any) -> str | None:
    return None if value is None else str(value)


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)
