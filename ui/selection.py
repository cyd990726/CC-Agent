"""Mouse selection for a read-only transcript and system clipboard writes."""

import base64
import os
import shutil
import subprocess
import sys

from prompt_toolkit.formatted_text import fragment_list_to_text
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.mouse_events import MouseButton, MouseEventType


def copy_to_clipboard(text, output):
    """Use a local clipboard when available, otherwise ask the terminal."""
    commands = []
    if not os.environ.get("SSH_CONNECTION"):
        if sys.platform == "darwin":
            commands = [["pbcopy"]]
        elif sys.platform == "win32":
            commands = [["clip.exe"]]
        else:
            commands = [["wl-copy"], ["xclip", "-selection", "clipboard"],
                        ["xsel", "--clipboard", "--input"]]
    for command in commands:
        if shutil.which(command[0]):
            try:
                encoding = "utf-16le" if command[0] == "clip.exe" else "utf-8"
                subprocess.run(command, input=text.encode(encoding), check=True,
                               timeout=2, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
                return "已复制到剪贴板"
            except (OSError, subprocess.SubprocessError):
                continue
    encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
    output.write_raw(f"\033]52;c;{encoded}\a")
    output.flush()
    return "已发送复制请求（需终端支持 OSC 52）"


class TranscriptSelectionControl(FormattedTextControl):
    """Keep an immutable selection snapshot while output continues arriving."""

    def __init__(self, source, on_start, on_copy):
        self.source = source
        self.on_start = on_start
        self.on_copy = on_copy
        self.snapshot = None
        self.rendered = []
        self.anchor = self.end = 0
        self.dragging = False
        self.width = None
        super().__init__(self._selection_fragments, show_cursor=False)

    @property
    def selected_text(self):
        text = fragment_list_to_text(self.snapshot or [])
        start, end = sorted((self.anchor, self.end))
        return text[start:end]

    def clear_selection(self):
        self.snapshot = None
        self.anchor = self.end = 0
        self.dragging = False

    def create_content(self, width, height):
        if self.width is not None and width != self.width:
            self.clear_selection()
        self.width = width
        return super().create_content(width, height)

    def _selection_fragments(self):
        fragments = self.snapshot if self.snapshot is not None else list(self.source())
        self.rendered = list(fragments)
        start, end = sorted((self.anchor, self.end))
        result = []
        offset = 0
        for style, text, *_ in fragments:
            left = max(0, min(len(text), start - offset))
            right = max(left, min(len(text), end - offset))
            result.extend(((style, text[:left]),
                           (style + " reverse", text[left:right]),
                           (style, text[right:])))
            offset += len(text)
        return result

    def _offset(self, position):
        lines = fragment_list_to_text(self.snapshot or self.rendered).split("\n")
        row = max(0, min(position.y, len(lines) - 1))
        return sum(len(line) + 1 for line in lines[:row]) + min(
            max(0, position.x), len(lines[row]))

    def mouse_handler(self, event):
        if event.event_type in (MouseEventType.SCROLL_UP, MouseEventType.SCROLL_DOWN):
            return NotImplemented
        if event.button is not MouseButton.LEFT and not (
            self.dragging and event.event_type is MouseEventType.MOUSE_UP
            and event.button is MouseButton.UNKNOWN
        ):
            return NotImplemented
        if event.event_type is MouseEventType.MOUSE_DOWN:
            self.snapshot = list(self.rendered)
            self.anchor = self.end = self._offset(event.position)
            self.dragging = True
            self.on_start()
        elif self.dragging and event.event_type in (
            MouseEventType.MOUSE_MOVE, MouseEventType.MOUSE_UP
        ):
            self.end = self._offset(event.position)
            if event.event_type is MouseEventType.MOUSE_UP:
                self.dragging = False
                if self.selected_text:
                    self.on_copy(self.selected_text)
                else:
                    self.clear_selection()
        else:
            return NotImplemented
        return None
