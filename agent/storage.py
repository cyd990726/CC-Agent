"""Shared paths for Mini Agent persistent data."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path


def data_home(
    environ: dict[str, str] | None = None,
    *,
    home: Path | None = None,
    platform_name: str | None = None,
) -> Path:
    """Return the per-user Mini Agent data directory without creating it."""

    env = os.environ if environ is None else environ
    explicit = env.get("MINI_AGENT_HOME")
    if explicit:
        return Path(explicit).expanduser()
    platform = os.name if platform_name is None else platform_name
    if platform == "nt" and env.get("APPDATA"):
        return Path(env["APPDATA"]) / "mini-agent"
    if env.get("XDG_DATA_HOME"):
        return Path(env["XDG_DATA_HOME"]) / "mini-agent"
    return (home or Path.home()) / ".local" / "share" / "mini-agent"


def project_key(workspace: Path) -> str:
    """Build a stable, filesystem-safe key for a workspace."""

    resolved = workspace.expanduser().resolve()
    name = re.sub(r"[^A-Za-z0-9_.-]+", "-", resolved.name).strip(".-")
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:12]
    return f"{name or 'workspace'}-{digest}"


def project_data_dir(workspace: Path, *, root: Path | None = None) -> Path:
    """Return the persistent data directory for one workspace."""

    return (root or data_home()) / "projects" / project_key(workspace)
