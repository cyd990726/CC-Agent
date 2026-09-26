"""Regressions for viewport, approval, input and worker lifecycle boundaries."""

import json
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

from prompt_toolkit.formatted_text import fragment_list_to_text
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.utils import get_cwidth
from rich.console import Console
from rich.markdown import Markdown
from agent.cancellation import CancellationToken

from agent.permissions import PermissionMode
from agent.events import AgentEvent, EventType
from agent.runtime import AgentRuntime
from ui.permissions import SessionPermissionHandler
from ui.renderer import TerminalRenderer, _JsonStringFieldStreamer
from ui.terminal import TerminalApp, _TranscriptWindow


class InteractionTests(unittest.TestCase):
    def test_copy_selection_precedes_cancel_and_escape_restores_cancel(self):
        from prompt_toolkit.keys import Keys

        app = self.make_app()
        token = CancellationToken()
        app._active_cancellation = token
        with create_pipe_input() as pipe:
            application = app._create_application(input=pipe, output=DummyOutput())
        app._application = application
        app._input_field.text = "draft"
        control = app._selection_control
        control.snapshot = [("", "selected output")]
        control.anchor, control.end = 0, 8
        event = Mock(app=application)
        copy = application.key_bindings.get_bindings_for_keys((Keys.ControlC,))[-1]
        with patch("ui.terminal.copy_to_clipboard", return_value="已复制") as clipboard:
            copy.handler(event)
        clipboard.assert_called_once_with("selected", application.output)
        self.assertFalse(token.event.is_set())
        self.assertEqual(app._input_field.text, "draft")
        escape = application.key_bindings.get_bindings_for_keys((Keys.Escape,))[-1]
        self.assertTrue(escape.filter())
        escape.handler(event)
        self.assertIsNone(control.snapshot)
        copy.handler(event)
        self.assertTrue(token.event.is_set())

    def test_transcript_reflows_markdown_and_table_without_duplicate_content(self):
        app = self.make_app(30)
        app._ui_active = True
        source = "| Name | Description |\n|---|---|\n| item | " + "word " * 25 + " |\n\nEND"
        app._append_renderables((Markdown(source),))
        narrow = "".join(app._transcript)
        with app._state_lock:
            app._reflow_transcript(100)
        wide = "".join(app._transcript)
        self.assertLess(wide.count("\n"), narrow.count("\n"))
        self.assertEqual(wide.count("END"), 1)
        with app._state_lock:
            app._reflow_transcript(30)
        self.assertEqual("".join(app._transcript), narrow)

    def test_ctrl_c_cancels_busy_task_and_preserves_input_draft(self):
        app = self.make_app()
        token = CancellationToken()
        app._active_cancellation = token
        with create_pipe_input() as pipe:
            application = app._create_application(input=pipe, output=DummyOutput())
        app._input_field.text = "draft"
        from prompt_toolkit.keys import Keys
        bindings = application.key_bindings.get_bindings_for_keys((Keys.ControlC,))
        event = Mock(app=application)
        bindings[-1].handler(event)
        self.assertTrue(token.event.is_set())
        self.assertEqual(app._input_field.text, "draft")

    def make_app(self, width=80):
        console = Console(file=StringIO(), width=width, force_terminal=False)
        runtime = Mock(spec=AgentRuntime)
        return TerminalApp(
            runtime, TerminalRenderer(console), console,
            model_name="a-very-long-model-name", workspace=Path("/tmp/中文目录/long/path"),
            permission_handler=SessionPermissionHandler(console),
        )

    def test_wrapped_line_scrolls_by_screen_rows_and_stays_anchored(self):
        control = FormattedTextControl("x" * 100)
        window = _TranscriptWindow(
            content=control, wrap_lines=True,
            on_scroll=lambda offset: None, get_target_line=lambda: 999,
        )
        content = control.create_content(10, 3)
        window._scroll(content, 10, 3)
        tail = window.vertical_scroll_2
        self.assertGreater(tail, 0)
        window.scroll_rows(-2)
        window._scroll(content, 10, 3)
        self.assertEqual(window.vertical_scroll_2, tail - 2)
        content = FormattedTextControl("x" * 200).create_content(10, 3)
        window._scroll(content, 10, 3)
        self.assertEqual(window.vertical_scroll_2, tail - 2)
        window.scroll_rows(100)
        window._scroll(content, 10, 3)
        self.assertIsNone(window._view_anchor)
        self.assertGreater(window.vertical_scroll_2, tail)

    def test_footer_fits_narrow_terminal_and_keeps_permission_mode(self):
        for width in (1, 12, 24, 40, 80):
            with self.subTest(width=width):
                app = self.make_app(width)
                app.runtime.permission_mode = PermissionMode.FULL
                text = fragment_list_to_text(app._status_fragments())
                self.assertLessEqual(get_cwidth(text), width)
                if width >= len("full access on"):
                    self.assertIn("full access on", text)

    def test_unexpected_task_error_does_not_kill_queue_worker(self):
        app = self.make_app()
        app.runtime.run.side_effect = [RuntimeError("adapter failed"), None]
        prompts = iter(["first", "second", "/exit"])
        app._prompt = lambda _: next(prompts)
        app._uses_terminal_prompt = False
        self.assertEqual(app.run(), 0)
        self.assertEqual([call.args[0] for call in app.runtime.run.call_args_list], ["first", "second"])
        self.assertIn("adapter failed", app.console.file.getvalue())

    def test_shutdown_denies_new_permission_without_waiting(self):
        app = self.make_app()
        app._closing = True
        self.assertEqual(app._request_permission("shell", {"command": "pwd"}), "n")
        self.assertTrue(app._permission_requests.empty())

    def test_approval_menu_has_numbered_choices_scope_and_fits_width(self):
        from ui.terminal import _PermissionRequest
        import threading

        for width in (12, 40, 80):
            app = self.make_app(width)
            app._active_permission = _PermissionRequest(
                "shell", {"command": "echo 中文 " + "x" * 100}, threading.Event())
            app._permission_selection = 1
            fake_app = Mock()
            fake_app.output.get_size.return_value.columns = width
            with patch("ui.terminal.get_app", return_value=fake_app):
                fragments = app._permission_fragments()
            text = fragment_list_to_text(fragments)
            self.assertEqual(len(text.splitlines()), 10)
            for line in text.splitlines():
                self.assertLessEqual(get_cwidth(line), width)
            if width == 80:
                self.assertIn("是否允许执行命令？", text)
                self.assertIn("❯ 2. 本次会话始终允许", text)
                self.assertIn("所有 shell 调用", text)
                self.assertIn("Esc 拒绝", text)
            self.assertTrue(any(style == "class:approval.selected" for style, _ in fragments))

    def test_approval_number_keys_keep_the_existing_decisions(self):
        from ui.terminal import _PermissionRequest
        import threading

        for key, answer in (("1", "y"), ("2", "a"), ("3", "n")):
            app = self.make_app()
            with create_pipe_input() as pipe:
                application = app._create_application(input=pipe, output=DummyOutput())
            request = _PermissionRequest("shell", {"command": "pwd"}, threading.Event())
            app._permission_requests.put(request)
            app._activate_permission_request()
            self.assertEqual(app._permission_selection, 2)
            binding = application.key_bindings.get_bindings_for_keys((key,))[-1]
            self.assertTrue(binding.filter())
            binding.handler(Mock(app=application))
            self.assertEqual(request.answer, answer)
            self.assertTrue(request.ready.is_set())
            self.assertFalse(binding.filter())

    def test_approval_diff_separates_unterminated_lines(self):
        preview = SessionPermissionHandler._snippet_diff("before", "after", "a.py")
        self.assertIn("-before\n\\ No newline at end of file\n+after\n", preview)
        rendered = SessionPermissionHandler._style_diff(preview)
        self.assertIn("+after", rendered.plain)

    def test_approval_panel_preserves_full_command_and_edit_preview(self):
        app = self.make_app()
        command = "echo " + "x" * 100 + " && echo END_OF_COMMAND"
        app.console.print(app.permission_handler.request_panel("shell", {"command": command}))
        app.console.print(app.permission_handler.request_panel("edit_file", {
            "path": "example.txt", "old_text": "before", "new_text": "after",
        }))
        rendered = app.console.file.getvalue()
        self.assertIn("END_OF_COMMAND", rendered)
        self.assertIn("-before", rendered)
        self.assertIn("+after", rendered)

    def test_completion_arrow_enter_submits_command_not_task(self):
        app = self.make_app()
        with create_pipe_input() as pipe:
            original = app._create_application
            app._create_application = lambda: original(input=pipe, output=DummyOutput())
            pipe.send_text("/sta\x1b[B\r/exit\r")
            self.assertEqual(app.run(), 0)
        app.runtime.run.assert_not_called()
        self.assertIn("Session", app.console.file.getvalue())

    def test_json_unicode_surrogate_pair_survives_every_chunk_boundary(self):
        answer = "中文 😀 done\n\n"
        payload = json.dumps({"final_answer": answer}, ensure_ascii=True)
        for split in range(len(payload) + 1):
            stream = _JsonStringFieldStreamer("final_answer")
            result = stream.feed(payload[:split]) + stream.feed(payload[split:])
            self.assertEqual(result, answer)
        stream = _JsonStringFieldStreamer("final_answer")
        self.assertEqual("".join(stream.feed(char) for char in payload), answer)

    def test_invalid_unicode_escape_does_not_crash_renderer(self):
        stream = _JsonStringFieldStreamer("final_answer")
        self.assertEqual(stream.feed('{"final_answer":"\\uZZZZ"}'), "�")

    def test_answer_activity_persists_without_resetting_for_each_delta(self):
        app = self.make_app()
        updates = []
        app.renderer.set_activity_handler(lambda message, start: updates.append((message, start)))
        app.renderer(AgentEvent(EventType.MODEL_STARTED, {"step": 1}))
        app.renderer(AgentEvent(EventType.MODEL_DELTA, {"delta": '{"final_answer":"hello'}))
        responding = updates[-1]
        self.assertEqual(responding[0], "Responding")
        app.renderer(AgentEvent(EventType.MODEL_DELTA, {"delta": ' world"}'}))
        self.assertEqual(updates[-1], responding)
        app.renderer(AgentEvent(EventType.RUN_COMPLETED, {"answer": "hello world", "steps": 1}))
        self.assertEqual(updates[-1], (None, None))
