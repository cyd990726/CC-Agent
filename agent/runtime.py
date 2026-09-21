"""The minimal model -> action -> observation agent loop."""

import json
from collections.abc import Callable, Mapping
from typing import Any

from agent.events import AgentEvent, EventType
from agent.prompt import build_system_prompt
from agent.state import AgentState
from model.llm import LLM
from tools.base import ToolExecutor


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

    def __init__(
        self,
        model: LLM,
        tools: ToolExecutor,
        *,
        max_steps: int = 20,
    ) -> None:
        if max_steps < 1:
            raise ValueError("max_steps must be at least 1")
        self.model = model
        self.tools = tools
        self.max_steps = max_steps

    def run(
        self,
        task: str,
        *,
        on_event: Callable[[AgentEvent], None] | None = None,
    ) -> AgentState:
        task = task.strip()
        if not task:
            raise ValueError("task cannot be empty")

        state = AgentState(current_task=task)
        state.add_message("system", build_system_prompt(self.tools.describe()))
        state.add_message("user", task)
        self._emit(on_event, EventType.RUN_STARTED, task=task)

        try:
            for _ in range(self.max_steps):
                self._emit(
                    on_event,
                    EventType.MODEL_STARTED,
                    step=state.steps + 1,
                )
                response = self.model.chat(state.messages)
                state.steps += 1
                self._record_model_response(state, response)
                self._emit(
                    on_event,
                    EventType.MODEL_COMPLETED,
                    step=state.steps,
                    response=dict(response),
                )

                has_action = "action" in response and response["action"] is not None
                has_answer = "final_answer" in response
                if has_action == has_answer:
                    raise AgentRuntimeError(
                        "model response must contain exactly one of "
                        "action or final_answer"
                    )

                if has_answer:
                    answer = response["final_answer"]
                    if not isinstance(answer, str) or not answer.strip():
                        raise AgentRuntimeError(
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

                tool_name, args = self._parse_action(response["action"])
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
