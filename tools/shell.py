"""Shell command tool."""

import json
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agent.sandbox import SandboxManager
from tools.base import Tool, ToolError, require_string


class ShellTool(Tool):
    name = "shell"
    description = (
        "Run a shell command in the workspace and capture its exit code and output."
    )
    args_schema = {
        "command": "string; shell command to run",
        "timeout": "optional number of seconds; defaults to 60",
    }

    def __init__(
        self,
        workspace: str | Path,
        *,
        default_timeout: float = 60.0,
        sandbox: SandboxManager | None = None,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        if not self.workspace.is_dir():
            raise ValueError(f"workspace is not a directory: {self.workspace}")
        self.default_timeout = default_timeout
        self.sandbox = sandbox or SandboxManager.disabled()

    def run(self, args: Mapping[str, Any]) -> str:
        command = require_string(args, "command")
        timeout = args.get("timeout", self.default_timeout)
        if (
            not isinstance(timeout, (int, float))
            or isinstance(timeout, bool)
            or timeout <= 0
        ):
            raise ToolError("timeout must be a positive number")
        argv = self.sandbox.wrap_shell_command(command, self.workspace)
        try:
            completed = subprocess.run(
                argv,
                cwd=self.workspace,
                shell=False,
                text=True,
                capture_output=True,
                timeout=float(timeout),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ToolError(f"command timed out after {timeout} seconds") from exc
        return json.dumps(
            {
                "exit_code": completed.returncode,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
            },
            ensure_ascii=False,
        )
