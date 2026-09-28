"""Persistent memory tools."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agent.memory import MemoryStore
from tools.base import Tool, ToolError, require_string


class ReadMemoryTool(Tool):
    name = "read_memory"
    description = "Read project-scoped Mini Agent memory."
    input_schema = {"type": "object", "properties": {}, "additionalProperties": False}
    read_only = True
    concurrency_safe = True

    def __init__(self, memory: MemoryStore) -> None:
        self.memory = memory

    def run(self, args: Mapping[str, Any]) -> str:
        content = self.memory.read().strip()
        return content or "No memories have been saved yet."


class RememberTool(Tool):
    name = "remember"
    description = (
        "Append a durable project memory. Use proactively for stable user "
        "preferences, corrections, decisions, and project context that should "
        "persist across sessions."
    )
    input_schema = {
        "type": "object",
        "properties": {"content": {"type": "string", "minLength": 1}},
        "required": ["content"],
        "additionalProperties": False,
    }

    def __init__(self, memory: MemoryStore) -> None:
        self.memory = memory

    def run(self, args: Mapping[str, Any]) -> str:
        content = require_string(args, "content")
        try:
            self.memory.append(content)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        return f"saved memory to {self.memory.entrypoint}"
