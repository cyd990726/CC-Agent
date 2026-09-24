"""Command-line entry point for Mini Agent Runtime."""

import argparse
import os
import sys
from pathlib import Path

from rich.console import Console

from agent.config import run_init, user_config_path
from agent.diagnostics import run_doctor
from agent.permissions import PermissionMode
from agent.runtime import AgentRuntime
from model.llm import ChatCompletionsLLM
from tools import (
    EditFileTool,
    FetchUrlTool,
    FindFilesTool,
    ListFilesTool,
    ReadFileTool,
    SearchTool,
    ShellTool,
    ToolExecutor,
    WebSearchTool,
    WriteFileTool,
    create_search_provider,
)
from ui import SessionPermissionHandler, TerminalApp, TerminalRenderer


VERSION = "0.1.0"


# 加载环境变量
def load_env_file(path: Path) -> None:
    """Load simple KEY=VALUE entries without overriding the process environment."""

    if not path.is_file():
        return
    lines = path.read_text(encoding="utf-8").splitlines()
    for line_number, raw_line in enumerate(lines, 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").lstrip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not key.isidentifier():
            raise ValueError(f"invalid .env entry on line {line_number}")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a minimal coding agent")
    parser.add_argument("task", nargs="*", help="task for the agent")
    parser.add_argument(
        "--workspace",
        default=".",
        help="workspace available to tools (default: current directory)",
    )
    parser.add_argument(
        "--model", default=os.environ.get("MINI_AGENT_MODEL"), help="model name"
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("MINI_AGENT_BASE_URL", "https://api.openai.com/v1"),
        help="OpenAI-compatible API base URL",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=os.environ.get("MINI_AGENT_MAX_STEPS", "20"),
        help="maximum agent steps (default: 20 or MINI_AGENT_MAX_STEPS)",
    )
    parser.add_argument("--request-timeout", type=float, default=60.0)
    parser.add_argument(
        "--max-rpm",
        type=int,
        default=os.environ.get("MINI_AGENT_MAX_RPM"),
        help="maximum model requests per minute (also auto-detected from 429 errors)",
    )
    parser.add_argument(
        "--search-provider",
        default=os.environ.get("MINI_AGENT_SEARCH_PROVIDER", "auto"),
        choices=["auto", "tavily", "brave", "serper"],
        help="web search provider (default: auto-detect from API key)",
    )
    parser.add_argument(
        "--no-confirm",
        action="store_true",
        help="start in Full Access mode without confirmation",
    )
    parser.add_argument(
        "--plan",
        action="store_true",
        help=(
            "plan with read-only tools; do not modify files or run shell commands"
        ),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="show complete tool output",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    safe_cwd = recover_cwd()
    if arguments and arguments[0] == "init":
        return _run_init_command(arguments[1:])

    loaded_configs = load_configuration(cwd=safe_cwd)
    if arguments and arguments[0] == "doctor":
        return _run_doctor_command(arguments[1:], loaded_configs)

    parser = build_parser()
    args = parser.parse_args(arguments)
    if not args.model:
        parser.error(
            "--model or MINI_AGENT_MODEL is required; "
            "run `mini-agent init` to configure it"
        )

    workspace_path = Path(args.workspace).expanduser()
    if not workspace_path.is_absolute():
        workspace_path = safe_cwd / workspace_path
    workspace = workspace_path.resolve()
    console = Console()
    initial_permission_mode = (
        PermissionMode.FULL if args.no_confirm else PermissionMode.ASK
    )
    permission_handler = SessionPermissionHandler(
        console,
        workspace=workspace,
        mode=initial_permission_mode,
    )
    if args.no_confirm:
        console.print(
            "[bold yellow]警告：Full Access 已启用，Agent 可以访问工作区外文件、"
            "执行命令和访问网络。[/]"
        )
    tools = [
        ReadFileTool(workspace),
        EditFileTool(workspace),
        WriteFileTool(workspace),
        FindFilesTool(workspace),
        ListFilesTool(workspace),
        SearchTool(workspace),
        WebSearchTool(create_search_provider(args.search_provider)),
        FetchUrlTool(timeout=args.request_timeout),
        ShellTool(workspace),
    ]
    executor = ToolExecutor(
        tools,
        permission_handler=permission_handler,
        permission_mode=initial_permission_mode,
    )
    model = ChatCompletionsLLM(
        model=args.model,
        base_url=args.base_url,
        api_key=os.environ.get("MINI_AGENT_API_KEY"),
        timeout=args.request_timeout,
        max_rpm=args.max_rpm,
    )
    runtime = AgentRuntime(
        model,
        executor,
        max_steps=args.max_steps,
        plan_mode=args.plan,
        permission_mode=initial_permission_mode,
    )
    renderer = TerminalRenderer(console, verbose=args.verbose)
    app = TerminalApp(
        runtime,
        renderer,
        console,
        model_name=args.model,
        workspace=workspace,
        plan_mode=args.plan,
        permission_handler=permission_handler,
    )

    task = " ".join(args.task).strip()
    if task:
        return 0 if app.run_task(task) else 1
    return app.run()


def load_configuration(*, cwd: Path | None = None) -> list[Path]:
    """Load project then user configuration, preserving shell precedence."""

    loaded: list[Path] = []
    local_path = (cwd or recover_cwd()) / ".env"
    user_path = user_config_path()
    for path in (local_path, user_path):
        if path in loaded or not path.is_file():
            continue
        load_env_file(path)
        loaded.append(path)
    return loaded


def recover_cwd() -> Path:
    """Return a usable cwd, recovering when the process starts in a deleted path."""

    try:
        return Path.cwd()
    except FileNotFoundError:
        pwd = os.environ.get("PWD", "")
        if pwd:
            candidate = Path(pwd).expanduser()
            for path in (candidate, *candidate.parents):
                if path.is_dir():
                    os.chdir(path)
                    return path.resolve()
        home = Path.home()
        os.chdir(home)
        return home.resolve()


def _run_init_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="mini-agent init",
        description="Configure a model provider for Mini Agent",
    )
    parser.add_argument("--force", action="store_true", help="overwrite config")
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="custom config path (default: user config directory)",
    )
    args = parser.parse_args(argv)
    console = Console()
    try:
        return run_init(console, path=args.config, force=args.force)
    except (EOFError, KeyboardInterrupt):
        console.print("\n[yellow]初始化已取消。[/]")
        return 130


def _run_doctor_command(argv: list[str], loaded_configs: list[Path]) -> int:
    parser = argparse.ArgumentParser(
        prog="mini-agent doctor",
        description="Check Mini Agent configuration and runtime dependencies",
    )
    parser.add_argument("--workspace", default=".")
    parser.add_argument(
        "--connectivity",
        action="store_true",
        help="also call the provider's /models endpoint",
    )
    args = parser.parse_args(argv)
    console = Console()
    if loaded_configs:
        console.print(
            "[bright_black]Loaded config: "
            + ", ".join(str(path) for path in loaded_configs)
            + "[/]"
        )
    return run_doctor(
        console,
        Path(args.workspace).expanduser().resolve(),
        connectivity=args.connectivity,
    )


if __name__ == "__main__":
    raise SystemExit(main())
