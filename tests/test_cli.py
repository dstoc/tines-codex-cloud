from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from tines_codex_cloud.bridge import PrerequisiteCheck
from tines_codex_cloud.cli import main


class CliTests(unittest.TestCase):
    def test_doctor_reports_checks_and_returns_failure_when_one_is_missing(self) -> None:
        checks = [
            PrerequisiteCheck("Codex CLI", ("codex", "--version"), True, "codex 1.0"),
            PrerequisiteCheck(
                "Tines CLI",
                ("tines", "--version"),
                False,
                "executable not found on PATH",
            ),
        ]
        output = io.StringIO()

        with patch("tines_codex_cloud.cli.check_prerequisites", return_value=checks) as check:
            with redirect_stdout(output):
                status = main(
                    [
                        "doctor",
                        "--codex-binary",
                        "/opt/codex",
                        "--tines-binary",
                        "/opt/tines",
                    ]
                )

        self.assertEqual(status, 1)
        check.assert_called_once_with(codex_binary="/opt/codex", tines_binary="/opt/tines")
        self.assertIn("[ok] Codex CLI: codex 1.0", output.getvalue())
        self.assertIn("[FAIL] Tines CLI: executable not found on PATH", output.getvalue())


if __name__ == "__main__":
    unittest.main()
