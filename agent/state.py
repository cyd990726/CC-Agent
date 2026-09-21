"""In-memory state for one agent run."""

from dataclasses import dataclass, field

from tools.base import ToolResult


@dataclass
class AgentState:
    """Mutable execution state for a single user task."""

    current_task: str
    messages: list[dict[str, str]] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)
    finished: bool = False
    final_answer: str | None = None
    steps: int = 0

    def add_message(self, role: str, content: str) -> None:
        self.messages.append({"role": role, "content": content})
