"""Immutable renderable snapshots, cached at the current terminal width."""

from copy import deepcopy
from dataclasses import dataclass, field
from io import StringIO
from typing import Any

from rich.console import Console


@dataclass
class TranscriptBlock:
    objects: tuple[Any, ...]
    options: dict[str, Any] = field(default_factory=dict)
    _width: int | None = field(default=None, init=False)
    _text: str = field(default="", init=False)

    def __post_init__(self):
        self.objects = deepcopy(self.objects)
        self.options = dict(self.options)

    def render(self, width: int, color_system) -> str:
        width = max(1, width)
        if width != self._width:
            output = StringIO()
            console = Console(
                file=output, width=width, color_system=color_system,
                force_terminal=color_system is not None,
            )
            console.print(*self.objects, **self.options)
            self._text = output.getvalue()
            self._width = width
        return self._text
