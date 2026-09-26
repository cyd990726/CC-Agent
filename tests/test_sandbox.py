import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.sandbox import (
    BubblewrapSandboxBackend,
    MacOSSandboxExecBackend,
    SandboxConfig,
    SandboxError,
    SandboxManager,
    choose_backend,
    shell_command_argv,
)
from tools.shell import ShellTool


class FakeBackend:
    name = "fake"

    def __init__(self, *, available: bool = True) -> None:
        self._available = available
        self.calls: list[tuple[str, Path, SandboxConfig]] = []

    def available(self) -> bool:
        return self._available

    def wrap(self, command: str, *, cwd: Path, config: SandboxConfig) -> list[str]:
        self.calls.append((command, cwd, config))
        return ["/fake-sandbox", command]


class SandboxTests(unittest.TestCase):
    def test_disabled_sandbox_uses_shell_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = SandboxManager.disabled()

            argv = manager.wrap_shell_command("echo hello", Path(directory))

        if os.name == "nt":
            self.assertEqual(argv[-4:], ["/d", "/s", "/c", "echo hello"])
        else:
            self.assertEqual(argv[-2:], ["-lc", "echo hello"])

    def test_shell_command_argv_uses_cmd_on_windows(self) -> None:
        with patch("agent.sandbox.os.name", "nt"), patch.dict(
            "agent.sandbox.os.environ",
            {"COMSPEC": r"C:\Windows\System32\cmd.exe"},
        ):
            argv = shell_command_argv("echo hello")

        self.assertEqual(
            argv,
            [r"C:\Windows\System32\cmd.exe", "/d", "/s", "/c", "echo hello"],
        )

    def test_unsupported_platform_fails_closed_when_sandbox_enabled(self) -> None:
        backend = choose_backend("Windows")
        manager = SandboxManager(SandboxConfig(enabled=True), backend=backend)

        with self.assertRaisesRegex(SandboxError, "unavailable"):
            manager.wrap_shell_command("echo hello", Path.cwd())

    def test_enabled_sandbox_uses_backend(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            backend = FakeBackend()
            manager = SandboxManager(
                SandboxConfig(enabled=True),
                backend=backend,
            )
            workspace = Path(directory)

            argv = manager.wrap_shell_command("pwd", workspace)

        self.assertEqual(argv, ["/fake-sandbox", "pwd"])
        self.assertEqual(backend.calls[0][0], "pwd")

    def test_enabled_sandbox_fails_when_backend_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = SandboxManager(
                SandboxConfig(enabled=True),
                backend=FakeBackend(available=False),
            )

            with self.assertRaisesRegex(SandboxError, "unavailable"):
                manager.wrap_shell_command("pwd", Path(directory))

    def test_disabled_env_does_not_probe_home_or_temp_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch("agent.sandbox.Path.home", side_effect=RuntimeError),
                patch("agent.sandbox.sandbox_temp_dir", side_effect=AssertionError),
            ):
                manager = SandboxManager.from_env(
                    Path(directory),
                    environ={},
                )

        self.assertFalse(manager.config.enabled)

    def test_bubblewrap_backend_adds_network_isolation_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            backend = BubblewrapSandboxBackend(executable="/usr/bin/bwrap")
            config = SandboxConfig(enabled=True, write_roots=(workspace,))

            argv = backend.wrap("pwd", cwd=workspace, config=config)

        self.assertIn("--unshare-net", argv)
        self.assertIn("--bind", argv)
        self.assertIn("--dir", argv)
        self.assertIn(str(workspace), argv)

    def test_macos_backend_writes_network_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            backend = MacOSSandboxExecBackend(executable="/usr/bin/sandbox-exec")
            config = SandboxConfig(
                enabled=True,
                write_roots=(workspace,),
            )

            argv = backend.wrap("pwd", cwd=workspace, config=config)
            profile = argv[2]

        self.assertEqual(argv[:2], ["/usr/bin/sandbox-exec", "-p"])
        self.assertIn("(allow default)", profile)
        self.assertIn("(deny network*)", profile)

    def test_shell_tool_executes_wrapped_argv_without_shell_true(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            backend = FakeBackend()
            manager = SandboxManager(SandboxConfig(enabled=True), backend=backend)
            completed = subprocess.CompletedProcess(
                args=["/fake-sandbox", "echo hi"],
                returncode=0,
                stdout="hi\n",
                stderr="",
            )
            with patch("tools.shell.subprocess.run", return_value=completed) as run:
                output = ShellTool(Path(directory), sandbox=manager).run(
                    {"command": "echo hi"}
                )

        self.assertIn('"stdout": "hi\\n"', output)
        _, kwargs = run.call_args
        self.assertEqual(kwargs["shell"], False)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(run.call_args.args[0], ["/fake-sandbox", "echo hi"])

    def test_shell_tool_uses_native_shell_when_sandbox_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            completed = subprocess.CompletedProcess(
                args="echo hi",
                returncode=0,
                stdout="hi\n",
                stderr="",
            )
            with patch("tools.shell.subprocess.run", return_value=completed) as run:
                output = ShellTool(Path(directory)).run({"command": "echo hi"})

        self.assertIn('"stdout": "hi\\n"', output)
        self.assertEqual(run.call_args.args[0], "echo hi")
        self.assertTrue(run.call_args.kwargs["shell"])
        self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)


if __name__ == "__main__":
    unittest.main()
