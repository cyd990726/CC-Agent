"""The minimal model -> action -> observation agent loop."""

import json
from collections.abc import Callable, Mapping
from typing import Any

from agent.events import AgentEvent, EventType
from agent.cancellation import CancellationToken, RunCancelled, check_cancelled
from agent.permissions import PermissionMode
from agent.prompt import build_system_prompt
from agent.state import AgentState
from model.llm import LLM, ModelProtocolError
from tools.base import READ_ONLY_TOOL_NAMES, ToolExecutor


# 智能体运行时错误的基类
class AgentRuntimeError(RuntimeError):
    """Raised when the model violates the runtime protocol."""


# 目前只实现一个「超过最大步数」的错误
class MaxStepsExceeded(AgentRuntimeError):
    """Raised when a run does not finish within its configured step budget."""

    def __init__(self, state: AgentState):
        self.state = state
        super().__init__(f"agent did not finish within {state.steps} steps")


class AgentRuntime:
    """Coordinate an LLM and a collection of tools until the task is done."""

    protocol_retry_limit = 2

    def __init__(
        self,
        model: LLM,
        tools: ToolExecutor,
        *,
        max_steps: int = 20,
        plan_mode: bool = False,
        permission_mode: PermissionMode = PermissionMode.ASK,
        protocol_retries: int = 2,
    ) -> None:
        if max_steps < 1:
            raise ValueError("max_steps must be at least 1")
        if protocol_retries < 0:
            raise ValueError("protocol_retries cannot be negative")
        self.model = model
        self.tools = tools
        self.max_steps = max_steps
        self.protocol_retry_limit = protocol_retries
        self.plan_mode = False
        self.set_plan_mode(plan_mode)
        self.permission_mode = PermissionMode.ASK
        self.set_permission_mode(permission_mode)

    def set_plan_mode(self, enabled: bool) -> None:
        """Enable read-only planning or restore the full tool set."""

        self.plan_mode = enabled
        self.tools.set_allowed_tools(READ_ONLY_TOOL_NAMES if enabled else None)

    def set_permission_mode(self, mode: PermissionMode) -> None:
        """Change approval behavior and workspace access for future tool calls."""

        self.permission_mode = mode
        self.tools.set_permission_mode(mode)

    def run(
        self,
        task: str,
        *,
        on_event: Callable[[AgentEvent], None] | None = None,
        cancellation: CancellationToken | None = None,
    ) -> AgentState:
        token = cancellation or CancellationToken()
        try:
            with token.bind():
                return self._run(task, on_event=on_event)
        except RunCancelled:
            self._emit(on_event, EventType.RUN_CANCELLED)
            raise

    def _run(
        self,
        task: str,
        *,
        on_event: Callable[[AgentEvent], None] | None = None,
    ) -> AgentState:
        task = task.strip()
        if not task:
            raise ValueError("task cannot be empty")

        state = AgentState(current_task=task)
        state.add_message(
            "system",
            build_system_prompt(
                self.tools.describe(),
                plan_mode=self.plan_mode,
                permission_mode=self.permission_mode,
            ),
        )
        state.add_message("user", task)
        self._emit(on_event, EventType.RUN_STARTED, task=task)
        protocol_errors = 0

        try:
            for _ in range(self.max_steps):
                check_cancelled()
                self._emit(
                    on_event,
                    EventType.MODEL_STARTED,
                    step=state.steps + 1,
                )
                try:
                    response = self.model.stream_chat(
                        state.messages,
                        on_delta=lambda delta: self._emit(
                            on_event,
                            EventType.MODEL_DELTA,
                            delta=delta,
                        ),
                    )
                    check_cancelled()
                    state.steps += 1
                    response = self._normalize_response(response)
                    self._record_model_response(state, response)
                    self._emit(
                        on_event,
                        EventType.MODEL_COMPLETED,
                        step=state.steps,
                        response=dict(response),
                    )
                except ModelProtocolError as exc:
                    state.steps += 1
                    protocol_errors += 1
                    if protocol_errors > self.protocol_retry_limit:
                        raise AgentRuntimeError(
                            "model failed to follow the JSON action protocol "
                            f"after {self.protocol_retry_limit} retries: {exc}"
                        ) from exc
                    self._emit(
                        on_event,
                        EventType.MODEL_COMPLETED,
                        step=state.steps,
                        response={"protocol_error": str(exc)},
                    )
                    state.add_message(
                        "user",
                        self._protocol_error_observation(
                            exc,
                            attempts_remaining=(
                                self.protocol_retry_limit - protocol_errors
                            ),
                        ),
                    )
                    self._emit_protocol_error(on_event, str(exc))
                    continue

                has_action = "action" in response and response["action"] is not None
                has_answer = "final_answer" in response
                if has_action == has_answer:
                    if self._record_protocol_error(
                        state,
                        on_event,
                        "model response must contain exactly one of "
                        "action or final_answer",
                        protocol_errors,
                    ):
                        protocol_errors += 1
                        continue
                    raise AgentRuntimeError(
                        "model failed to follow the JSON action protocol "
                        f"after {self.protocol_retry_limit} retries: "
                        "model response must contain exactly one of "
                        "action or final_answer"
                    )

                if has_answer:
                    answer = response["final_answer"]
                    if not isinstance(answer, str) or not answer.strip():
                        if self._record_protocol_error(
                            state,
                            on_event,
                            "final_answer must be a non-empty string",
                            protocol_errors,
                        ):
                            protocol_errors += 1
                            continue
                        raise AgentRuntimeError(
                            "model failed to follow the JSON action protocol "
                            f"after {self.protocol_retry_limit} retries: "
                            "final_answer must be a non-empty string"
                        )
                    state.finished = True
                    state.final_answer = answer.strip()
                    self._emit(
                        on_event,
                        EventType.RUN_COMPLETED,
                        answer=state.final_answer,
                        steps=state.steps,
                    )
                    return state

                try:
                    tool_name, args = self._parse_action(response["action"])
                except AgentRuntimeError as exc:
                    if self._record_protocol_error(
                        state,
                        on_event,
                        str(exc),
                        protocol_errors,
                    ):
                        protocol_errors += 1
                        continue
                    raise AgentRuntimeError(
                        "model failed to follow the JSON action protocol "
                        f"after {self.protocol_retry_limit} retries: {exc}"
                    ) from exc
                protocol_errors = 0
                event_args = dict(args)
                self._emit(
                    on_event,
                    EventType.TOOL_REQUESTED,
                    tool=tool_name,
                    args=event_args,
                )
                self._emit(
                    on_event,
                    EventType.TOOL_STARTED,
                    tool=tool_name,
                    args=event_args,
                )
                result = self.tools.execute(tool_name, args)
                check_cancelled()
                state.tool_results.append(result)
                self._emit(
                    on_event,
                    EventType.TOOL_COMPLETED,
                    result=result.as_dict(),
                )
                state.add_message(
                    "user",
                    "Observation:\n"
                    + json.dumps(
                        result.as_dict(), ensure_ascii=False, sort_keys=True
                    ),
                )

            raise MaxStepsExceeded(state)
        except RunCancelled:
            raise
        except Exception as exc:
            self._emit(
                on_event,
                EventType.RUN_FAILED,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise

    @staticmethod
    def _emit(
        handler: Callable[[AgentEvent], None] | None,
        event_type: EventType,
        **data: Any,
    ) -> None:
        if handler is not None:
            handler(AgentEvent(event_type, data))

    def _record_protocol_error(
        self,
        state: AgentState,
        on_event: Callable[[AgentEvent], None] | None,
        message: str,
        protocol_errors: int,
    ) -> bool:
        attempts_remaining = self.protocol_retry_limit - protocol_errors - 1
        if attempts_remaining < 0:
            return False
        state.add_message(
            "user",
            self._protocol_error_observation(
                message,
                attempts_remaining=attempts_remaining,
            ),
        )
        self._emit(
            on_event,
            EventType.TOOL_COMPLETED,
            result=self._protocol_error_result(message),
        )
        return True

    def _emit_protocol_error(
        self,
        on_event: Callable[[AgentEvent], None] | None,
        message: str,
    ) -> None:
        self._emit(
            on_event,
            EventType.TOOL_COMPLETED,
            result=self._protocol_error_result(message),
        )

    @staticmethod
    def _protocol_error_result(message: str) -> dict[str, Any]:
        return {
            "tool": "model_protocol",
            "args": {},
            "success": False,
            "output": message,
        }

    @staticmethod
    def _protocol_error_observation(
        error: Exception | str,
        *,
        attempts_remaining: int,
    ) -> str:
        return (
            "Observation:\n"
            + json.dumps(
                {
                    "tool": "model_protocol",
                    "args": {},
                    "success": False,
                    "output": (
                        f"{error}. Reply with exactly one JSON object matching "
                        "the action protocol. "
                        f"{attempts_remaining} protocol retries remaining."
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )

    @staticmethod
    def _record_model_response(
        state: AgentState, response: Mapping[str, Any]
    ) -> None:
        if not isinstance(response, Mapping):
            raise AgentRuntimeError("model response must be a JSON object")
        try:
            content = json.dumps(dict(response), ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise AgentRuntimeError("model response is not JSON serializable") from exc
        state.add_message("assistant", content)

    @staticmethod
    def _normalize_response(response: Mapping[str, Any]) -> Mapping[str, Any]:
        """Accept common final-answer variants without treating them as tools."""

        action = response.get("action")
        if not isinstance(action, Mapping):
            return response
        tool_name = action.get("tool")
        if tool_name != "final_answer":
            return response
        args = action.get("args", {})
        if not isinstance(args, Mapping):
            return response
        answer = args.get("answer", args.get("final_answer"))
        if answer is None:
            return response
        normalized = dict(response)
        normalized.pop("action", None)
        normalized["final_answer"] = answer
        return normalized

    @staticmethod
    def _parse_action(action: Any) -> tuple[str, Mapping[str, Any]]:
        if not isinstance(action, Mapping):
            raise AgentRuntimeError("action must be an object")
        tool_name = action.get("tool")
        args = action.get("args", {})
        if not isinstance(tool_name, str) or not tool_name.strip():
            raise AgentRuntimeError("action.tool must be a non-empty string")
        if not isinstance(args, Mapping):
            raise AgentRuntimeError("action.args must be an object")
        return tool_name, args
