"""Selection boundaries, stream stability and clipboard routing."""

import base64
import unittest
from unittest.mock import Mock, patch

from prompt_toolkit.data_structures import Point
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
from ui.selection import TranscriptSelectionControl, copy_to_clipboard


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.fragments = [("bold", "你好 world\nsecond line")]
        self.copied = Mock()
        self.started = Mock()
        self.control = TranscriptSelectionControl(
            lambda: self.fragments, self.started, self.copied)
        self.control.create_content(40, 10)

    def mouse(self, kind, x, y=0, button=MouseButton.LEFT):
        return self.control.mouse_handler(MouseEvent(
            position=Point(x=x, y=y), event_type=kind,
            button=button, modifiers=frozenset()))

    def test_reverse_multiline_selection_copies_plain_unicode(self):
        self.mouse(MouseEventType.MOUSE_DOWN, 6, 1)
        self.mouse(MouseEventType.MOUSE_MOVE, 0)
        fragments = self.control._selection_fragments()
        self.assertTrue(any("reverse" in style and text for style, text in fragments))
        self.copied.assert_not_called()
        self.mouse(MouseEventType.MOUSE_UP, 0)
        self.copied.assert_called_once_with("你好 world\nsecond")
        self.started.assert_called_once()

    def test_streaming_keeps_selection_snapshot_and_wheel_passes_through(self):
        self.mouse(MouseEventType.MOUSE_DOWN, 0)
        self.fragments.append(("", "\nnew output"))
        self.control._selection_fragments()
        self.assertNotIn("new output", str(self.control.rendered))
        self.assertIs(self.mouse(MouseEventType.SCROLL_UP, 0), NotImplemented)
        self.mouse(MouseEventType.MOUSE_UP, 2, button=MouseButton.UNKNOWN)
        self.copied.assert_called_once_with("你好")
        self.control.clear_selection()
        self.control._selection_fragments()
        self.assertIn("new output", str(self.control.rendered))

    def test_click_without_drag_does_not_copy(self):
        self.mouse(MouseEventType.MOUSE_DOWN, 2)
        self.mouse(MouseEventType.MOUSE_UP, 2)
        self.copied.assert_not_called()
        self.assertIsNone(self.control.snapshot)

    def test_resize_clears_stale_coordinates(self):
        self.mouse(MouseEventType.MOUSE_DOWN, 0)
        self.mouse(MouseEventType.MOUSE_MOVE, 2)
        self.control.create_content(20, 10)
        self.assertEqual(self.control.selected_text, "")
        self.assertIsNone(self.control.snapshot)

    @patch("ui.selection.subprocess.run")
    @patch("ui.selection.shutil.which", return_value="/usr/bin/pbcopy")
    @patch("ui.selection.sys.platform", "darwin")
    @patch.dict("ui.selection.os.environ", {}, clear=True)
    def test_macos_clipboard_receives_utf8_without_shell(self, which, run):
        output = Mock()
        self.assertEqual(copy_to_clipboard("你好", output), "已复制到剪贴板")
        self.assertEqual(run.call_args.args[0], ["pbcopy"])
        self.assertEqual(run.call_args.kwargs["input"], "你好".encode())
        output.write_raw.assert_not_called()

    @patch("ui.selection.shutil.which", return_value=None)
    def test_clipboard_fallback_uses_base64_osc52(self, which):
        output = Mock()
        result = copy_to_clipboard("hello\n你好", output)
        encoded = base64.b64encode("hello\n你好".encode()).decode()
        output.write_raw.assert_called_once_with(f"\033]52;c;{encoded}\a")
        self.assertIn("请求", result)
        output.flush.assert_called_once()
