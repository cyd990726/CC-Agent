import unittest
from io import StringIO

from prompt_toolkit.document import Document
from rich.console import Console

from agent.events import AgentEvent, EventType
from ui.permissions import SessionPermissionHandler
from ui.renderer import TerminalRenderer
from ui.terminal import COMMANDS, SlashCommandCompleter


class SlashCommandCompleterTests(unittest.TestCase):
    def test_slash_opens_all_command_suggestions(self) -> None:
        completions = list(
            SlashCommandCompleter().get_completions(Document("/"), None)
        )

        self.assertEqual(
            [completion.text for completion in completions],
            [command for command, _description in COMMANDS],
        )

    def test_partial_command_filters_and_replaces_current_token(self) -> None:
        completions = list(
            SlashCommandCompleter().get_completions(Document("/st"), None)
        )

        self.assertEqual(len(completions), 1)
        self.assertEqual(completions[0].text, "/status")
        self.assertEqual(completions[0].start_position, -3)

    def test_regular_prompt_does_not_show_commands(self) -> None:
        completions = list(
            SlashCommandCompleter().get_completions(Document("fix tests"), None)
        )

        self.assertEqual(completions, [])


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

    def test_edit_file_requires_permission(self) -> None:
        prompts: list[str] = []
        handler = SessionPermissionHandler(
            self.console,
            ask=lambda prompt: prompts.append(prompt) or "n",
        )

        allowed = handler(
            "edit_file",
            {"path": "a.py", "old_text": "before", "new_text": "after"},
        )

        self.assertFalse(allowed)
        self.assertEqual(len(prompts), 1)

    def test_network_access_requires_permission(self) -> None:
        prompts: list[str] = []
        handler = SessionPermissionHandler(
            self.console,
            ask=lambda prompt: prompts.append(prompt) or "n",
        )

        self.assertFalse(handler("fetch_url", {"url": "https://example.com"}))
        self.assertEqual(len(prompts), 1)


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
        renderer(
            AgentEvent(EventType.RUN_COMPLETED, {"answer": "第一行\n第二行"})
        )

        rendered = output.getvalue()
        self.assertIn("第一行", rendered)
        self.assertIn("第二行", rendered)
        self.assertNotIn('"final_answer"', rendered)

    def test_shell_output_is_compact_by_default(self) -> None:
        output = StringIO()
        renderer = TerminalRenderer(
            Console(file=output, force_terminal=False), output_lines=2
        )
        shell_output = '{"exit_code":0,"stdout":"one\\ntwo\\nthree","stderr":""}'

        renderer(
            AgentEvent(
                EventType.TOOL_COMPLETED,
                {
                    "result": {
                        "tool": "shell",
                        "success": True,
                        "output": shell_output,
                    }
                },
            )
        )

        rendered = output.getvalue()
        self.assertIn("Exited with code 0", rendered)
        self.assertIn("one", rendered)
        self.assertIn("1 more line", rendered)
        self.assertNotIn("three", rendered)

    def test_read_result_shows_summary_instead_of_file_contents(self) -> None:
        output = StringIO()
        renderer = TerminalRenderer(Console(file=output, force_terminal=False))

        renderer(
            AgentEvent(
                EventType.TOOL_COMPLETED,
                {
                    "result": {
                        "tool": "read_file",
                        "success": True,
                        "output": "secret contents\nsecond line",
                    }
                },
            )
        )

        rendered = output.getvalue()
        self.assertIn("Read 2 lines", rendered)
        self.assertNotIn("secret contents", rendered)


if __name__ == "__main__":
    unittest.main()
