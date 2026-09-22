import os
import stat
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from rich.console import Console

from agent.config import run_init, user_config_path, write_user_config
from agent.diagnostics import collect_diagnostics


class UserConfigTests(unittest.TestCase):
    def test_uses_xdg_and_windows_config_directories(self) -> None:
        self.assertEqual(
            user_config_path(
                {"XDG_CONFIG_HOME": "/tmp/config"},
                home=Path("/home/user"),
                platform_name="posix",
            ),
            Path("/tmp/config/mini-agent/.env"),
        )
        self.assertEqual(
            user_config_path(
                {"APPDATA": "C:/Users/test/AppData/Roaming"},
                platform_name="nt",
            ),
            Path("C:/Users/test/AppData/Roaming/mini-agent/.env"),
        )

    def test_writes_private_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mini-agent" / ".env"
            write_user_config(
                path,
                {
                    "MINI_AGENT_MODEL": "test-model",
                    "MINI_AGENT_API_KEY": "secret",
                },
            )

            content = path.read_text(encoding="utf-8")
            self.assertIn('MINI_AGENT_MODEL="test-model"', content)
            self.assertIn('MINI_AGENT_API_KEY="secret"', content)
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_init_writes_selected_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.env"
            answers = iter(["2", "kimi-test", "0"])
            secrets = iter(["model-secret"])
            console = Console(file=StringIO(), force_terminal=False)

            result = run_init(
                console,
                path=path,
                input_func=lambda _message: next(answers),
                secret_func=lambda _message: next(secrets),
            )

            self.assertEqual(result, 0)
            content = path.read_text(encoding="utf-8")
            self.assertIn('MINI_AGENT_MODEL="kimi-test"', content)
            self.assertIn('MINI_AGENT_API_KEY="model-secret"', content)
            self.assertIn('MINI_AGENT_BASE_URL="https://api.moonshot.cn/v1"', content)


class DiagnosticTests(unittest.TestCase):
    def test_reports_missing_required_model_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {}, clear=True):
                checks = collect_diagnostics(Path(directory))

        failures = {check.name for check in checks if check.status == "fail"}
        self.assertIn("Model", failures)
        self.assertIn("API key", failures)

    def test_configured_environment_passes_required_checks(self) -> None:
        environment = {
            "MINI_AGENT_MODEL": "test-model",
            "MINI_AGENT_API_KEY": "secret",
            "MINI_AGENT_BASE_URL": "https://example.com/v1",
            "SHELL": "/bin/sh",
        }
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, environment, clear=True):
                checks = collect_diagnostics(Path(directory))

        self.assertFalse(any(check.status == "fail" for check in checks))


if __name__ == "__main__":
    unittest.main()
