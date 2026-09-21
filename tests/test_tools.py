import json
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from tools.base import Tool, ToolExecutor
from tools.file import ReadFileTool, WriteFileTool
from tools.search import SearchTool
from tools.shell import ShellTool


class ToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_write_and_read_file(self) -> None:
        executor = ToolExecutor(
            [WriteFileTool(self.workspace), ReadFileTool(self.workspace)]
        )
        write = executor.execute(
            "write_file", {"path": "src/example.py", "content": "answer = 42\n"}
        )
        read = executor.execute("read_file", {"path": "src/example.py"})

        self.assertTrue(write.success)
        self.assertTrue(read.success)
        self.assertEqual(read.output, "answer = 42\n")

    def test_file_tool_rejects_path_outside_workspace(self) -> None:
        result = ToolExecutor([ReadFileTool(self.workspace)]).execute(
            "read_file", {"path": "../outside.txt"}
        )

        self.assertFalse(result.success)
        self.assertIn("inside the workspace", result.output)

    def test_search_finds_matching_lines(self) -> None:
        (self.workspace / "one.py").write_text("alpha\nbeta\n", encoding="utf-8")
        (self.workspace / "two.txt").write_text("beta\n", encoding="utf-8")

        output = SearchTool(self.workspace).run({"query": "beta", "glob": "*.py"})

        self.assertEqual(output, "one.py:2:beta")

    def test_shell_captures_exit_code_and_streams(self) -> None:
        output = ShellTool(self.workspace).run(
            {"command": "printf hello; printf problem >&2; exit 3"}
        )
        result = json.loads(output)

        self.assertEqual(result["exit_code"], 3)
        self.assertEqual(result["stdout"], "hello")
        self.assertEqual(result["stderr"], "problem")

    def test_permission_denial_is_a_recoverable_result(self) -> None:
        class RecordingTool(Tool):
            name = "sensitive"
            description = "A sensitive test tool."
            args_schema: Mapping[str, Any] = {}

            def __init__(self) -> None:
                self.called = False

            def run(self, args: Mapping[str, Any]) -> str:
                self.called = True
                return "ran"

        tool = RecordingTool()
        executor = ToolExecutor(
            [tool], permission_handler=lambda _name, _args: False
        )

        result = executor.execute("sensitive", {})

        self.assertFalse(result.success)
        self.assertEqual(result.output, "execution denied by user")
        self.assertFalse(tool.called)

    def test_permission_interrupt_is_not_swallowed(self) -> None:
        def interrupt(_name: str, _args: Mapping[str, Any]) -> bool:
            raise KeyboardInterrupt

        executor = ToolExecutor(
            [ReadFileTool(self.workspace)], permission_handler=interrupt
        )

        with self.assertRaises(KeyboardInterrupt):
            executor.execute("read_file", {"path": "anything.txt"})


if __name__ == "__main__":
    unittest.main()
