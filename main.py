"""Command-line entry point for Mini Agent Runtime."""

import argparse
import os
from pathlib import Path

from rich.console import Console

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
    parser.add_argument("--max-steps", type=int, default=20)
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
        help="allow sensitive tools without confirmation",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="show complete tool output",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    load_env_file(Path(__file__).resolve().parent / ".env")
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.model:
        parser.error("--model or MINI_AGENT_MODEL is required")

    workspace = Path(args.workspace).expanduser().resolve()
    console = Console()
    permission_handler = None
    if not args.no_confirm:
        permission_handler = SessionPermissionHandler(console)
    else:
        console.print(
            "[bold yellow]警告：权限确认已关闭，Agent 可以直接写文件"
            "、执行命令和访问网络。[/]"
        )
    executor = ToolExecutor(
        [
            ReadFileTool(workspace),
            EditFileTool(workspace),
            WriteFileTool(workspace),
            FindFilesTool(workspace),
            ListFilesTool(workspace),
            SearchTool(workspace),
            WebSearchTool(create_search_provider(args.search_provider)),
            FetchUrlTool(timeout=args.request_timeout),
            ShellTool(workspace),
        ],
        permission_handler=permission_handler,
    )
    model = ChatCompletionsLLM(
        model=args.model,
        base_url=args.base_url,
        api_key=os.environ.get("MINI_AGENT_API_KEY"),
        timeout=args.request_timeout,
        max_rpm=args.max_rpm,
    )
    runtime = AgentRuntime(model, executor, max_steps=args.max_steps)
    renderer = TerminalRenderer(console, verbose=args.verbose)
    app = TerminalApp(
        runtime,
        renderer,
        console,
        model_name=args.model,
        workspace=workspace,
    )

    task = " ".join(args.task).strip()
    if task:
        return 0 if app.run_task(task) else 1
    return app.run()


if __name__ == "__main__":
    raise SystemExit(main())
