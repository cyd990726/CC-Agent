import threading
import time
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

from prompt_toolkit.document import Document
from prompt_toolkit.data_structures import Point
from prompt_toolkit.formatted_text import ANSI, to_formatted_text
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
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
    @staticmethod
    def _attach_renderer(app: TerminalApp, renderer: TerminalRenderer) -> None:
        app._ui_active = True
        renderer.set_activity_handler(app._update_activity)
        renderer.set_output_handler(app._write_transcript)

    @staticmethod
    def _transcript_text(app: TerminalApp) -> str:
        return "".join(
            text
            for style_text in app._transcript
            for _style, text in to_formatted_text(ANSI(style_text))
        )

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

    def test_interactive_activity_has_own_row_and_preserves_status(self) -> None:
        output = StringIO()
        console = Console(file=output, force_terminal=False)
        renderer = TerminalRenderer(console)
        app = TerminalApp(
            Mock(spec=AgentRuntime),
            renderer,
            console,
            model_name="test-model",
            workspace=Path("/tmp/example"),
            plan_mode=True,
        )
        self._attach_renderer(app, renderer)

        renderer(AgentEvent(EventType.MODEL_STARTED, {"step": 4}))

        status = "".join(text for _style, text in app._status_fragments())
        fake_app = Mock()
        fake_app.output.get_size.return_value.columns = 100
        with patch("ui.terminal.get_app", return_value=fake_app):
            activity = "".join(
                text for _style, text in app._activity_fragments()
            )
            border = "".join(
                text for _style, text in app._border_fragments("╭", "╮")
            )
        self.assertIsNone(renderer._status)
        self.assertNotIn("Thinking", status)
        self.assertIn("plan mode on", status)
        self.assertIn("Thinking · step 4", activity)
        self.assertNotIn("Thinking", border)

        renderer(AgentEvent(EventType.MODEL_COMPLETED, {"response": {}}))
        with patch("ui.terminal.get_app", return_value=fake_app):
            activity = "".join(
                text for _style, text in app._activity_fragments()
            )
        self.assertNotIn("Thinking", activity)

    def test_interactive_exploration_is_committed_when_tool_finishes(self) -> None:
        output = StringIO()
        console = Console(file=output, force_terminal=True)
        renderer = TerminalRenderer(console)
        app = TerminalApp(
            Mock(spec=AgentRuntime),
            renderer,
            console,
            model_name="test-model",
            workspace=Path("/tmp/example"),
        )
        self._attach_renderer(app, renderer)

        renderer(
            AgentEvent(
                EventType.TOOL_REQUESTED,
                {"tool": "read_file", "args": {"path": "README.md"}},
            )
        )
        renderer(
            AgentEvent(
                EventType.TOOL_COMPLETED,
                {
                    "result": {
                        "tool": "read_file",
                        "success": True,
                        "output": "contents",
                    }
                },
            )
        )

        rendered_before_close = self._transcript_text(app)
        self.assertNotIn("Explored", rendered_before_close)
        self.assertIn("Read README.md", rendered_before_close)

        renderer.close()

        rendered = self._transcript_text(app)
        self.assertNotIn("Explored", rendered)
        self.assertEqual(rendered.count("Read README.md"), 1)

    def test_interactive_shell_call_is_committed_when_tool_finishes(self) -> None:
        output = StringIO()
        console = Console(file=output, force_terminal=True)
        renderer = TerminalRenderer(console)
        app = TerminalApp(
            Mock(spec=AgentRuntime),
            renderer,
            console,
            model_name="test-model",
            workspace=Path("/tmp/example"),
        )
        self._attach_renderer(app, renderer)

        renderer(
            AgentEvent(
                EventType.TOOL_REQUESTED,
                {"tool": "shell", "args": {"command": "pwd"}},
            )
        )
        renderer(
            AgentEvent(
                EventType.TOOL_COMPLETED,
                {
                    "result": {
                        "tool": "shell",
                        "success": True,
                        "output": '{"exit_code":0,"stdout":"/tmp","stderr":""}',
                    }
                },
            )
        )

        rendered = self._transcript_text(app)
        self.assertIn("Bash", rendered)
        self.assertIn("pwd", rendered)
        self.assertIn("Exited with code 0", rendered)

    def test_streaming_transcript_never_mutates_input_buffer(self) -> None:
        output = StringIO()
        console = Console(file=output, force_terminal=True)
        renderer = TerminalRenderer(console)
        app = TerminalApp(
            Mock(spec=AgentRuntime),
            renderer,
            console,
            model_name="test-model",
            workspace=Path("/tmp/example"),
        )
        with create_pipe_input() as pipe_input:
            application = app._create_application(
                input=pipe_input,
                output=DummyOutput(),
            )
        self._attach_renderer(app, renderer)
        assert app._input_field is not None
        app._input_field.text = "正在输入的草稿"

        renderer(AgentEvent(EventType.MODEL_STARTED, {"step": 1}))
        renderer(
            AgentEvent(
                EventType.MODEL_DELTA,
                {
                    "delta": (
                        '{"final_answer":"第一段\\n\\n第二段\\n\\n'
                        '第三段"}'
                    )
                },
            )
        )
        renderer(
            AgentEvent(
                EventType.RUN_COMPLETED,
                {"answer": "第一段\n\n第二段\n\n第三段", "steps": 1},
            )
        )

        self.assertTrue(application.full_screen)
        self.assertEqual(app._input_field.text, "正在输入的草稿")
        self.assertEqual(output.getvalue(), "")
        rendered = self._transcript_text(app)
        self.assertEqual(rendered.count("第一段"), 1)
        self.assertEqual(rendered.count("第二段"), 1)
        self.assertEqual(rendered.count("第三段"), 1)

    def test_transcript_scroll_clamps_to_same_content_snapshot(self) -> None:
        console = Console(file=StringIO(), force_terminal=False, width=80)
        app = TerminalApp(
            Mock(spec=AgentRuntime),
            TerminalRenderer(console),
            console,
            model_name="test-model",
            workspace=Path("/tmp/example"),
        )
        app._transcript = ["first\nsecond\n"]
        app._transcript_styled = list(
            to_formatted_text(ANSI(app._transcript[0]))
        )
        app._transcript_lines = app._fragment_line_count(
            app._transcript_styled
        )

        with create_pipe_input() as pipe_input:
            app._create_application(
                input=pipe_input,
                output=DummyOutput(),
            )
        assert app._transcript_window is not None
        content = app._transcript_window.content.create_content(
            width=80, height=20
        )

        # Simulate a background append after FormattedTextControl captured its
        # fragments but before Window calculates scrolling.
        app._transcript_lines = 100
        app._transcript_window._scroll(content, width=80, height=20)

        self.assertLess(
            app._transcript_window.vertical_scroll,
            content.line_count,
        )

    def test_trackpad_scroll_moves_transcript_not_input_history(self) -> None:
        console = Console(file=StringIO(), force_terminal=False, width=80)
        app = TerminalApp(
            Mock(spec=AgentRuntime),
            TerminalRenderer(console),
            console,
            model_name="test-model",
            workspace=Path("/tmp/example"),
        )
        with create_pipe_input() as pipe_input:
            application = app._create_application(
                input=pipe_input,
                output=DummyOutput(),
            )
        assert app._input_field is not None
        app._input_field.text = "draft"
        app._transcript_lines = 20

        app._input_field.window._mouse_handler(
            MouseEvent(
                position=Point(x=0, y=0),
                event_type=MouseEventType.SCROLL_UP,
                button=MouseButton.NONE,
                modifiers=frozenset(),
            )
        )

        self.assertTrue(application.mouse_support())
        self.assertEqual(app._transcript_cursor_line, 16)
        self.assertEqual(app._input_field.text, "draft")

        app._scroll_transcript(3)
        self.assertIsNone(app._transcript_cursor_line)

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

    def test_inputs_submitted_while_busy_are_processed_in_order(self) -> None:
        output = StringIO()
        console = Console(file=output, force_terminal=False)
        runtime = Mock(spec=AgentRuntime)
        answers = iter(["first task", "second task", "/exit"])
        app = TerminalApp(
            runtime,
            TerminalRenderer(console),
            console,
            model_name="test-model",
            workspace=Path("/tmp/example"),
            prompt=lambda _message: next(answers),
        )

        result = app.run()

        self.assertEqual(result, 0)
        self.assertEqual(
            [call.args[0] for call in runtime.run.call_args_list],
            ["first task", "second task"],
        )

    def test_unified_application_accepts_input_while_worker_runs(self) -> None:
        output = StringIO()
        console = Console(file=output, force_terminal=False, width=80)
        runtime = Mock(spec=AgentRuntime)
        renderer = TerminalRenderer(console)
        app = TerminalApp(
            runtime,
            renderer,
            console,
            model_name="test-model",
            workspace=Path("/tmp/example"),
        )

        with create_pipe_input() as pipe_input:
            original_create = app._create_application
            app._create_application = lambda: original_create(
                input=pipe_input,
                output=DummyOutput(),
            )
            pipe_input.send_text("first task\rsecond task\r/exit\r")

            result = app.run()

        self.assertEqual(result, 0)
        self.assertEqual(
            [call.args[0] for call in runtime.run.call_args_list],
            ["first task", "second task"],
        )

    def test_permission_callback_can_be_replaced_by_terminal_owner(self) -> None:
        console = Console(file=StringIO(), force_terminal=False)
        handler = SessionPermissionHandler(console)
        handler.set_prompt_callback(lambda _message: "y")

        self.assertTrue(handler("shell", {"command": "pwd"}))

    def test_background_permission_request_is_served_by_terminal_thread(self) -> None:
        console = Console(file=StringIO(), force_terminal=False)
        handler = SessionPermissionHandler(console)
        app = TerminalApp(
            Mock(spec=AgentRuntime),
            TerminalRenderer(console),
            console,
            model_name="test-model",
            workspace=Path("/tmp/example"),
            permission_handler=handler,
        )
        handler.set_request_callback(app._request_permission)
        result: list[bool] = []
        worker = threading.Thread(
            target=lambda: result.append(handler("shell", {"command": "pwd"}))
        )

        worker.start()
        deadline = time.monotonic() + 1
        while app._permission_requests.empty() and time.monotonic() < deadline:
            time.sleep(0.001)
        self.assertTrue(app._activate_permission_request())
        self.assertEqual(app._active_permission.tool_name, "shell")
        app._resolve_permission("y")
        worker.join(timeout=1)

        self.assertFalse(worker.is_alive())
        self.assertEqual(result, [True])
        self.assertEqual(console.file.getvalue(), "")


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

    def test_edit_result_shows_complete_diff_without_verbose(self) -> None:
        import difflib

        before = "".join(f"line {index}\n" for index in range(30))
        after = before.replace("line 5\n", "changed five\n").replace(
            "line 25\n", "changed twenty five\n")
        diff = "".join(difflib.unified_diff(
            before.splitlines(keepends=True), after.splitlines(keepends=True),
            fromfile="a/example.py", tofile="b/example.py"))
        for interactive in (False, True):
            with self.subTest(interactive=interactive):
                output = StringIO()
                renderer = TerminalRenderer(Console(
                    file=output, force_terminal=False, width=100))
                chunks = []
                if interactive:
                    renderer.set_output_handler(chunks.append)
                renderer(AgentEvent(EventType.TOOL_COMPLETED, {"result": {
                    "tool": "edit_file", "success": True,
                    "output": "updated example.py\n" + diff,
                }}))
                rendered = "".join(chunks) if interactive else output.getvalue()
                self.assertIn("+2 / -2 lines", rendered)
                for line in diff.splitlines()[2:]:
                    self.assertIn(line, rendered)
                self.assertNotIn("/verbose", rendered)
                self.assertNotIn("--- a/example.py", rendered)

    def test_edit_diff_colors_and_newline_marker(self) -> None:
        renderer = TerminalRenderer(Console(file=StringIO(), force_terminal=False))
        rows = []
        renderer.set_render_handler(lambda objects, options: rows.extend(objects))
        renderer(AgentEvent(EventType.TOOL_COMPLETED, {"result": {
            "tool": "edit_file", "success": True,
            "output": "updated a.py\n--- a/a.py\n+++ b/a.py\n"
                      "@@ -1 +1 @@\n--- old\n+++ new\n"
                      "\\ No newline at end of file",
        }}))
        styles = {row.plain: row.style for row in rows}
        self.assertEqual(styles["    │ --- old"], "red")
        self.assertEqual(styles["    │ +++ new"], "green")
        self.assertIn("    │ \\ No newline at end of file", styles)
        self.assertIn("+1 / -1 lines", rows[0].plain)

    def test_trimmed_stream_completion_keeps_the_answer_tail(self) -> None:
        import json

        output = StringIO()
        renderer = TerminalRenderer(Console(file=output, force_terminal=False))
        answer = "\n\nFirst paragraph.\n\nTAIL remains intact."
        renderer(AgentEvent(EventType.MODEL_STARTED, {"step": 1}))
        renderer(AgentEvent(EventType.MODEL_DELTA, {
            "delta": json.dumps({"final_answer": answer}),
        }))
        renderer(AgentEvent(EventType.RUN_COMPLETED, {"answer": answer.strip(), "steps": 1}))
        self.assertEqual(output.getvalue().count("First paragraph."), 1)
        self.assertIn("TAIL remains intact.", output.getvalue())

    def test_protocol_retry_is_not_rendered_as_read_failure(self) -> None:
        output = StringIO()
        renderer = TerminalRenderer(Console(file=output, force_terminal=True,
                                            color_system=None), live_factory=self.FakeLive)
        renderer(AgentEvent(EventType.TOOL_REQUESTED, {
            "tool": "read_file", "args": {"path": "README.md"},
        }))
        renderer(AgentEvent(EventType.TOOL_COMPLETED, {"result": {
            "tool": "read_file", "success": True, "output": "contents",
        }}))
        renderer(AgentEvent(EventType.TOOL_COMPLETED, {"result": {
            "tool": "model_protocol", "success": False,
            "output": "model did not return valid JSON",
        }}))
        renderer.close()
        rendered = output.getvalue()
        self.assertLess(rendered.index("Read README.md"), rendered.index("模型响应格式异常"))
        self.assertNotIn("Failed", rendered)
        self.assertIsNone(renderer._exploration)

    def test_toggle_verbose(self) -> None:
        renderer = TerminalRenderer(
            Console(file=StringIO(), force_terminal=False), verbose=False
        )

        self.assertTrue(renderer.toggle_verbose())
        self.assertFalse(renderer.toggle_verbose())

    def test_committed_output_can_be_owned_by_interactive_ui(self) -> None:
        output = StringIO()
        chunks: list[str] = []
        renderer = TerminalRenderer(Console(file=output, force_terminal=False))
        renderer.set_output_handler(chunks.append)

        renderer(
            AgentEvent(
                EventType.RUN_COMPLETED,
                {"answer": "最终答案", "steps": 1},
            )
        )

        self.assertEqual(output.getvalue(), "")
        self.assertIn("最终答案", "".join(chunks))
        self.assertIn("Done in", "".join(chunks))

    def test_thinking_status_includes_live_elapsed_time(self) -> None:
        output = StringIO()
        console = Console(file=output, force_terminal=False)
        renderer = TerminalRenderer(console)

        renderer(AgentEvent(EventType.MODEL_STARTED, {"step": 4}))

        self.assertIsNotNone(renderer._status)
        assert renderer._status is not None
        console.print(renderer._status.status)
        rendered = output.getvalue()
        self.assertIn("Thinking · step 4", rendered)
        self.assertRegex(rendered, r"Thinking · step 4 · \d+ms")

        renderer(AgentEvent(EventType.MODEL_COMPLETED, {"response": {}}))
        self.assertIsNone(renderer._status)

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

    def test_streaming_output_waits_for_complete_markdown_block(self) -> None:
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

        self.assertNotIn("半行", output.getvalue())

        renderer(
            AgentEvent(
                EventType.MODEL_DELTA,
                {"delta": '\\n下一'},
            )
        )

        self.assertEqual(self.FakeLive.instances, [])
        self.assertNotIn("半行", output.getvalue())

        renderer(
            AgentEvent(
                EventType.MODEL_DELTA,
                {"delta": '\\n\\n'},
            )
        )

        self.assertIn("半行", output.getvalue())
        self.assertIn("下一", output.getvalue())

    def test_streaming_markdown_does_not_split_fenced_code(self) -> None:
        stable = TerminalRenderer._stable_streaming_text

        self.assertEqual(stable('```python\nprint("hello")\n'), "")
        self.assertEqual(
            stable('```python\nprint("hello")\n```\n\n'),
            '```python\nprint("hello")\n```\n\n',
        )

    def test_streaming_output_is_not_reprinted_on_completion(self) -> None:
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

        self.assertEqual(self.FakeLive.instances, [])
        rendered = output.getvalue()
        self.assertEqual(rendered.count("预览行"), 1)
        self.assertEqual(rendered.count("最终行"), 1)

    def test_long_streaming_answer_is_committed_once(self) -> None:
        output = StringIO()
        renderer = TerminalRenderer(
            Console(file=output, force_terminal=True, height=8),
            live_factory=self.FakeLive,
        )
        answer = "\n\n".join(f"唯一行-{index}" for index in range(40))

        renderer(AgentEvent(EventType.MODEL_STARTED, {"step": 1}))
        renderer(
            AgentEvent(
                EventType.MODEL_DELTA,
                {"delta": '{"final_answer":"' + answer.replace("\n", "\\n") + '"}'},
            )
        )
        renderer(
            AgentEvent(
                EventType.RUN_COMPLETED,
                {"answer": answer, "steps": 1},
            )
        )

        rendered = output.getvalue()
        self.assertEqual(self.FakeLive.instances, [])
        self.assertEqual(rendered.count("唯一行-0"), 1)
        self.assertEqual(rendered.count("唯一行-39"), 1)

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

    def test_tool_request_is_live_before_tool_completes(self) -> None:
        output = StringIO()
        console = Console(file=output, force_terminal=True)
        renderer = TerminalRenderer(console, live_factory=self.FakeLive)

        renderer(
            AgentEvent(
                EventType.TOOL_REQUESTED,
                {"tool": "read_file", "args": {"path": "README.md"}},
            )
        )

        self.assertEqual(len(self.FakeLive.instances), 1)
        live = self.FakeLive.instances[0]
        self.assertTrue(live.started)
        self.assertTrue(live.kwargs["transient"])
        self.assertEqual(live.kwargs["vertical_overflow"], "crop")
        console.print(live.renderable)
        rendered = output.getvalue()
        self.assertIn("Read", rendered)
        self.assertIn("README.md", rendered)
        self.assertRegex(rendered, r"\d+ms")

    def test_tool_completion_stops_live_line_and_commits_transcript(self) -> None:
        output = StringIO()
        renderer = TerminalRenderer(
            Console(file=output, force_terminal=True),
            live_factory=self.FakeLive,
        )
        renderer(
            AgentEvent(
                EventType.TOOL_REQUESTED,
                {"tool": "shell", "args": {"command": "pwd"}},
            )
        )

        renderer(
            AgentEvent(
                EventType.TOOL_COMPLETED,
                {
                    "result": {
                        "tool": "shell",
                        "success": True,
                        "output": '{"exit_code":0,"stdout":"/tmp","stderr":""}',
                    }
                },
            )
        )

        self.assertTrue(self.FakeLive.instances[0].stopped)
        rendered = output.getvalue()
        self.assertIn("Bash", rendered)
        self.assertIn("pwd", rendered)
        self.assertIn("Exited with code 0", rendered)

    def test_consecutive_exploration_tools_are_grouped_until_other_work(self) -> None:
        output = StringIO()
        console = Console(file=output, force_terminal=True)
        renderer = TerminalRenderer(console, live_factory=self.FakeLive)

        for path in ("events.py", "runtime.py"):
            renderer(
                AgentEvent(
                    EventType.TOOL_REQUESTED,
                    {"tool": "read_file", "args": {"path": path}},
                )
            )
            renderer(
                AgentEvent(
                    EventType.TOOL_COMPLETED,
                    {
                        "result": {
                            "tool": "read_file",
                            "success": True,
                            "output": "contents",
                        }
                    },
                )
            )

        self.assertEqual(len(self.FakeLive.instances), 1)
        self.assertFalse(self.FakeLive.instances[0].stopped)

        renderer(
            AgentEvent(
                EventType.TOOL_REQUESTED,
                {"tool": "shell", "args": {"command": "pwd"}},
            )
        )

        self.assertTrue(self.FakeLive.instances[0].stopped)
        rendered = output.getvalue()
        self.assertNotIn("Explored", rendered)
        self.assertIn("Read events.py, runtime.py", rendered)

    def test_model_commentary_breaks_tools_and_preserves_order(self) -> None:
        for interactive in (False, True):
            with self.subTest(interactive=interactive):
                output = StringIO()
                console = Console(file=output, force_terminal=True, color_system=None)
                renderer = TerminalRenderer(console, live_factory=self.FakeLive)
                if interactive:
                    renderer.set_activity_handler(lambda *_: None)
                for tool, path, thought in (
                    ("list_files", "agent/", "Inspecting the runtime next."),
                    ("read_file", "agent/runtime.py", "Found the relevant logic."),
                ):
                    renderer(AgentEvent(EventType.TOOL_REQUESTED, {
                        "tool": tool, "args": {"path": path},
                    }))
                    renderer(AgentEvent(EventType.TOOL_COMPLETED, {
                        "result": {"tool": tool, "success": True,
                                   "output": "raw contents hidden"},
                    }))
                    renderer(AgentEvent(EventType.MODEL_COMPLETED, {
                        "response": {"thought": thought, "action": {}},
                    }))
                    self.assertIsNone(renderer._exploration)
                renderer.close()
                rendered = output.getvalue()
                self.assertNotIn("Explored", rendered)
                self.assertNotIn("raw contents hidden", rendered)
                self.assertEqual(rendered.count("Read agent/runtime.py"), 1)
                self.assertLess(rendered.index("List agent/"),
                                rendered.index("Inspecting the runtime next."))
                self.assertLess(rendered.index("Inspecting the runtime next."),
                                rendered.index("Read agent/runtime.py"))
                self.assertLess(rendered.index("Read agent/runtime.py"),
                                rendered.index("Found the relevant logic."))
                self.assertIn("\n• Inspecting the runtime next.", rendered)
                self.assertIn("\n• Found the relevant logic.", rendered)

    def test_failed_exploration_keeps_error_visible(self) -> None:
        output = StringIO()
        renderer = TerminalRenderer(
            Console(file=output, force_terminal=True),
            live_factory=self.FakeLive,
        )
        renderer(
            AgentEvent(
                EventType.TOOL_REQUESTED,
                {"tool": "read_file", "args": {"path": "missing.py"}},
            )
        )
        renderer(
            AgentEvent(
                EventType.TOOL_COMPLETED,
                {
                    "result": {
                        "tool": "read_file",
                        "success": False,
                        "output": "file does not exist",
                    }
                },
            )
        )

        renderer.close()

        rendered = output.getvalue()
        self.assertIn("missing.py", rendered)
        self.assertIn("file does not exist", rendered)

    def test_completed_exploration_has_no_group_or_item_timer(self) -> None:
        output = StringIO()
        console = Console(file=output, force_terminal=True)
        renderer = TerminalRenderer(console, live_factory=self.FakeLive)

        renderer(
            AgentEvent(
                EventType.TOOL_REQUESTED,
                {"tool": "read_file", "args": {"path": "README.md"}},
            )
        )
        renderer(
            AgentEvent(
                EventType.TOOL_COMPLETED,
                {
                    "result": {
                        "tool": "read_file",
                        "success": True,
                        "output": "contents",
                    }
                },
            )
        )

        assert renderer._exploration is not None
        console.print(renderer._exploration)

        rendered = output.getvalue()
        self.assertNotIn("Explored", rendered)
        self.assertIn("Read README.md", rendered)
        self.assertNotRegex(rendered, r"\d+(?:ms|\.\d+s)")


if __name__ == "__main__":
    unittest.main()
