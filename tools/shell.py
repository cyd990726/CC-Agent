"""Shell command tool."""

import json
import os
import signal
import subprocess
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agent.sandbox import SandboxManager
from agent.cancellation import current_token
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
        if self.sandbox.config.enabled:
            command_or_argv: str | list[str] = self.sandbox.wrap_shell_command(
                command,
                self.workspace,
            )
            shell = False
        else:
            command_or_argv = command
            shell = True
        try:
            token = current_token.get()
            if token is not None:
                return self._run_cancellable(command_or_argv, shell, float(timeout), token)
            completed = subprocess.run(
                command_or_argv,
                cwd=self.workspace,
                shell=shell,
                text=True,
                capture_output=True,
                stdin=subprocess.DEVNULL,
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

    def _run_cancellable(self, command, shell, timeout, token):
        token.check()
        process = subprocess.Popen(
            command, cwd=self.workspace, shell=shell, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            start_new_session=os.name != "nt",
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
        deadline = time.monotonic() + timeout
        try:
            while True:
                token.check()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                try:
                    stdout, stderr = process.communicate(timeout=min(0.05, remaining))
                    token.check()
                    return json.dumps({
                        "exit_code": process.returncode,
                        "stdout": stdout, "stderr": stderr,
                    }, ensure_ascii=False)
                except subprocess.TimeoutExpired:
                    continue
        finally:
            # A shell can exit before descendants that still hold its pipes.
            # Terminate the group even if poll() already reports shell exit.
            if (process.poll() is None or token.event.is_set()
                    or time.monotonic() >= deadline):
                self._terminate_tree(process)

    @staticmethod
    def _terminate_tree(process):
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=5, check=False,
            )
        else:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(process.pid, sig)
                except ProcessLookupError:
                    break
                if sig == signal.SIGTERM:
                    try:
                        process.wait(timeout=0.2)
                    except subprocess.TimeoutExpired:
                        pass
        try:
            process.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)
        finally:
            process.stdout.close()
            process.stderr.close()
