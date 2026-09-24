import json
import unittest
from collections.abc import Mapping, Sequence
from typing import Any

from agent.events import AgentEvent, EventType
from agent.runtime import AgentRuntime, AgentRuntimeError, MaxStepsExceeded
from model.llm import LLM, ModelProtocolError
from tools.base import Tool, ToolExecutor


class QueueLLM(LLM):
    def __init__(self, responses: list[Mapping[str, Any]]) -> None:
        self.responses = iter(responses)
        self.calls: list[list[Mapping[str, str]]] = []

    def chat(self, messages: Sequence[Mapping[str, str]]) -> Mapping[str, Any]:
        self.calls.append(list(messages))
        return next(self.responses)


class StreamingQueueLLM(QueueLLM):
    def stream_chat(self, messages, on_delta=None):
        response = self.chat(messages)
        if on_delta is not None:
            on_delta('{"final_answer":"')
            on_delta(str(response["final_answer"]) + '"}')
        return response


class ProtocolErrorThenQueueLLM(QueueLLM):
    def __init__(self, responses: list[Mapping[str, Any]]) -> None:
        super().__init__(responses)
        self.failures_remaining = 1

    def stream_chat(self, messages, on_delta=None):
        self.calls.append(list(messages))
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise ModelProtocolError("model did not return valid JSON")
        return next(self.responses)


class EchoTool(Tool):
    name = "echo"
    description = "Echo text."
    args_schema = {"text": "string"}

    def run(self, args: Mapping[str, Any]) -> str:
        return str(args["text"])


class RuntimeTests(unittest.TestCase):
    def test_action_observation_then_final_answer(self) -> None:
        model = QueueLLM(
            [
                {"action": {"tool": "echo", "args": {"text": "hello"}}},
                {"final_answer": "done"},
            ]
        )
        runtime = AgentRuntime(model, ToolExecutor([EchoTool()]))

        state = runtime.run("test task")

        self.assertTrue(state.finished)
        self.assertEqual(state.final_answer, "done")
        self.assertEqual(state.steps, 2)
        self.assertEqual(state.tool_results[0].output, "hello")
        observation = model.calls[1][-1]["content"]
        self.assertTrue(observation.startswith("Observation:"))
        self.assertTrue(json.loads(observation.split("\n", 1)[1])["success"])

    def test_unknown_tool_is_returned_as_recoverable_observation(self) -> None:
        model = QueueLLM(
            [
                {"action": {"tool": "missing", "args": {}}},
                {"final_answer": "could not use that tool"},
            ]
        )

        state = AgentRuntime(model, ToolExecutor([])).run("test task")

        self.assertFalse(state.tool_results[0].success)
        self.assertIn("unknown tool", state.tool_results[0].output)

    def test_final_answer_tool_shape_is_normalized(self) -> None:
        model = QueueLLM(
            [
                {
                    "action": {
                        "tool": "final_answer",
                        "args": {"answer": "done"},
                    }
                }
            ]
        )

        state = AgentRuntime(model, ToolExecutor([])).run("test task")

        self.assertTrue(state.finished)
        self.assertEqual(state.final_answer, "done")
        self.assertEqual(state.tool_results, [])

    def test_model_protocol_error_is_returned_as_recoverable_observation(
        self,
    ) -> None:
        model = ProtocolErrorThenQueueLLM([{"final_answer": "recovered"}])

        state = AgentRuntime(model, ToolExecutor([])).run("test task")

        self.assertTrue(state.finished)
        self.assertEqual(state.final_answer, "recovered")
        self.assertEqual(state.steps, 2)
        observation = model.calls[1][-1]["content"]
        self.assertTrue(observation.startswith("Observation:"))
        payload = json.loads(observation.split("\n", 1)[1])
        self.assertEqual(payload["tool"], "model_protocol")
        self.assertFalse(payload["success"])
        self.assertIn("1 protocol retries remaining", payload["output"])

    def test_invalid_action_shape_can_be_retried(self) -> None:
        model = QueueLLM(
            [
                {"action": "bad"},
                {"action": {"tool": "echo", "args": {"text": "hello"}}},
                {"final_answer": "done"},
            ]
        )

        state = AgentRuntime(model, ToolExecutor([EchoTool()])).run("test task")

        self.assertTrue(state.finished)
        self.assertEqual(state.final_answer, "done")
        self.assertEqual(state.tool_results[0].output, "hello")
        observation = model.calls[1][-1]["content"]
        payload = json.loads(observation.split("\n", 1)[1])
        self.assertIn("action must be an object", payload["output"])

    def test_model_protocol_errors_fail_after_two_retries(self) -> None:
        model = QueueLLM(
            [
                {"action": "bad"},
                {"action": {"tool": ""}},
                {"final_answer": ""},
            ]
        )
        runtime = AgentRuntime(model, ToolExecutor([]), max_steps=5)

        with self.assertRaises(AgentRuntimeError) as caught:
            runtime.run("test task")

        self.assertIn("after 2 retries", str(caught.exception))

    def test_step_limit_preserves_state(self) -> None:
        model = QueueLLM([{"action": {"tool": "echo", "args": {"text": "x"}}}])
        runtime = AgentRuntime(model, ToolExecutor([EchoTool()]), max_steps=1)

        with self.assertRaises(MaxStepsExceeded) as caught:
            runtime.run("test task")

        self.assertEqual(caught.exception.state.steps, 1)
        self.assertFalse(caught.exception.state.finished)

    def test_emits_events_for_complete_tool_cycle(self) -> None:
        model = QueueLLM(
            [
                {"action": {"tool": "echo", "args": {"text": "hello"}}},
                {"final_answer": "done"},
            ]
        )
        events: list[AgentEvent] = []

        AgentRuntime(model, ToolExecutor([EchoTool()])).run(
            "test task", on_event=events.append
        )

        self.assertEqual(
            [event.type for event in events],
            [
                EventType.RUN_STARTED,
                EventType.MODEL_STARTED,
                EventType.MODEL_COMPLETED,
                EventType.TOOL_REQUESTED,
                EventType.TOOL_STARTED,
                EventType.TOOL_COMPLETED,
                EventType.MODEL_STARTED,
                EventType.MODEL_COMPLETED,
                EventType.RUN_COMPLETED,
            ],
        )
        self.assertEqual(events[-1].data["answer"], "done")

    def test_forwards_streaming_model_deltas_as_events(self) -> None:
        events: list[AgentEvent] = []

        AgentRuntime(
            StreamingQueueLLM([{"final_answer": "done"}]), ToolExecutor([])
        ).run("test task", on_event=events.append)

        deltas = [
            event.data["delta"]
            for event in events
            if event.type is EventType.MODEL_DELTA
        ]
        self.assertEqual(deltas, ['{"final_answer":"', 'done"}'])

    def test_emits_failure_event_when_step_limit_is_exceeded(self) -> None:
        events: list[AgentEvent] = []
        runtime = AgentRuntime(
            QueueLLM([{"action": {"tool": "echo", "args": {"text": "x"}}}]),
            ToolExecutor([EchoTool()]),
            max_steps=1,
        )

        with self.assertRaises(MaxStepsExceeded):
            runtime.run("test task", on_event=events.append)

        self.assertEqual(events[-1].type, EventType.RUN_FAILED)


if __name__ == "__main__":
    unittest.main()
