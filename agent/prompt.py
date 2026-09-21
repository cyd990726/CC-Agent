"""Prompt construction for the JSON action protocol."""

import json
from collections.abc import Sequence
from typing import Any


SYSTEM_PROMPT = """You are a small coding agent operating inside a workspace.
Work on the user's task by choosing one tool at a time and using each observation
to decide the next step. Inspect relevant files before changing them and run an
appropriate check when possible.

Reply with exactly one JSON object and no surrounding prose. Use one of these
two forms:

{"thought":"brief status","action":{"tool":"tool_name","args":{}}}
{"thought":"brief status","final_answer":"concise result for the user"}

`thought` is an optional short status summary, not hidden chain-of-thought.
Never invent a tool result. If a tool fails, use the returned observation to
recover. Only provide `final_answer` when the task is complete or you can clearly
explain why it cannot be completed.

Available tools:
{tools}
"""


# 构建系统提示词
def build_system_prompt(tool_descriptions: Sequence[dict[str, Any]]) -> str:
    """Render the runtime prompt with the currently registered tools."""

    return SYSTEM_PROMPT.replace(
        "{tools}", json.dumps(tool_descriptions, ensure_ascii=False, indent=2)
    )
