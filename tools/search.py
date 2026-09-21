"""Portable workspace text search tool."""

import fnmatch
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from tools.base import ToolError, require_string
from tools.file import WorkspaceTool


class SearchTool(WorkspaceTool):
    name = "search"
    description = "Search text files in the workspace and return matching lines."
    args_schema = {
        "query": "string; literal text or regular expression",
        "path": "optional string; file or directory, defaults to .",
        "glob": "optional string; filename glob such as *.py",
        "regex": "optional boolean; defaults to false",
        "max_results": "optional positive integer; defaults to 100",
    }

    def run(self, args: Mapping[str, Any]) -> str:
        query = require_string(args, "query")
        path_value = args.get("path", ".")
        if not isinstance(path_value, str) or not path_value:
            raise ToolError("path must be a non-empty string")
        root = self.resolve_path(path_value)
        if not root.exists():
            relative_root = root.relative_to(self.workspace)
            raise ToolError(f"search path does not exist: {relative_root}")

        glob = args.get("glob")
        if glob is not None and (not isinstance(glob, str) or not glob):
            raise ToolError("glob must be a non-empty string")
        use_regex = args.get("regex", False)
        if not isinstance(use_regex, bool):
            raise ToolError("regex must be a boolean")
        max_results = args.get("max_results", 100)
        if (
            not isinstance(max_results, int)
            or isinstance(max_results, bool)
            or max_results < 1
        ):
            raise ToolError("max_results must be a positive integer")

        try:
            pattern = re.compile(query if use_regex else re.escape(query))
        except re.error as exc:
            raise ToolError(f"invalid regular expression: {exc}") from exc

        matches: list[str] = []
        for discovered_path in self._files(root):
            path = discovered_path.resolve()
            try:
                path.relative_to(self.workspace)
            except ValueError:
                continue
            if glob and not fnmatch.fnmatch(path.name, glob):
                continue
            try:
                relative = path.relative_to(self.workspace)
                with path.open(encoding="utf-8") as handle:
                    for line_number, line in enumerate(handle, 1):
                        if pattern.search(line):
                            matches.append(f"{relative}:{line_number}:{line.rstrip()}")
                            if len(matches) >= max_results:
                                return "\n".join(matches)
            except (OSError, UnicodeDecodeError, ValueError):
                continue
        return "\n".join(matches) if matches else "no matches"

    @staticmethod
    def _files(root: Path) -> Iterable[Path]:
        if root.is_file():
            yield root
            return
        for path in root.rglob("*"):
            if path.is_file() and not any(
                part in {".git", "__pycache__"} for part in path.parts
            ):
                yield path
