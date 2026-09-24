"""Runtime sandbox support for shell commands."""

from __future__ import annotations

import os
import platform
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}


class SandboxError(RuntimeError):
    """A user-visible sandbox setup or execution error."""


@dataclass(frozen=True)
class SandboxConfig:
    enabled: bool = False
    read_roots: tuple[Path, ...] = ()
    write_roots: tuple[Path, ...] = ()
    deny_read: tuple[Path, ...] = ()
    deny_write: tuple[Path, ...] = ()


class SandboxBackend(Protocol):
    name: str

    def available(self) -> bool:
        """Return whether this backend can run on the current machine."""

    def wrap(self, command: str, *, cwd: Path, config: SandboxConfig) -> list[str]:
        """Return argv for executing command inside the sandbox."""


class NoopSandboxBackend:
    name = "none"

    def available(self) -> bool:
        return True

    def wrap(self, command: str, *, cwd: Path, config: SandboxConfig) -> list[str]:
        return shell_command_argv(command)


class UnsupportedSandboxBackend:
    name = "unsupported"

    def available(self) -> bool:
        return False

    def wrap(self, command: str, *, cwd: Path, config: SandboxConfig) -> list[str]:
        raise SandboxError("sandbox is enabled but this platform is unsupported")


class BubblewrapSandboxBackend:
    name = "bubblewrap"

    def __init__(self, executable: str | None = None) -> None:
        self.executable = executable or shutil.which("bwrap")

    def available(self) -> bool:
        return self.executable is not None

    def wrap(self, command: str, *, cwd: Path, config: SandboxConfig) -> list[str]:
        if self.executable is None:
            raise SandboxError("sandbox is enabled but bubblewrap is not installed")
        temp_root = sandbox_temp_dir()
        args = [
            self.executable,
            "--unshare-all",
            "--die-with-parent",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
            "--setenv",
            "TMPDIR",
            str(temp_root),
        ]
        args.append("--unshare-net")
        for root in _existing_system_roots():
            args.extend(["--ro-bind", str(root), str(root)])
        writable_roots = _unique_paths((*config.write_roots, cwd, temp_root))
        for mountpoint in _bubblewrap_mount_parent_dirs(
            _unique_paths((*config.read_roots, *writable_roots))
        ):
            args.extend(["--dir", str(mountpoint)])
        for root in _unique_paths(config.read_roots):
            if root in writable_roots:
                continue
            if root.exists():
                args.extend(["--ro-bind-try", str(root), str(root)])
        for root in writable_roots:
            root.mkdir(parents=True, exist_ok=True)
            args.extend(["--bind", str(root), str(root)])
        args.extend(["--chdir", str(cwd), shell_path(), "-lc", command])
        return args


class MacOSSandboxExecBackend:
    name = "sandbox-exec-network"

    def __init__(self, executable: str | None = None) -> None:
        self.executable = executable or shutil.which("sandbox-exec")

    def available(self) -> bool:
        return self.executable is not None

    def wrap(self, command: str, *, cwd: Path, config: SandboxConfig) -> list[str]:
        if self.executable is None:
            raise SandboxError("sandbox is enabled but sandbox-exec is not installed")
        profile = _macos_profile(cwd, config)
        return [self.executable, "-p", profile, shell_path(), "-lc", command]


