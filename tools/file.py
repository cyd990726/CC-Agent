"""Workspace-scoped file tools."""

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
    description = "Read a UTF-8 text file from the workspace."
    args_schema = {"path": "string; path relative to the workspace"}

    def run(self, args: Mapping[str, Any]) -> str:
        path = self.resolve_path(require_string(args, "path"))
        if not path.is_file():
            raise ToolError(f"file does not exist: {path.relative_to(self.workspace)}")
        return path.read_text(encoding="utf-8")


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
