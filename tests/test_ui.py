import unittest
from io import StringIO

from rich.console import Console

from agent.events import AgentEvent, EventType
from ui.permissions import SessionPermissionHandler
from ui.renderer import TerminalRenderer


class PermissionHandlerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.output = StringIO()
        self.console = Console(file=self.output, force_terminal=False)

    def test_safe_tool_is_allowed_without_prompt(self) -> None:
        prompts: list[str] = []
        handler = SessionPermissionHandler(
            self.console,
            ask=lambda prompt: prompts.append(prompt) or "n",
        )

        self.assertTrue(handler("read_file", {"path": "README.md"}))
        self.assertEqual(prompts, [])

    def test_always_allow_is_remembered_for_session(self) -> None:
        answers = iter(["a"])
        handler = SessionPermissionHandler(
            self.console,
            ask=lambda _prompt: next(answers),
        )

        self.assertTrue(handler("shell", {"command": "pwd"}))
        self.assertTrue(handler("shell", {"command": "ls"}))
        self.assertIn("shell", handler.allowed_for_session)

    def test_denies_sensitive_tool(self) -> None:
        handler = SessionPermissionHandler(
            self.console,
            ask=lambda _prompt: "n",
        )

        self.assertFalse(handler("write_file", {"path": "a.py", "content": "x"}))


class RendererTests(unittest.TestCase):
    def test_toggle_verbose(self) -> None:
        renderer = TerminalRenderer(
            Console(file=StringIO(), force_terminal=False), verbose=False
        )

        self.assertTrue(renderer.toggle_verbose())
        self.assertFalse(renderer.toggle_verbose())

    def test_streams_only_final_answer_text(self) -> None:
        output = StringIO()
        renderer = TerminalRenderer(Console(file=output, force_terminal=False))

        renderer(AgentEvent(EventType.MODEL_STARTED, {"step": 1}))
        renderer(
            AgentEvent(
                EventType.MODEL_DELTA,
                {"delta": '{"thought":"done","final_'},
            )
        )
        renderer(
            AgentEvent(
                EventType.MODEL_DELTA,
                {"delta": 'answer":"第一行\\n第二行"}'},
            )
        )
        renderer(AgentEvent(EventType.RUN_COMPLETED, {"answer": "第一行\n第二行"}))

        rendered = output.getvalue()
        self.assertIn("第一行\n第二行", rendered)
        self.assertNotIn('"final_answer"', rendered)


if __name__ == "__main__":
    unittest.main()
