import json
import tempfile
import unittest
from pathlib import Path

from tools.base import ToolExecutor
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


if __name__ == "__main__":
    unittest.main()
