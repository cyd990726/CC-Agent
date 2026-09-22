"""User-level configuration and first-run setup helpers."""

from __future__ import annotations

import getpass
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console


@dataclass(frozen=True)
class ProviderPreset:
    label: str
    base_url: str
    model_example: str


PROVIDER_PRESETS: dict[str, ProviderPreset] = {
    "openai": ProviderPreset(
        "OpenAI",
        "https://api.openai.com/v1",
        "gpt-5.1",
    ),
    "kimi-cn": ProviderPreset(
        "Kimi（中国大陆）",
        "https://api.moonshot.cn/v1",
        "kimi-k2.5",
    ),
    "kimi-global": ProviderPreset(
        "Kimi（国际）",
        "https://api.moonshot.ai/v1",
        "kimi-k2.5",
    ),
    "deepseek": ProviderPreset(
        "DeepSeek",
        "https://api.deepseek.com",
        "deepseek-flash",
    ),
    "custom": ProviderPreset("自定义 OpenAI-compatible", "", "your-model"),
}

SEARCH_PROVIDERS: dict[str, tuple[str, str]] = {
    "none": ("暂不配置", ""),
    "tavily": ("Tavily", "TAVILY_API_KEY"),
    "brave": ("Brave Search", "BRAVE_SEARCH_API_KEY"),
    "serper": ("Serper", "SERPER_API_KEY"),
}


def user_config_path(
    environ: Mapping[str, str] | None = None,
    *,
    home: Path | None = None,
    platform_name: str | None = None,
) -> Path:
    """Return the per-user config path without creating it."""

    env = os.environ if environ is None else environ
    explicit = env.get("MINI_AGENT_CONFIG")
    if explicit:
        return Path(explicit).expanduser()
    platform = os.name if platform_name is None else platform_name
    if platform == "nt" and env.get("APPDATA"):
        root = Path(env["APPDATA"])
    elif env.get("XDG_CONFIG_HOME"):
        root = Path(env["XDG_CONFIG_HOME"])
    else:
        root = (home or Path.home()) / ".config"
    return root / "mini-agent" / ".env"


def write_user_config(
    path: Path,
    values: Mapping[str, str],
    *,
    overwrite: bool = False,
) -> None:
    """Write a private KEY=VALUE config file."""

    if path.exists() and not overwrite:
        raise FileExistsError(f"config already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    lines = [
        "# Mini Agent user configuration",
        "# Local project .env and shell variables take precedence.",
    ]
    for key, value in values.items():
        if not key.isidentifier():
            raise ValueError(f"invalid configuration key: {key}")
        if any(character in value for character in "\r\n\"'"):
            raise ValueError(f"unsupported character in configuration value: {key}")
        lines.append(f'{key}="{value}"')
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def run_init(
    console: Console,
    *,
    path: Path | None = None,
    force: bool = False,
    input_func: Callable[[str], str] | None = None,
    secret_func: Callable[[str], str] | None = None,
) -> int:
    """Run the interactive first-time configuration wizard."""

    ask = input_func or input
    ask_secret = secret_func or getpass.getpass
    destination = (path or user_config_path()).expanduser()
    if destination.exists() and not force:
        console.print(
            f"[yellow]配置已经存在：[/]{destination}\n"
            "使用 [bold]mini-agent init --force[/] 覆盖。"
        )
        return 1

    console.print("[bold bright_cyan]Mini Agent 初始化[/]\n")
    provider_names = list(PROVIDER_PRESETS)
    for index, name in enumerate(provider_names, 1):
        console.print(f"  [bright_cyan]{index}[/]  {PROVIDER_PRESETS[name].label}")
    provider_name = _choose(
        ask,
        "\n选择模型厂商 [1]：",
        provider_names,
        default_index=0,
    )
    preset = PROVIDER_PRESETS[provider_name]
    base_url = preset.base_url
    if provider_name == "custom":
        base_url = _required(ask, "API Base URL：")
    model = _required(ask, f"模型名称（例如 {preset.model_example}）：")
    api_key = _required(ask_secret, "API Key（输入不会显示）：")

    console.print("\n[bold]联网搜索（可选）[/]")
    search_names = list(SEARCH_PROVIDERS)
    for index, name in enumerate(search_names):
        console.print(f"  [bright_cyan]{index}[/]  {SEARCH_PROVIDERS[name][0]}")
    search_name = _choose(
        ask,
        "选择搜索服务 [0]：",
        search_names,
        default_index=0,
        displayed_offset=0,
    )

    values = {
        "MINI_AGENT_MODEL": model,
        "MINI_AGENT_API_KEY": api_key,
        "MINI_AGENT_BASE_URL": base_url,
    }
    search_key_name = SEARCH_PROVIDERS[search_name][1]
    if search_key_name:
        values["MINI_AGENT_SEARCH_PROVIDER"] = search_name
        values[search_key_name] = _required(
            ask_secret,
            f"{SEARCH_PROVIDERS[search_name][0]} API Key（输入不会显示）：",
        )

    try:
        write_user_config(destination, values, overwrite=force)
    except (OSError, ValueError) as exc:
        console.print(f"[red]写入配置失败：{exc}[/]")
        return 1
    console.print(f"\n[green]✓ 配置已保存：[/]{destination}")
    console.print("运行 [bold]mini-agent doctor[/] 检查配置。")
    return 0


def _required(ask: Callable[[str], str], message: str) -> str:
    while True:
        value = ask(message).strip()
        if value:
            return value


def _choose(
    ask: Callable[[str], str],
    message: str,
    values: list[str],
    *,
    default_index: int,
    displayed_offset: int = 1,
) -> str:
    while True:
        raw = ask(message).strip()
        if not raw:
            return values[default_index]
        try:
            index = int(raw) - displayed_offset
        except ValueError:
            continue
        if 0 <= index < len(values):
            return values[index]
