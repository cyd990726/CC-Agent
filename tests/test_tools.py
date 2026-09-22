import json
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from tools.base import Tool, ToolExecutor
from tools.discovery import FindFilesTool, ListFilesTool
from tools.file import EditFileTool, ReadFileTool, WriteFileTool
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

    def test_read_file_supports_line_ranges(self) -> None:
        path = self.workspace / "lines.txt"
        path.write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")

        output = ReadFileTool(self.workspace).run(
            {"path": "lines.txt", "offset": 2, "limit": 2}
        )

        self.assertEqual(output, "[lines 2-3 of 4]\ntwo\nthree\n")

    def test_read_file_rejects_invalid_line_range(self) -> None:
        (self.workspace / "lines.txt").write_text("one\n", encoding="utf-8")
        result = ToolExecutor([ReadFileTool(self.workspace)]).execute(
            "read_file", {"path": "lines.txt", "offset": 2}
        )

        self.assertFalse(result.success)
        self.assertIn("exceeds file length", result.output)

    def test_edit_file_replaces_unique_text_and_returns_diff(self) -> None:
        path = self.workspace / "example.py"
        path.write_text("answer = 41\nprint(answer)\n", encoding="utf-8")

        output = EditFileTool(self.workspace).run(
            {
                "path": "example.py",
                "old_text": "answer = 41",
                "new_text": "answer = 42",
            }
        )

        self.assertEqual(
            path.read_text(encoding="utf-8"), "answer = 42\nprint(answer)\n"
        )
        self.assertIn("--- a/example.py", output)
        self.assertIn("+++ b/example.py", output)
        self.assertIn("-answer = 41", output)
        self.assertIn("+answer = 42", output)

    def test_edit_file_diff_reports_removed_final_newline(self) -> None:
        path = self.workspace / "newline.txt"
        path.write_text("value\n", encoding="utf-8")

        output = EditFileTool(self.workspace).run(
            {"path": "newline.txt", "old_text": "value\n", "new_text": "value"}
        )

        self.assertEqual(path.read_text(encoding="utf-8"), "value")
        self.assertIn("No newline at end of file", output)

    def test_edit_file_rejects_non_unique_text_without_writing(self) -> None:
        path = self.workspace / "duplicate.txt"
        path.write_text("same\nsame\n", encoding="utf-8")
        result = ToolExecutor([EditFileTool(self.workspace)]).execute(
            "edit_file",
            {"path": "duplicate.txt", "old_text": "same", "new_text": "new"},
        )

        self.assertFalse(result.success)
        self.assertIn("matched 2 times", result.output)
        self.assertEqual(path.read_text(encoding="utf-8"), "same\nsame\n")

    def test_find_files_supports_recursive_globs(self) -> None:
        (self.workspace / "src" / "nested").mkdir(parents=True)
        (self.workspace / "root.py").write_text("", encoding="utf-8")
        (self.workspace / "src" / "one.py").write_text("", encoding="utf-8")
        (self.workspace / "src" / "nested" / "two.py").write_text(
            "", encoding="utf-8"
        )
        (self.workspace / "src" / "notes.txt").write_text("", encoding="utf-8")

        output = FindFilesTool(self.workspace).run({"pattern": "**/*.py"})

        self.assertEqual(
            output.splitlines(),
            ["root.py", "src/nested/two.py", "src/one.py"],
        )

    def test_list_files_respects_depth_and_displays_sizes(self) -> None:
        (self.workspace / "src" / "nested").mkdir(parents=True)
        (self.workspace / "README.md").write_text("hello", encoding="utf-8")
        (self.workspace / "src" / "app.py").write_text("x", encoding="utf-8")
        (self.workspace / "src" / "nested" / "deep.py").write_text(
            "x", encoding="utf-8"
        )

        output = ListFilesTool(self.workspace).run({"depth": 2})

        self.assertIn("src/", output)
        self.assertIn("src/app.py (1 bytes)", output)
        self.assertIn("src/nested/", output)
        self.assertIn("README.md (5 bytes)", output)
        self.assertNotIn("deep.py", output)

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
