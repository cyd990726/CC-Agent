import unittest

from agent.permissions import PermissionMode
from agent.prompt import build_system_prompt


TOOLS = [
    {
        "name": "read_file",
        "description": "Read a file.",
        "args": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    }
]


class TextProtocolPromptTests(unittest.TestCase):
    def test_text_protocol_prompt_teaches_json_envelope(self) -> None:
        prompt = build_system_prompt(TOOLS)

        self.assertIn('{"thought":"brief status","action":', prompt)
        self.assertIn('"final_answer":"concise result for the user"', prompt)
        self.assertNotIn("final_answer tool", prompt)

    def test_native_tools_prompt_does_not_teach_json_envelope(self) -> None:
        prompt = build_system_prompt(TOOLS, native_tools=True)

        self.assertNotIn('"action":{"tool":', prompt)
        self.assertNotIn('{"thought"', prompt)

    def test_native_tools_prompt_forbids_argument_wrapping(self) -> None:
        prompt = build_system_prompt(TOOLS, native_tools=True)

        self.assertIn("top-level", prompt)
        self.assertIn("final_answer", prompt)
        self.assertIn("exactly one tool per step", prompt)


class PromptModeTests(unittest.TestCase):
    def test_native_prompt_keeps_plan_and_permission_instructions(self) -> None:
        prompt = build_system_prompt(
            TOOLS,
            native_tools=True,
            plan_mode=True,
            permission_mode=PermissionMode.FULL,
        )

        self.assertIn("PLAN MODE", prompt)
        self.assertIn("Full Access mode", prompt)

    def test_native_prompt_includes_memory_section(self) -> None:
        prompt = build_system_prompt(
            TOOLS,
            native_tools=True,
            memory_section="\n# Persistent memory\nnote\n",
        )

        self.assertIn("Persistent memory", prompt)


if __name__ == "__main__":
    unittest.main()
