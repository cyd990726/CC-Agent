import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from main import load_env_file


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


if __name__ == "__main__":
    unittest.main()
