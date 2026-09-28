"""Mouse selection for a read-only transcript and system clipboard writes."""

import base64
import os
import shutil
import subprocess
import sys

from prompt_toolkit.formatted_text import fragment_list_to_text
from prompt_toolkit.formatted_text.utils import split_lines
from prompt_toolkit.layout.controls import FormattedTextControl, UIContent
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

    def __init__(self, source, on_start, on_copy, source_lines=None):
        self.source = source
        self.source_lines = source_lines
        self.on_start = on_start
        self.on_copy = on_copy
        self.snapshot = None
        self._snapshot_lines = None
        self._line_texts = ()
        self._line_offsets = ()
        self.rendered = []
        self._line_fragments = ((),)
        self.anchor = self.end = 0
        self.dragging = False
        self.width = None
        super().__init__(self._selection_fragments, show_cursor=False)

    @property
    def selected_text(self):
        if self._snapshot_lines is not None:
            return self._selected_text_from_lines()
        text = fragment_list_to_text(self.snapshot or [])
        start, end = sorted((self.anchor, self.end))
        return text[start:end]

    def clear_selection(self):
        self.snapshot = None
        self._snapshot_lines = None
        self._line_texts = ()
        self._line_offsets = ()
        self.anchor = self.end = 0
        self.dragging = False

    def create_content(self, width, height):
        if self.width is not None and width != self.width:
            self.clear_selection()
        self.width = width
        if self.snapshot is None:
            if self.source_lines is not None:
                lines = tuple(self.source_lines())
            else:
                lines = tuple(tuple(line) for line in split_lines(list(self.source())))
            self._line_fragments = lines or ((),)
            self._refresh_line_metrics(self._line_fragments)
        elif self._snapshot_lines is not None:
            lines = self._snapshot_lines
        else:
            lines = tuple(tuple(line) for line in split_lines(self._selection_fragments()))
        return UIContent(
            get_line=lambda index: self._line_with_selection(lines[index], index)
            if self._snapshot_lines is not None
            else lines[index],
            line_count=len(lines),
            show_cursor=False,
        )

    def _selection_fragments(self):
        if self._snapshot_lines is not None:
            fragments = []
            for index, line in enumerate(self._snapshot_lines):
                if index:
                    fragments.append(("", "\n"))
                fragments.extend(self._line_with_selection(line, index))
            self.rendered = fragments
            return fragments
        fragments = (
            self.snapshot
            if self.snapshot is not None
            else self._current_fragments()
        )
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

    def _current_fragments(self):
        if self.source_lines is None:
            return list(self.source())
        return self._flatten_lines(self._line_fragments)

    @staticmethod
    def _flatten_lines(lines):
        fragments = []
        for index, line in enumerate(lines):
            if index:
                fragments.append(("", "\n"))
            fragments.extend(line)
        return fragments

    def _refresh_line_metrics(self, lines):
        texts = tuple(fragment_list_to_text(line) for line in lines)
        offsets = []
        offset = 0
        for text in texts:
            offsets.append(offset)
            offset += len(text) + 1
        self._line_texts = texts
        self._line_offsets = tuple(offsets)

    def _offset(self, position):
        if self._line_texts:
            lines = self._line_texts
            row = max(0, min(position.y, len(lines) - 1))
            return self._line_offsets[row] + min(max(0, position.x), len(lines[row]))
        lines = fragment_list_to_text(self.snapshot or self.rendered).split("\n")
        row = max(0, min(position.y, len(lines) - 1))
        return sum(len(line) + 1 for line in lines[:row]) + min(
            max(0, position.x), len(lines[row]))

    def _line_with_selection(self, line, index):
        if not self._line_texts or index >= len(self._line_texts):
            return line
        start, end = sorted((self.anchor, self.end))
        line_start = self._line_offsets[index]
        line_text = self._line_texts[index]
        line_end = line_start + len(line_text)
        selected_start = max(0, min(len(line_text), start - line_start))
        selected_end = max(selected_start, min(len(line_text), end - line_start))
        if selected_start == selected_end or end <= line_start or start >= line_end:
            return line

        result = []
        offset = 0
        for style, text, *rest in line:
            left = max(0, min(len(text), selected_start - offset))
            right = max(left, min(len(text), selected_end - offset))
            if left:
                result.append((style, text[:left], *rest))
            if right > left:
                result.append((style + " reverse", text[left:right], *rest))
            if right < len(text):
                result.append((style, text[right:], *rest))
            offset += len(text)
        return tuple(result)

    def _selected_text_from_lines(self):
        start, end = sorted((self.anchor, self.end))
        if start == end:
            return ""
        selected = []
        for index, text in enumerate(self._line_texts):
            line_start = self._line_offsets[index]
            line_end = line_start + len(text)
            selected_start = max(0, min(len(text), start - line_start))
            selected_end = max(selected_start, min(len(text), end - line_start))
            if selected_start < selected_end:
                selected.append(text[selected_start:selected_end])
            if index < len(self._line_texts) - 1 and start <= line_end and end > line_end:
                selected.append("\n")
        return "".join(selected)

    def mouse_handler(self, event):
        if event.event_type in (MouseEventType.SCROLL_UP, MouseEventType.SCROLL_DOWN):
            return NotImplemented
        if event.button is not MouseButton.LEFT and not (
            self.dragging and event.event_type is MouseEventType.MOUSE_UP
            and event.button is MouseButton.UNKNOWN
        ):
            return NotImplemented
        if event.event_type is MouseEventType.MOUSE_DOWN:
            if self.source_lines is not None:
                self._snapshot_lines = tuple(self.source_lines()) or ((),)
                self.snapshot = []
                self._refresh_line_metrics(self._snapshot_lines)
                self.rendered = []
            else:
                self.snapshot = self._current_fragments()
                self._snapshot_lines = tuple(
                    tuple(line) for line in split_lines(self.snapshot)
                )
                self._refresh_line_metrics(self._snapshot_lines)
                self.rendered = list(self.snapshot)
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
