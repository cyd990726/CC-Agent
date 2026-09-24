import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import Mock

from prompt_toolkit.document import Document
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from agent.events import AgentEvent, EventType
from agent.runtime import AgentRuntime
from ui.logo import LOGO_LINES, render_logo
from ui.permissions import SessionPermissionHandler
from ui.renderer import TerminalRenderer
from ui.terminal import COMMANDS, SlashCommandCompleter, TerminalApp


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


class TerminalAppTests(unittest.TestCase):
    def test_logo_has_stable_dimensions_and_text_fallback(self) -> None:
        self.assertEqual(len(LOGO_LINES), 3)

        rendered = render_logo(color=False).plain

        self.assertEqual(rendered.splitlines(), list(LOGO_LINES))
        self.assertIn("o.o", rendered)

    def test_status_line_contains_model_and_workspace(self) -> None:
        output = StringIO()
        console = Console(file=output, force_terminal=False)
        workspace = Path("/tmp/example")
        app = TerminalApp(
            Mock(spec=AgentRuntime),
            TerminalRenderer(console),
            console,
            model_name="test-model",
            workspace=workspace,
            prompt=lambda _message: "/exit",
        )

        fragments = app._status_fragments()
        rendered = "".join(text for _style, text in fragments)

        self.assertIn("test-model", rendered)
        expected_path = str(workspace)
        self.assertIn(expected_path, rendered)
        model_style = next(style for style, text in fragments if text == "test-model")
        path_style = next(style for style, text in fragments if text == expected_path)
        self.assertNotEqual(model_style, path_style)

    def test_input_height_counts_wrapped_and_explicit_lines(self) -> None:
        measure = TerminalApp._measure_input_height

        self.assertEqual(measure("short", 10, 24), 1)
        self.assertEqual(measure("1234567", 10, 24), 2)
        self.assertEqual(measure("one\ntwo", 10, 24), 2)
        self.assertEqual(measure("中文中文", 10, 24), 2)

    def test_completion_panel_padding_uses_display_width(self) -> None:
        padded = TerminalApp._fit_cells("/status 查看当前配置", 20)

        from prompt_toolkit.utils import get_cwidth

        self.assertEqual(get_cwidth(padded), 20)


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

    def test_permission_card_shows_requested_operation(self) -> None:
        handler = SessionPermissionHandler(
            self.console,
            ask=lambda _prompt: "",
        )

        self.assertFalse(handler("shell", {"command": "python -m unittest"}))
        rendered = self.output.getvalue()
        self.assertIn("权限确认", rendered)
        self.assertIn("执行命令", rendered)
        self.assertIn("python -m unittest", rendered)

    def test_permission_selector_supports_arrow_keys(self) -> None:
        handler = SessionPermissionHandler(self.console)
        with create_pipe_input() as pipe_input:
            pipe_input.send_text("\x1b[C\r")

            answer = handler._select_permission(
                input=pipe_input,
                output=DummyOutput(),
            )

        self.assertEqual(answer, "y")

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
    class FakeLive:
        instances: list["RendererTests.FakeLive"] = []

        def __init__(self, renderable, **kwargs) -> None:
            self.renderable = renderable
            self.kwargs = kwargs
            self.started = False
            self.stopped = False
            self.updates = []
            self.__class__.instances.append(self)

        def start(self) -> None:
            self.started = True

        def stop(self) -> None:
            self.stopped = True

        def update(self, renderable, *, refresh: bool = False) -> None:
            self.renderable = renderable
            self.updates.append((renderable, refresh))

    def setUp(self) -> None:
        self.FakeLive.instances = []

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

    def test_streaming_answer_does_not_repeat_previous_text(self) -> None:
        output = StringIO()
        renderer = TerminalRenderer(Console(file=output, force_terminal=False))

        renderer(AgentEvent(EventType.MODEL_STARTED, {"step": 1}))
        renderer(
            AgentEvent(
                EventType.MODEL_DELTA,
                {"delta": '{"final_answer":"项目当前状态'},
            )
        )
        renderer(
            AgentEvent(
                EventType.MODEL_DELTA,
                {"delta": '\\n你在分支上"}'},
            )
        )
        renderer(
            AgentEvent(
                EventType.RUN_COMPLETED,
                {"answer": "项目当前状态\n你在分支上", "steps": 1},
            )
        )

        rendered = output.getvalue()
        self.assertEqual(rendered.count("项目当前状态"), 1)
        self.assertEqual(rendered.count("你在分支上"), 1)

    def test_streaming_answer_is_rendered_as_markdown_once_on_completion(self) -> None:
        output = StringIO()
        renderer = TerminalRenderer(Console(file=output, force_terminal=False))

        renderer(AgentEvent(EventType.MODEL_STARTED, {"step": 1}))
        renderer(
            AgentEvent(
                EventType.MODEL_DELTA,
                {"delta": '{"final_answer":"### 标题\\n- 条目"}'},
            )
        )
        renderer(
            AgentEvent(
                EventType.RUN_COMPLETED,
                {"answer": "### 标题\n- 条目", "steps": 1},
            )
        )

        rendered = output.getvalue()
        self.assertEqual(rendered.count("标题"), 1)
        self.assertEqual(rendered.count("条目"), 1)
        self.assertNotIn("### 标题", rendered)

    def test_streaming_preview_waits_for_complete_line(self) -> None:
        output = StringIO()
        renderer = TerminalRenderer(
            Console(file=output, force_terminal=True),
            live_factory=self.FakeLive,
        )

        renderer(
            AgentEvent(
                EventType.MODEL_DELTA,
                {"delta": '{"final_answer":"半行'},
            )
        )

        self.assertEqual(self.FakeLive.instances, [])

        renderer(
            AgentEvent(
                EventType.MODEL_DELTA,
                {"delta": '\\n下一'},
            )
        )

        self.assertEqual(len(self.FakeLive.instances), 1)
        live = self.FakeLive.instances[0]
        self.assertTrue(live.started)
        self.assertTrue(live.kwargs["transient"])
        self.assertEqual(live.renderable.renderables[1].markup, "半行\n")

    def test_streaming_preview_is_cleared_before_final_answer(self) -> None:
        output = StringIO()
        renderer = TerminalRenderer(
            Console(file=output, force_terminal=True),
            live_factory=self.FakeLive,
        )

        renderer(
            AgentEvent(
                EventType.MODEL_DELTA,
                {"delta": '{"final_answer":"预览行\\n最终行"}'},
            )
        )
        renderer(
            AgentEvent(
                EventType.RUN_COMPLETED,
                {"answer": "预览行\n最终行", "steps": 1},
            )
        )

        self.assertEqual(len(self.FakeLive.instances), 1)
        self.assertTrue(self.FakeLive.instances[0].stopped)
        rendered = output.getvalue()
        self.assertEqual(rendered.count("预览行"), 1)
        self.assertEqual(rendered.count("最终行"), 1)

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
