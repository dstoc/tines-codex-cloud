from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import Mock, patch

from tines_codex_cloud.bridge import PrerequisiteCheck, SkillMetadata
from tines_codex_cloud.cli import main


class CliTests(unittest.TestCase):
    def test_run_help_describes_issue_and_prompt_state_defaults(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as exit_result:
            main(["run", "--help"])

        self.assertEqual(exit_result.exception.code, 0)
        help_text = " ".join(output.getvalue().split())
        self.assertIn("durable runner-managed state for issue prompts", help_text)
        self.assertIn("prompt's .cloud-task.json sidecar otherwise", help_text)

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
        ) as runner_factory, patch(
            "tines_codex_cloud.cli.default_cloud_state_file",
            return_value="/var/lib/tines-codex-cloud/issue.cloud-task.json",
        ) as state_file:
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
        state_file.assert_called_once_with(
            "/tmp/prompt.md",
            issue_ref="demo/7",
            environment="example",
            branch=None,
        )
        build.assert_called_once_with(
            "## Issue: demo/7 — review\n",
            "https://tines.example",
            "ephemeral-key",
            cloud_environment="example",
            branch=None,
            project="demo",
            repository=None,
            base_branch=None,
            skill_metadata=metadata,
        )
        runner_factory.assert_called_once_with(
            "example",
            None,
            5.0,
            model=None,
            timeout=1800,
            status_retries=3,
            retry_backoff=0.5,
            state_file="/var/lib/tines-codex-cloud/issue.cloud-task.json",
            cancel_timeout=5.0,
            cancel_on_timeout=True,
            cancel_on_interrupt=True,
        )
        runner.run.assert_called_once_with("cloud prompt", issue_ref="demo/7")

    def test_run_reports_malformed_mapping_repository_as_cli_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            mapping_path = f"{directory}/mapping.json"
            with open(mapping_path, "w", encoding="utf-8") as mapping_file:
                json.dump({"defaults": {"repository": "https://[invalid/repo"}}, mapping_file)

            error = io.StringIO()
            with patch(
                "tines_codex_cloud.cli.required_tines_environment",
                return_value=("https://tines.example", "ephemeral-key"),
            ), patch(
                "tines_codex_cloud.cli.read_prompt",
                return_value="## Issue: demo/7 — review\n",
            ), redirect_stderr(error):
                status = main(
                    [
                        "run",
                        "--config",
                        mapping_path,
                        "--prompt-file",
                        f"{directory}/prompt.md",
                    ]
                )

        self.assertEqual(status, 1)
        self.assertEqual(
            error.getvalue(),
            "error: defaults.repository must be a valid repository URL\n",
        )
        self.assertNotIn("Traceback", error.getvalue())

    def test_run_accepts_tines_resolved_model_and_passes_it_to_cloud_runner(self) -> None:
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
            return_value=(),
        ), patch(
            "tines_codex_cloud.cli.build_cloud_prompt",
            return_value="cloud prompt",
        ), patch(
            "tines_codex_cloud.cli.CloudRunner",
            return_value=runner,
        ) as cloud_runner, patch(
            "tines_codex_cloud.cli.default_cloud_state_file",
            return_value="/var/lib/tines-codex-cloud/issue.cloud-task.json",
        ):
            status = main(
                [
                    "run",
                    "--env",
                    "example",
                    "--prompt-file",
                    "/tmp/prompt.md",
                    "--model",
                    "gpt-5.6-sol",
                ]
            )

        self.assertEqual(status, 0)
        cloud_runner.assert_called_once_with(
            "example",
            None,
            5.0,
            model="gpt-5.6-sol",
            timeout=1800,
            status_retries=3,
            retry_backoff=0.5,
            state_file="/var/lib/tines-codex-cloud/issue.cloud-task.json",
            cancel_timeout=5.0,
            cancel_on_timeout=True,
            cancel_on_interrupt=True,
        )

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
