import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from main import build_parser, load_configuration, load_env_file


class LoadEnvFileTests(unittest.TestCase):
    def test_loads_values_and_does_not_override_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text(
                "# comment\nFIRST=value\nSECOND='from file'\n", encoding="utf-8"
            )
            with patch.dict(os.environ, {"SECOND": "from process"}, clear=True):
                load_env_file(env_file)

                self.assertEqual(os.environ["FIRST"], "value")
                self.assertEqual(os.environ["SECOND"], "from process")

    def test_rejects_invalid_variable_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text("NOT-VALID=value\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "line 1"):
                load_env_file(env_file)

    def test_project_config_takes_precedence_over_user_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            local_config = project / ".env"
            user_config = root / "user.env"
            local_config.write_text("MINI_AGENT_MODEL=local\n", encoding="utf-8")
            user_config.write_text(
                "MINI_AGENT_MODEL=user\nMINI_AGENT_API_KEY=secret\n",
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"MINI_AGENT_CONFIG": str(user_config)},
                clear=True,
            ):
                loaded = load_configuration(cwd=project)

                self.assertEqual(os.environ["MINI_AGENT_MODEL"], "local")
                self.assertEqual(os.environ["MINI_AGENT_API_KEY"], "secret")
                self.assertEqual(loaded, [local_config, user_config])

    def test_max_steps_can_come_from_environment(self) -> None:
        with patch.dict(os.environ, {"MINI_AGENT_MAX_STEPS": "75"}, clear=True):
            args = build_parser().parse_args([])

        self.assertEqual(args.max_steps, 75)


if __name__ == "__main__":
    unittest.main()
