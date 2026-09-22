"""Workspace-scoped file tools."""

import difflib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from tools.base import Tool, ToolError, require_string


class WorkspaceTool(Tool):
    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        if not self.workspace.is_dir():
            raise ValueError(f"workspace is not a directory: {self.workspace}")

    def resolve_path(self, value: str) -> Path:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        candidate = candidate.resolve()
        try:
            candidate.relative_to(self.workspace)
        except ValueError as exc:
            raise ToolError("path must stay inside the workspace") from exc
        return candidate


class ReadFileTool(WorkspaceTool):
    name = "read_file"
    description = "Read all or a line range from a UTF-8 workspace file."
    args_schema = {
        "path": "string; path relative to the workspace",
        "offset": "optional positive integer; first line to read, 1-based",
        "limit": "optional positive integer; maximum lines to return",
    }

    def run(self, args: Mapping[str, Any]) -> str:
        path = self.resolve_path(require_string(args, "path"))
        if not path.is_file():
            raise ToolError(f"file does not exist: {path.relative_to(self.workspace)}")
        content = path.read_text(encoding="utf-8")
        if "offset" not in args and "limit" not in args:
            return content

        offset = self._positive_integer(args.get("offset", 1), "offset")
        limit_value = args.get("limit")
        limit = (
            self._positive_integer(limit_value, "limit")
            if limit_value is not None
            else None
        )
        lines = content.splitlines(keepends=True)
        total = len(lines)
        if total == 0:
            if offset != 1:
                raise ToolError("offset must be 1 for an empty file")
            return "[file is empty: 0 lines]"
        if offset > total:
            raise ToolError(
                f"offset {offset} exceeds file length of {total} lines"
            )
        start = offset - 1
        selected = lines[start:] if limit is None else lines[start : start + limit]
        end = start + len(selected)
        header = f"[lines {offset}-{end} of {total}]"
        return header + ("\n" + "".join(selected) if selected else "")

    @staticmethod
    def _positive_integer(value: Any, name: str) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ToolError(f"{name} must be a positive integer")
        return value


class WriteFileTool(WorkspaceTool):
    name = "write_file"
    description = "Create or replace a UTF-8 text file in the workspace."
    args_schema = {
        "path": "string; path relative to the workspace",
        "content": "string; complete new file content",
    }

    def run(self, args: Mapping[str, Any]) -> str:
        path = self.resolve_path(require_string(args, "path"))
        content = args.get("content")
        if not isinstance(content, str):
            raise ToolError("content must be a string")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        size = len(content.encode("utf-8"))
        return f"wrote {size} bytes to {path.relative_to(self.workspace)}"


class EditFileTool(WorkspaceTool):
    name = "edit_file"
    description = (
        "Replace one uniquely matching text block in a UTF-8 workspace file."
    )
    args_schema = {
        "path": "string; path relative to the workspace",
        "old_text": "string; exact text that must occur exactly once",
        "new_text": "string; replacement text, which may be empty",
    }

    def run(self, args: Mapping[str, Any]) -> str:
        path = self.resolve_path(require_string(args, "path"))
        if not path.is_file():
            raise ToolError(f"file does not exist: {path.relative_to(self.workspace)}")
        old_text = require_string(args, "old_text")
        new_text = args.get("new_text")
        if not isinstance(new_text, str):
            raise ToolError("new_text must be a string")
        if old_text == new_text:
            raise ToolError("new_text must differ from old_text")

        content = path.read_text(encoding="utf-8")
        matches = content.count(old_text)
        if matches == 0:
            raise ToolError("old_text was not found in the file")
        if matches > 1:
            raise ToolError(
                f"old_text matched {matches} times; provide a unique text block"
            )

        updated = content.replace(old_text, new_text, 1)
        path.write_text(updated, encoding="utf-8")
        relative = path.relative_to(self.workspace).as_posix()
        diff_lines = difflib.unified_diff(
            content.splitlines(keepends=True),
            updated.splitlines(keepends=True),
            fromfile=f"a/{relative}",
            tofile=f"b/{relative}",
        )
        rendered_diff: list[str] = []
        for line in diff_lines:
            rendered_diff.append(line)
            if not line.endswith("\n"):
                rendered_diff.append("\n\\ No newline at end of file\n")
        diff = "".join(rendered_diff)
        return f"updated {relative}\n{diff}".rstrip()