class SandboxManager:
    def __init__(
        self,
        config: SandboxConfig,
        *,
        backend: SandboxBackend | None = None,
    ) -> None:
        self.config = config
        self.backend = backend or choose_backend()

    @classmethod
    def disabled(cls) -> "SandboxManager":
        return cls(SandboxConfig(enabled=False), backend=NoopSandboxBackend())

    @classmethod
    def from_env(
        cls,
        workspace: Path,
        environ: dict[str, str] | None = None,
    ) -> "SandboxManager":
        env = os.environ if environ is None else environ
        enabled = _env_bool(env, "MINI_AGENT_SANDBOX", default=False)
        if not enabled:
            return cls.disabled()
        config = SandboxConfig(
            enabled=enabled,
            write_roots=(workspace.expanduser().resolve(), sandbox_temp_dir()),
            deny_read=default_sensitive_paths(),
            deny_write=default_sensitive_paths(),
        )
        return cls(config)

    def wrap_shell_command(
        self,
        command: str,
        cwd: Path,
    ) -> list[str]:
        if not self.config.enabled:
            return NoopSandboxBackend().wrap(command, cwd=cwd, config=self.config)
        if not self.backend.available():
            message = f"sandbox is enabled but {self.backend.name} is unavailable"
            raise SandboxError(message)
        return self.backend.wrap(command, cwd=cwd, config=self.config)


def choose_backend(system: str | None = None) -> SandboxBackend:
    platform_name = (system or platform.system()).lower()
    if platform_name == "linux":
        return BubblewrapSandboxBackend()
    if platform_name == "darwin":
        return MacOSSandboxExecBackend()
    return UnsupportedSandboxBackend()


def shell_path() -> str:
    if os.name == "nt":
        return os.environ.get("COMSPEC") or "cmd.exe"
    return os.environ.get("SHELL") or "/bin/sh"


def shell_command_argv(command: str) -> list[str]:
    if os.name == "nt":
        return [shell_path(), "/d", "/s", "/c", command]
    return [shell_path(), "-lc", command]


def sandbox_temp_dir() -> Path:
    path = Path(tempfile.gettempdir()) / "mini-agent-sandbox"
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_sensitive_paths(home: Path | None = None) -> tuple[Path, ...]:
    try:
        root = home or Path.home()
    except RuntimeError:
        return ()
    return tuple(
        path
        for path in (
            root / ".ssh",
            root / ".aws",
            root / ".config",
            root / ".gitconfig",
        )
        if path.exists()
    )


def sandbox_status(
    workspace: Path,
    environ: dict[str, str] | None = None,
) -> tuple[str, str]:
    manager = SandboxManager.from_env(workspace, environ)
    if not manager.config.enabled:
        return "warn", "disabled"
    if manager.backend.available():
        return "ok", f"{manager.backend.name}, network=none"
    return "fail", f"{manager.backend.name} unavailable"


def _env_bool(env: dict[str, str] | os._Environ[str], key: str, *, default: bool) -> bool:
    raw = env.get(key)
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value in _TRUTHY:
        return True
    if value in _FALSY:
        return False
    raise ValueError(f"{key} must be a boolean value")


def _existing_system_roots() -> tuple[Path, ...]:
    candidates = (
        Path("/bin"),
        Path("/usr"),
        Path("/lib"),
        Path("/lib64"),
        Path("/etc/ssl"),
        Path("/etc/alternatives"),
    )
    return tuple(path for path in candidates if path.exists())


def _unique_paths(paths: tuple[Path, ...]) -> tuple[Path, ...]:
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in paths:
        resolved = path.expanduser().resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(resolved)
    return tuple(unique)


def _bubblewrap_mount_parent_dirs(paths: tuple[Path, ...]) -> tuple[Path, ...]:
    parents: list[Path] = []
    for path in paths:
        current = path.parent
        chain: list[Path] = []
        while current != current.parent:
            chain.append(current)
            current = current.parent
        parents.extend(reversed(chain))
    return tuple(
        path
        for path in _unique_paths(tuple(parents))
        if path not in {Path("/bin"), Path("/usr"), Path("/lib"), Path("/lib64")}
    )


def _macos_profile(cwd: Path, config: SandboxConfig) -> str:
    # macOS sandbox-exec is too restrictive with `(deny default)` for normal shell
    # startup on current macOS releases. Keep this backend as a network sandbox;
    # Linux bubblewrap provides the stronger filesystem isolation.
    return "(version 1)\n(allow default)\n(deny network*)\n"


def _escape_scheme(path: Path) -> str:
    return str(path).replace("\\", "\\\\").replace('"', '\\"')
