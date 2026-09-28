"""Project-scoped persistent memory."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from agent.storage import project_data_dir


ENTRYPOINT_NAME = "MEMORY.md"


@dataclass
class MemoryStore:
    """Small file-backed memory store scoped to a workspace."""

    workspace: Path
    root: Path | None = None

    @property
    def directory(self) -> Path:
        return project_data_dir(self.workspace, root=self.root) / "memory"

    @property
    def entrypoint(self) -> Path:
        return self.directory / ENTRYPOINT_NAME

    def ensure(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        if not self.entrypoint.exists():
            self.entrypoint.write_text(
                "# Mini Agent Memory\n\n"
                "Project-level notes that should persist across sessions.\n",
                encoding="utf-8",
            )

    def read(self) -> str:
        try:
            return self.entrypoint.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    def append(self, content: str) -> None:
        content = " ".join(content.strip().split())
        if not content:
            raise ValueError("memory content cannot be empty")
        self.ensure()
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        existing = self.read()
        prefix = "" if existing.endswith("\n") else "\n"
        with self.entrypoint.open("a", encoding="utf-8") as file:
            file.write(f"{prefix}\n- {timestamp}: {content}\n")

    def prompt_section(self) -> str:
        """Return memory instructions plus the current entrypoint content."""

        self.ensure()
        content = self.read().strip()
        if not content:
            content = "No memories have been saved yet."
        return (
            "\n# Persistent memory\n"
            f"You have project-scoped memory at `{self.directory}`. "
            "Use it proactively for durable user preferences, corrections, "
            "decisions, and project context that cannot be derived from the "
            "current files. When a turn reveals something worth keeping for "
            "future sessions, call the `remember` tool before your final answer; "
            "do not ask for separate confirmation. Do not store secrets, raw "
            "credentials, transient task state, or facts that can be recovered "
            "from the repository. "
            "Treat remembered facts as potentially stale and verify them against "
            "the workspace before relying on them for code changes.\n\n"
            f"## {ENTRYPOINT_NAME}\n{content}\n"
        )
