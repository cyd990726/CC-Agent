"""TOML settings files layered with .env files and shell variables.

User settings live next to the existing ``.env`` file, for example
``~/.config/mini-agent/config.toml`` on Linux or
``%APPDATA%\\mini-agent\\config.toml`` on Windows. Project settings use the
closest ``mini-agent.toml`` (or ``.mini-agent.toml``) found by walking up from
the working directory, so running the agent from a nested folder still picks
up the project configuration.

Every value is mapped onto the ``MINI_AGENT_*`` environment variables that the
rest of the runtime already reads, which keeps the loader small and leaves the
existing ``.env`` workflow untouched.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agent.config import user_config_path

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]


PROJECT_SETTINGS_NAMES = ("mini-agent.toml", ".mini-agent.toml")
USER_SETTINGS_NAME = "config.toml"

# TOML key -> environment variable consumed by the rest of the runtime.
TOP_LEVEL_KEYS: dict[str, str] = {
    "model": "MINI_AGENT_MODEL",
    "base_url": "MINI_AGENT_BASE_URL",
    "api_key": "MINI_AGENT_API_KEY",
    "max_steps": "MINI_AGENT_MAX_STEPS",
    "max_rpm": "MINI_AGENT_MAX_RPM",
    "search_provider": "MINI_AGENT_SEARCH_PROVIDER",
    "sandbox": "MINI_AGENT_SANDBOX",
}

SEARCH_KEYS: dict[str, str] = {
    "provider": "MINI_AGENT_SEARCH_PROVIDER",
    "tavily_api_key": "TAVILY_API_KEY",
    "brave_api_key": "BRAVE_SEARCH_API_KEY",
    "serper_api_key": "SERPER_API_KEY",
}

BOOLEAN_KEYS = frozenset({"MINI_AGENT_SANDBOX"})
POSITIVE_INTEGER_KEYS = frozenset({"MINI_AGENT_MAX_STEPS", "MINI_AGENT_MAX_RPM"})


class SettingsError(ValueError):
    """Raised when a TOML settings file cannot be read or validated."""


def user_settings_path(
    environ: Mapping[str, str] | None = None,
    *,
    home: Path | None = None,
    platform_name: str | None = None,
) -> Path:
    """Return the per-user TOML settings path without creating it."""

    env = os.environ if environ is None else environ
    explicit = env.get("MINI_AGENT_SETTINGS")
    if explicit:
        return Path(explicit).expanduser()
    return user_config_path(
        environ, home=home, platform_name=platform_name
    ).with_name(USER_SETTINGS_NAME)


def discover_project_settings(start: Path) -> Path | None:
    """Return the closest project settings file at or above ``start``."""

    directory = Path(start).expanduser().resolve()
    for candidate_directory in (directory, *directory.parents):
        for name in PROJECT_SETTINGS_NAMES:
            candidate = candidate_directory / name
            if candidate.is_file():
                return candidate
    return None


def load_toml_settings(path: Path) -> dict[str, str]:
    """Parse ``path`` into environment-style key/value defaults."""

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SettingsError(f"{path}: cannot read settings: {exc}") from exc
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise SettingsError(f"{path}: invalid TOML: {exc}") from exc
    values: dict[str, str] = {}
    _collect(data, values, path=path, table="")
    return values


def set_environment_defaults(values: Mapping[str, str]) -> None:
    """Apply values without overriding anything already in the environment."""

    for key, value in values.items():
        os.environ.setdefault(key, value)


def _collect(
    data: Mapping[str, Any],
    values: dict[str, str],
    *,
    path: Path,
    table: str,
) -> None:
    keys = SEARCH_KEYS if table == "search" else TOP_LEVEL_KEYS
    for key, value in data.items():
        if table == "" and key == "search":
            if not isinstance(value, Mapping):
                raise SettingsError(f"{path}: [search] must be a table")
            _collect(value, values, path=path, table="search")
            continue
        display = f"{table}.{key}" if table else key
        env_name = keys.get(key)
        if env_name is None:
            raise SettingsError(f"{path}: unknown setting {display!r}")
        _validate(env_name, value, display=display, path=path)
        values[env_name] = _render(value, display=display, path=path)


def _render(value: Any, *, display: str, path: Path) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise SettingsError(f"{path}: {display} must not be empty")
        return text
    raise SettingsError(f"{path}: {display} must be a string, number, or boolean")


def _validate(env_name: str, value: Any, *, display: str, path: Path) -> None:
    if env_name in BOOLEAN_KEYS and not isinstance(value, bool):
        raise SettingsError(f"{path}: {display} must be true or false")
    if env_name in POSITIVE_INTEGER_KEYS and (
        isinstance(value, bool) or not isinstance(value, int) or value < 1
    ):
        raise SettingsError(f"{path}: {display} must be a positive integer")
