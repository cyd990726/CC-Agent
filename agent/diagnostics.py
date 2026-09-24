"""Environment diagnostics for the public CLI."""

from __future__ import annotations

import os
import shutil
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from rich import box
from rich.console import Console
from rich.table import Table

from agent.sandbox import sandbox_status
from tools.web import create_search_provider


@dataclass(frozen=True)
class Diagnostic:
    status: str
    name: str
    detail: str


def collect_diagnostics(
    workspace: Path,
    *,
    connectivity: bool = False,
    timeout: float = 10.0,
) -> list[Diagnostic]:
    """Inspect configuration without exposing secret values."""

    checks: list[Diagnostic] = []
    version = ".".join(str(part) for part in sys.version_info[:3])
    checks.append(
        Diagnostic(
            "ok" if sys.version_info >= (3, 10) else "fail",
            "Python",
            version,
        )
    )
    model = os.environ.get("MINI_AGENT_MODEL", "").strip()
    checks.append(
        Diagnostic("ok" if model else "fail", "Model", model or "未配置")
    )
    api_key = os.environ.get("MINI_AGENT_API_KEY", "").strip()
    checks.append(
        Diagnostic(
            "ok" if api_key else "fail",
            "API key",
            "已配置" if api_key else "未配置",
        )
    )
    base_url = os.environ.get(
        "MINI_AGENT_BASE_URL", "https://api.openai.com/v1"
    ).strip()
    parsed = urlsplit(base_url)
    valid_url = parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    checks.append(
        Diagnostic("ok" if valid_url else "fail", "Base URL", base_url)
    )
    workspace_ok = workspace.is_dir()
    workspace_writable = workspace_ok and os.access(workspace, os.W_OK)
    checks.append(
        Diagnostic(
            "ok" if workspace_writable else "fail",
            "Workspace",
            str(workspace) if workspace_ok else "目录不存在",
        )
    )
    shell = os.environ.get("COMSPEC") if os.name == "nt" else os.environ.get("SHELL")
    shell_path = shutil.which(shell) if shell else None
    checks.append(
        Diagnostic(
            "ok" if shell_path else "warn",
            "Shell",
            shell_path or "未检测到；shell 工具可能不可用",
        )
    )
    status, detail = sandbox_status(workspace)
    checks.append(Diagnostic(status, "Shell sandbox", detail))
    search_provider = create_search_provider()
    checks.append(
        Diagnostic(
            "warn" if search_provider.name == "unavailable" else "ok",
            "Web search",
            (
                "未配置（fetch_url 仍可使用）"
                if search_provider.name == "unavailable"
                else search_provider.name
            ),
        )
    )
    if connectivity and valid_url and api_key:
        checks.append(_check_connectivity(base_url, api_key, timeout))
    return checks


def run_doctor(
    console: Console,
    workspace: Path,
    *,
    connectivity: bool = False,
) -> int:
    checks = collect_diagnostics(workspace, connectivity=connectivity)
    table = Table(box=box.ROUNDED, header_style="bold bright_cyan")
    table.add_column("Status", width=8)
    table.add_column("Check", no_wrap=True)
    table.add_column("Details")
    markers = {
        "ok": ("✓ OK", "green"),
        "warn": ("! WARN", "yellow"),
        "fail": ("✕ FAIL", "red"),
    }
    for check in checks:
        marker, style = markers[check.status]
        table.add_row(f"[{style}]{marker}[/]", check.name, check.detail)
    console.print(table)
    failures = sum(check.status == "fail" for check in checks)
    if failures:
        console.print(f"[red]{failures} 项必要检查未通过。[/]")
        return 1
    console.print("[green]✓ Mini Agent 已可以运行。[/]")
    return 0


def _check_connectivity(base_url: str, api_key: str, timeout: float) -> Diagnostic:
    request = urllib.request.Request(
        base_url.rstrip("/") + "/models",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read(1)
        return Diagnostic("ok", "Connectivity", "模型服务可访问")
    except urllib.error.HTTPError as exc:
        return Diagnostic("fail", "Connectivity", f"HTTP {exc.code}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return Diagnostic("fail", "Connectivity", str(exc))
