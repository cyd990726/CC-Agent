"""Stable transcript projections cached by width and expansion state."""

from copy import deepcopy
from dataclasses import dataclass, field
from io import StringIO
from typing import Any

from rich.console import Console


@dataclass
class TranscriptBlock:
    objects: tuple[Any, ...]
    options: dict[str, Any] = field(default_factory=dict)
    block_id: str | None = None
    expanded_objects: tuple[Any, ...] | None = None
    _cache: dict[tuple[int, bool], str] = field(default_factory=dict, init=False)

    def __post_init__(self):
        self.objects = deepcopy(self.objects)
        self.options = dict(self.options)
        if self.expanded_objects is not None:
            self.expanded_objects = deepcopy(self.expanded_objects)

    @property
    def expandable(self) -> bool:
        return self.expanded_objects is not None

    def render(self, width: int, color_system, *, expanded: bool = False) -> str:
        width = max(1, width)
        use_expanded = expanded and self.expanded_objects is not None
        key = (width, use_expanded)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        output = StringIO()
        console = Console(
            file=output,
            width=width,
            color_system=color_system,
            force_terminal=color_system is not None,
        )
        objects = self.expanded_objects if use_expanded else self.objects
        console.print(*objects, **self.options)
        text = output.getvalue()
        self._cache[key] = text
        return text
