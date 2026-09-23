"""Crisp cat-head terminal logo for Mini Agent."""

from rich.text import Text


LOGO_LINES = (
    " /\\_/\\",
    "( o.o )",
    " > ^ <",
)


_MOTION = (
    (0, 0), (1, 0), (2, 0), (1, 0), (0, 0),
    (0, 1), (0, 2), (0, 1), (0, 0),
)
_CANVAS_WIDTH = 11


def _motion_frame(horizontal: int, vertical: int) -> tuple[str, ...]:
    cat = (LOGO_LINES[0], "( o.o )", LOGO_LINES[2])
    left = 1 + horizontal
    lines = [" " * _CANVAS_WIDTH for _ in range(5)]
    for index, line in enumerate(cat):
        row = list(lines[vertical + index])
        row[left : left + len(line)] = line
        lines[vertical + index] = "".join(row)
    return tuple(lines)


LOGO_ANIMATION_FRAMES = tuple(_motion_frame(x, y) for x, y in _MOTION)


def render_logo(*, color: bool = True, frame: int | None = None) -> Text:
    """Render one frame of the cat-head logo animation."""

    logo = Text(no_wrap=True)
    lines = LOGO_LINES if frame is None else LOGO_ANIMATION_FRAMES[frame % len(LOGO_ANIMATION_FRAMES)]
    for index, line in enumerate(lines):
        style = "bright_cyan" if color else "default"
        logo.append(line, style=style)
        if index < len(lines) - 1:
            logo.append("\n")
    if color:
        logo.highlight_regex("o", "bold white")
        logo.highlight_regex("^", "bold bright_white")
    return logo
