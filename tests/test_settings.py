import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.settings import (
    SettingsError,
    discover_project_settings,
    load_toml_settings,
    set_environment_defaults,
    user_settings_path,
)
from main import configuration_sources, load_configuration


class SettingsFileTests(unittest.TestCase):
    def write(self, directory: str, text: str, name: str = "mini-agent.toml") -> Path:
        path = Path(directory) / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_maps_top_level_keys_to_environment_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(
                directory,
                'model = "m"\n'
                'base_url = "http://localhost:8080/v1"\n'
                'api_key = "k"\n'
                "max_steps = 7\n"
                "sandbox = true\n",
            )

            self.assertEqual(
                load_toml_settings(path),
                {
                    "MINI_AGENT_MODEL": "m",
                    "MINI_AGENT_BASE_URL": "http://localhost:8080/v1",
                    "MINI_AGENT_API_KEY": "k",
                    "MINI_AGENT_MAX_STEPS": "7",
                    "MINI_AGENT_SANDBOX": "1",
                },
            )

    def test_loads_search_table(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(
                directory,
                '[search]\nprovider = "tavily"\ntavily_api_key = "tv"\n',
            )

            self.assertEqual(
                load_toml_settings(path),
                {
                    "MINI_AGENT_SEARCH_PROVIDER": "tavily",
                    "TAVILY_API_KEY": "tv",
                },
            )

    def test_rejects_unknown_setting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(directory, "unknown = 1\n")

            with self.assertRaisesRegex(SettingsError, "unknown setting 'unknown'"):
                load_toml_settings(path)

    def test_rejects_unknown_search_setting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(directory, "[search]\nwrong = 1\n")

            with self.assertRaisesRegex(SettingsError, "search.wrong"):
                load_toml_settings(path)

    def test_rejects_invalid_types(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sandbox = self.write(directory, 'sandbox = "yes"\n', name="sandbox.toml")
            with self.assertRaisesRegex(SettingsError, "must be true or false"):
                load_toml_settings(sandbox)

            steps = self.write(directory, "max_steps = 0\n", name="steps.toml")
            with self.assertRaisesRegex(SettingsError, "must be a positive integer"):
                load_toml_settings(steps)

            text = self.write(directory, 'max_rpm = "many"\n', name="rpm.toml")
            with self.assertRaisesRegex(SettingsError, "must be a positive integer"):
                load_toml_settings(text)

    def test_rejects_empty_string(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(directory, 'model = "   "\n')

            with self.assertRaisesRegex(SettingsError, "model must not be empty"):
                load_toml_settings(path)

    def test_rejects_invalid_toml(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(directory, "[broken\n")

            with self.assertRaisesRegex(SettingsError, "invalid TOML"):
                load_toml_settings(path)


class SettingsPathTests(unittest.TestCase):
    def test_user_settings_path_follows_platform_directories(self) -> None:
        self.assertEqual(
            user_settings_path(
                {"XDG_CONFIG_HOME": "/tmp/config"},
                home=Path("/home/user"),
                platform_name="posix",
            ),
            Path("/tmp/config/mini-agent/config.toml"),
        )
        self.assertEqual(
            user_settings_path(
                {"APPDATA": "C:/Users/test/AppData/Roaming"},
                platform_name="nt",
            ),
            Path("C:/Users/test/AppData/Roaming/mini-agent/config.toml"),
        )

    def test_explicit_settings_path_wins(self) -> None:
        self.assertEqual(
            user_settings_path({"MINI_AGENT_SETTINGS": "/tmp/custom.toml"}),
            Path("/tmp/custom.toml"),
        )

    def test_settings_path_follows_configured_env_file_directory(self) -> None:
        self.assertEqual(
            user_settings_path({"MINI_AGENT_CONFIG": "/tmp/agent/agent.env"}),
            Path("/tmp/agent/config.toml"),
        )

    def test_discovers_closest_project_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "mini-agent.toml").write_text('model = "root"\n', encoding="utf-8")
            nested = root / "a" / "b"
            nested.mkdir(parents=True)
            (nested / ".mini-agent.toml").write_text(
                'model = "nested"\n', encoding="utf-8"
            )

            self.assertEqual(
                discover_project_settings(nested), nested / ".mini-agent.toml"
            )
            self.assertEqual(
                discover_project_settings(root / "a"),
                root / "mini-agent.toml",
            )


class SettingsPrecedenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        # ``discover_project_settings`` resolves symlinks, and on macOS/Windows
        # the temporary directory path differ from its resolved form
        # (/var -> /private/var, 8.3 short names), so compare resolved paths.
        self.root = Path(self._temporary.name).resolve()
        self.project = self.root / "project"
        self.project.mkdir()
        self.user_env = self.root / "user.env"
        self.user_toml = self.root / "user-config.toml"

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def environment(self, **extra: str) -> dict[str, str]:
        values = {
            "MINI_AGENT_CONFIG": str(self.user_env),
            "MINI_AGENT_SETTINGS": str(self.user_toml),
        }
        values.update(extra)
        return values

    def test_project_toml_beats_user_configuration(self) -> None:
        (self.project / "mini-agent.toml").write_text(
            'model = "project-toml"\n', encoding="utf-8"
        )
        self.user_env.write_text("MINI_AGENT_MODEL=user-env\n", encoding="utf-8")
        self.user_toml.write_text(
            'model = "user-toml"\napi_key = "toml-key"\n', encoding="utf-8"
        )

        with patch.dict(os.environ, self.environment(), clear=True):
            loaded = load_configuration(cwd=self.project)

            self.assertEqual(os.environ["MINI_AGENT_MODEL"], "project-toml")
            self.assertEqual(os.environ["MINI_AGENT_API_KEY"], "toml-key")
            self.assertEqual(
                loaded,
                [
                    self.project / "mini-agent.toml",
                    self.user_env,
                    self.user_toml,
                ],
            )

    def test_project_env_beats_project_toml(self) -> None:
        (self.project / ".env").write_text(
            "MINI_AGENT_MODEL=project-env\n", encoding="utf-8"
        )
        (self.project / "mini-agent.toml").write_text(
            'model = "project-toml"\n', encoding="utf-8"
        )

        with patch.dict(os.environ, self.environment(), clear=True):
            load_configuration(cwd=self.project)

            self.assertEqual(os.environ["MINI_AGENT_MODEL"], "project-env")

    def test_user_env_beats_user_toml(self) -> None:
        self.user_env.write_text("MINI_AGENT_MODEL=user-env\n", encoding="utf-8")
        self.user_toml.write_text('model = "user-toml"\n', encoding="utf-8")

        with patch.dict(os.environ, self.environment(), clear=True):
            load_configuration(cwd=self.project)

            self.assertEqual(os.environ["MINI_AGENT_MODEL"], "user-env")

    def test_shell_environment_beats_every_file(self) -> None:
        (self.project / "mini-agent.toml").write_text(
            'model = "project-toml"\n', encoding="utf-8"
        )
        self.user_env.write_text("MINI_AGENT_MODEL=user-env\n", encoding="utf-8")

        with patch.dict(
            os.environ, self.environment(MINI_AGENT_MODEL="from-shell"), clear=True
        ):
            load_configuration(cwd=self.project)

            self.assertEqual(os.environ["MINI_AGENT_MODEL"], "from-shell")

    def test_nested_directory_uses_project_settings(self) -> None:
        (self.project / "mini-agent.toml").write_text(
            'model = "project-toml"\n', encoding="utf-8"
        )
        nested = self.project / "src" / "pkg"
        nested.mkdir(parents=True)

        with patch.dict(os.environ, self.environment(), clear=True):
            load_configuration(cwd=nested)

            self.assertEqual(os.environ["MINI_AGENT_MODEL"], "project-toml")

    def test_reports_configured_sources(self) -> None:
        with patch.dict(os.environ, self.environment(), clear=True):
            sources = configuration_sources(self.project)

        self.assertEqual(
            sources,
            [
                self.project / ".env",
                self.user_env,
                self.user_toml,
            ],
        )

    def test_set_environment_defaults_keeps_existing_values(self) -> None:
        with patch.dict(os.environ, {"MINI_AGENT_MODEL": "from-shell"}, clear=True):
            set_environment_defaults(
                {"MINI_AGENT_MODEL": "from-file", "MINI_AGENT_API_KEY": "k"}
            )

            self.assertEqual(os.environ["MINI_AGENT_MODEL"], "from-shell")
            self.assertEqual(os.environ["MINI_AGENT_API_KEY"], "k")


if __name__ == "__main__":
    unittest.main()
