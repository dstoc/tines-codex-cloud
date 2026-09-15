from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).parents[1]
FAKE_CODEX = ROOT / "tests" / "fixtures"
TASK_URL = "https://cloud.example/tasks/fake"


class FakeCodexIntegrationTests(unittest.TestCase):
    api_url = "https://tines.integration.example/api"
    api_key = "integration-secret-key"

    def run_bridge(
        self,
        scenario: str,
        *,
        branch: str | None = None,
        model: str | None = None,
        prompt: str = "original Tines prompt\n",
    ) -> tuple[subprocess.CompletedProcess[str], list[dict[str, object]]]:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            prompt_path = temporary_path / "prompt.md"
            log_path = temporary_path / "fake-codex.jsonl"
            state_path = temporary_path / "fake-codex.state"
            prompt_path.write_text(prompt, encoding="utf-8")

            environment = os.environ.copy()
            environment.update(
                {
                    "FAKE_CODEX_LOG": str(log_path),
                    "FAKE_CODEX_SCENARIO": scenario,
                    "FAKE_CODEX_STATE": str(state_path),
                    "FAKE_FAILURE_MARKER": self.api_key,
                    "PYTHONPATH": str(ROOT / "src"),
                    "TINES_API_KEY": self.api_key,
                    "TINES_API_URL": self.api_url,
                }
            )
            environment["PATH"] = os.pathsep.join(
                [str(FAKE_CODEX), environment.get("PATH", "")]
            )

            command = [
                sys.executable,
                "-m",
                "tines_codex_cloud.cli",
                "run",
                "--env",
                "integration",
                "--prompt-file",
                str(prompt_path),
                "--poll-interval",
                "0",
            ]
            if branch is not None:
                command.extend(["--branch", branch])
            if model is not None:
                command.extend(["--model", model])

            result = subprocess.run(
                command,
                cwd=ROOT,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            records = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
            ]
            return result, records

    def assert_no_credentials_in_logs(self, result: subprocess.CompletedProcess[str]) -> None:
        logs = result.stdout + result.stderr
        self.assertNotIn(self.api_url, logs)
        self.assertNotIn(self.api_key, logs)

    def test_successful_submission_passes_arguments_prompt_and_credentials_safely(self) -> None:
        result, records = self.run_bridge(
            "successful-submission",
            branch="feature/integration",
            prompt="prompt delivered through stdin\n",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.splitlines(),
            [
                "Cloud task submitted; polling until completion.",
                "Cloud task status: READY",
            ],
        )
        self.assertEqual(result.stderr, "")
        self.assert_no_credentials_in_logs(result)
        self.assertEqual(len(records), 2)

        submission, status = records
        self.assertEqual(submission["operation"], "exec")
        self.assertEqual(
            submission["args"],
            ["cloud", "exec", "--env", "integration", "--branch", "feature/integration", "-"],
        )
        submitted_prompt = str(submission["stdin"])
        self.assertIn("prompt delivered through stdin", submitted_prompt)
        self.assertIn(f"export TINES_API_URL={self.api_url}", submitted_prompt)
        self.assertIn(f"export TINES_API_KEY={self.api_key}", submitted_prompt)
        self.assertFalse(submission["tines_api_key_present"])
        self.assertEqual(submission["tines_api_url"], self.api_url)
        self.assertEqual(status["operation"], "status")
        self.assertEqual(status["args"], ["cloud", "status", TASK_URL])

    def test_resolved_model_is_diagnostic_metadata_only(self) -> None:
        result, records = self.run_bridge(
            "successful-submission",
            model="gpt-5.6-sol",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "requested/resolved by Tines: gpt-5.6-sol; delivered to Codex Cloud: provider/default configuration",
            result.stdout,
        )
        self.assertEqual(
            records[0]["args"],
            ["cloud", "exec", "--env", "integration", "-"],
        )
        self.assertNotIn("gpt-5.6-sol", str(records[0]["args"]))
        self.assertNotIn("gpt-5.6-sol", str(records[0]["stdin"]))

    def test_pending_to_ready_polls_until_ready(self) -> None:
        result, records = self.run_bridge("pending-to-ready")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.splitlines(),
            [
                "Cloud task submitted; polling until completion.",
                "Cloud task status: PENDING",
                "Cloud task status: READY",
            ],
        )
        self.assertEqual([record["operation"] for record in records], ["exec", "status", "status"])
        self.assertEqual([record["args"] for record in records[1:]], [["cloud", "status", TASK_URL]] * 2)
        self.assert_no_credentials_in_logs(result)

    def test_pending_to_error_returns_failure_after_polling(self) -> None:
        result, records = self.run_bridge("pending-to-error")

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, "")
        self.assertEqual(
            result.stdout.splitlines(),
            [
                "Cloud task submitted; polling until completion.",
                "Cloud task status: PENDING",
                "Cloud task status: ERROR",
            ],
        )
        self.assertEqual(len(records), 3)
        self.assert_no_credentials_in_logs(result)

    def test_malformed_submission_output_returns_failure_without_polling(self) -> None:
        result, records = self.run_bridge("malformed-submission")

        self.assertEqual(result.returncode, 1)
        self.assertEqual(
            result.stderr,
            "error: codex cloud exec returned no task URL or task identifier\n",
        )
        self.assertEqual(result.stdout, "")
        self.assertEqual([record["operation"] for record in records], ["exec"])
        self.assert_no_credentials_in_logs(result)

    def test_transient_status_failure_returns_failure_without_retry_or_leak(self) -> None:
        result, records = self.run_bridge("transient-status-failure")

        self.assertEqual(result.returncode, 1)
        self.assertEqual(
            result.stderr,
            "error: codex cloud status failed with exit code 17\n",
        )
        self.assertEqual(result.stdout, "Cloud task submitted; polling until completion.\n")
        self.assertEqual([record["operation"] for record in records], ["exec", "status"])
        self.assert_no_credentials_in_logs(result)

    def test_submission_process_failure_returns_failure_without_leaking_stderr(self) -> None:
        result, records = self.run_bridge("submission-process-failure")

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, "error: codex cloud exec failed with exit code 23\n")
        self.assertEqual(result.stdout, "")
        self.assertEqual([record["operation"] for record in records], ["exec"])
        self.assert_no_credentials_in_logs(result)

    def test_status_process_failure_returns_failure_without_leaking_stderr(self) -> None:
        result, records = self.run_bridge("status-process-failure")

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, "error: codex cloud status failed with exit code 29\n")
        self.assertEqual(result.stdout, "Cloud task submitted; polling until completion.\n")
        self.assertEqual([record["operation"] for record in records], ["exec", "status"])
        self.assert_no_credentials_in_logs(result)
