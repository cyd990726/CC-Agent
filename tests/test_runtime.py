import json
import unittest
from collections.abc import Mapping, Sequence
from typing import Any

from agent.runtime import AgentRuntime, MaxStepsExceeded
from model.llm import LLM
from tools.base import Tool, ToolExecutor


class QueueLLM(LLM):
    def __init__(self, responses: list[Mapping[str, Any]]) -> None:
        self.responses = iter(responses)
        self.calls: list[list[Mapping[str, str]]] = []

    def chat(self, messages: Sequence[Mapping[str, str]]) -> Mapping[str, Any]:
        self.calls.append(list(messages))
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

    def test_step_limit_preserves_state(self) -> None:
        model = QueueLLM([{"action": {"tool": "echo", "args": {"text": "x"}}}])
        runtime = AgentRuntime(model, ToolExecutor([EchoTool()]), max_steps=1)

        with self.assertRaises(MaxStepsExceeded) as caught:
            runtime.run("test task")

        self.assertEqual(caught.exception.state.steps, 1)
        self.assertFalse(caught.exception.state.finished)


if __name__ == "__main__":
    unittest.main()
