from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

from tines_codex_cloud.bridge import PrerequisiteCheck, SkillMetadata
from tines_codex_cloud.cli import main


class CliTests(unittest.TestCase):
    def test_run_prefetches_skill_metadata_before_building_cloud_prompt(self) -> None:
        metadata = (SkillMetadata("ctx_review", "review-checklist", "Review it.", 1),)
        runner = Mock()
        runner.run.return_value = 0

        with patch(
            "tines_codex_cloud.cli.required_tines_environment",
            return_value=("https://tines.example", "ephemeral-key"),
        ), patch(
            "tines_codex_cloud.cli.read_prompt",
            return_value="## Issue: demo/7 — review\n",
        ), patch(
            "tines_codex_cloud.cli.fetch_skill_metadata",
            return_value=metadata,
        ) as fetch, patch(
            "tines_codex_cloud.cli.build_cloud_prompt",
            return_value="cloud prompt",
        ) as build, patch(
            "tines_codex_cloud.cli.CloudRunner",
            return_value=runner,
        ):
            status = main(
                [
                    "run",
                    "--env",
                    "example",
                    "--prompt-file",
                    "/tmp/prompt.md",
                ]
            )

        self.assertEqual(status, 0)
        fetch.assert_called_once_with("demo/7", forbidden_values=("ephemeral-key",))
        build.assert_called_once_with(
            "## Issue: demo/7 — review\n",
            "https://tines.example",
            "ephemeral-key",
            cloud_environment="example",
            branch=None,
            skill_metadata=metadata,
        )
        runner.run.assert_called_once_with("cloud prompt", issue_ref="demo/7")

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
