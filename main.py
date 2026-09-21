"""Command-line entry point for Mini Agent Runtime."""

import argparse
import os
import sys
from pathlib import Path

from agent.runtime import AgentRuntime, AgentRuntimeError
from model.llm import ChatCompletionsLLM, ModelError
from tools import ReadFileTool, SearchTool, ShellTool, ToolExecutor, WriteFileTool


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
    return parser


def main(argv: list[str] | None = None) -> int:
    load_env_file(Path(__file__).resolve().parent / ".env")
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.model:
        parser.error("--model or MINI_AGENT_MODEL is required")

    task = " ".join(args.task).strip()
    if not task:
        try:
            task = input("Task: ").strip()
        except EOFError:
            task = ""
    if not task:
        parser.error("task cannot be empty")

    workspace = Path(args.workspace).expanduser().resolve()
    executor = ToolExecutor(
        [
            ReadFileTool(workspace),
            WriteFileTool(workspace),
            SearchTool(workspace),
            ShellTool(workspace),
        ]
    )
    model = ChatCompletionsLLM(
        model=args.model,
        base_url=args.base_url,
        api_key=os.environ.get("MINI_AGENT_API_KEY"),
        timeout=args.request_timeout,
    )
    runtime = AgentRuntime(model, executor, max_steps=args.max_steps)
    try:
        state = runtime.run(task)
    except (AgentRuntimeError, ModelError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(state.final_answer)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
