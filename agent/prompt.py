"""Prompt construction for the JSON action protocol."""

import json
from collections.abc import Sequence
from typing import Any

from agent.permissions import PermissionMode


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


NATIVE_TOOLS_PROMPT = """You are a small coding agent operating inside a workspace.
Work on the user's task by calling exactly one tool per step and using each
observation to decide the next step. Inspect relevant files before changing them
and run an appropriate check when possible.

Tools are provided through the API's native tool-calling interface. Pass each
tool's arguments directly as the top-level parameters of the call. Never wrap
them in an "args", "action", or "parameters" object, and never reply with JSON
text as your message.

Call the `final_answer` tool when the task is complete or you can clearly explain
why it cannot be completed. Never invent a tool result. If a tool fails, use the
returned observation to recover.

Available tools:
{tools}
"""


# 构建系统提示词
def build_system_prompt(
    tool_descriptions: Sequence[dict[str, Any]],
    *,
    plan_mode: bool = False,
    permission_mode: PermissionMode = PermissionMode.ASK,
    memory_section: str | None = None,
    native_tools: bool = False,
) -> str:
    """Render the runtime prompt with the currently registered tools.

    ``native_tools`` selects instructions for providers driven through native
    tool calling. Teaching the JSON envelope there makes models nest the
    envelope inside the tool arguments, so the two protocols must not be mixed.
    """

    template = NATIVE_TOOLS_PROMPT if native_tools else SYSTEM_PROMPT
    prompt = template.replace(
        "{tools}", json.dumps(tool_descriptions, ensure_ascii=False, indent=2)
    )
    if plan_mode:
        prompt += (
            "\nPLAN MODE: This is a read-only planning run. Inspect the workspace "
            "and return a concrete ordered plan. Do not modify files or ask to "
            "execute commands. State assumptions and risks where relevant."
        )
    if memory_section:
        prompt += memory_section
    mode_instructions = {
        PermissionMode.ASK: (
            "Ask for approval mode: ask before edits, shell commands, network "
            "access, or access outside the workspace."
        ),
        PermissionMode.APPROVE: (
            "Approve for me mode: workspace file edits are approved automatically. "
            "Ask before shell commands, network access, or access outside "
            "the workspace."
        ),
        PermissionMode.FULL: (
            "Full Access mode: file tools may access paths outside the workspace, "
            "and tools run without approval. If shell sandboxing is enabled in "
            "configuration, shell commands still run inside that sandbox."
        ),
    }
    prompt += f"\n{mode_instructions[permission_mode]}"
    return prompt
