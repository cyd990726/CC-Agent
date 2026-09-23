"""Read-only workspace discovery tools."""

import fnmatch
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from tools.base import ToolError, require_string
from tools.file import WorkspaceTool


EXCLUDED_DIRECTORIES = frozenset({".git", ".venv", "node_modules", "__pycache__"})


def _positive_integer(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ToolError(f"{name} must be a positive integer")
    return value


class FindFilesTool(WorkspaceTool):
    name = "find_files"
    description = "Find workspace files whose relative paths match a glob pattern."
    args_schema = {
        "pattern": "string; glob such as **/*.py or test_*.py",
        "path": "optional string; search directory, defaults to .",
        "max_results": "optional positive integer; defaults to 100",
    }

    def run(self, args: Mapping[str, Any]) -> str:
        pattern = require_string(args, "pattern")
        root = self.resolve_path(self._path_argument(args))
        if not root.is_dir():
            raise ToolError("find path must be an existing directory")
        max_results = _positive_integer(args.get("max_results", 100), "max_results")

        matches: list[str] = []
        for path in self._files(root):
            relative_to_root = path.relative_to(root).as_posix()
            if not self._matches(relative_to_root, pattern):
                continue
            matches.append(self.display_path(path))
            if len(matches) >= max_results:
                break
        return "\n".join(matches) if matches else "no files"

    def _files(self, root: Path) -> Iterable[Path]:
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root)
            if any(part in EXCLUDED_DIRECTORIES for part in relative.parts):
                continue
            if not path.is_file():
                continue
            resolved = path.resolve()
            if not self.can_access_outside_workspace:
                try:
                    resolved.relative_to(self.workspace)
                except ValueError:
                    continue
            yield resolved

    @staticmethod
    def _matches(path: str, pattern: str) -> bool:
        if fnmatch.fnmatch(path, pattern):
            return True
        return pattern.startswith("**/") and fnmatch.fnmatch(path, pattern[3:])

    @staticmethod
    def _path_argument(args: Mapping[str, Any]) -> str:
        value = args.get("path", ".")
        if not isinstance(value, str) or not value:
            raise ToolError("path must be a non-empty string")
        return value


class ListFilesTool(WorkspaceTool):
    name = "list_files"
    description = "List a workspace directory tree with file sizes."
    args_schema = {
        "path": "optional string; directory to list, defaults to .",
        "depth": "optional positive integer; levels to include, defaults to 1",
        "max_results": "optional positive integer; defaults to 200",
    }

    def run(self, args: Mapping[str, Any]) -> str:
        root = self.resolve_path(FindFilesTool._path_argument(args))
        if not root.is_dir():
            raise ToolError("list path must be an existing directory")
        depth = _positive_integer(args.get("depth", 1), "depth")
        max_results = _positive_integer(args.get("max_results", 200), "max_results")

        entries: list[str] = []
        self._walk(root, root, depth, max_results, entries)
        return "\n".join(entries) if entries else "empty directory"

    def _walk(
        self,
        root: Path,
        directory: Path,
        depth: int,
        max_results: int,
        entries: list[str],
    ) -> None:
        if depth < 1 or len(entries) >= max_results:
            return
        try:
            children = sorted(
                directory.iterdir(),
                key=lambda path: (not path.is_dir(), path.name.lower()),
            )
        except OSError as exc:
            raise ToolError(f"cannot list directory: {exc}") from exc
        for child in children:
            if len(entries) >= max_results:
                return
            if child.name in EXCLUDED_DIRECTORIES:
                continue
            resolved = child.resolve()
            if not self.can_access_outside_workspace:
                try:
                    resolved.relative_to(self.workspace)
                except ValueError:
                    continue
            relative = child.relative_to(root).as_posix()
            if child.is_dir():
                entries.append(f"{relative}/")
                self._walk(root, child, depth - 1, max_results, entries)
            elif child.is_file():
                entries.append(f"{relative} ({child.stat().st_size} bytes)")
