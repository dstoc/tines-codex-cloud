from __future__ import annotations

import io
import os
import subprocess
import unittest
from unittest.mock import patch

from tines_codex_cloud.bridge import (
    CloudCommandError,
    CloudRunner,
    build_cloud_prompt,
    check_prerequisites,
    extract_status,
    extract_task_reference,
    required_tines_environment,
)


class FakeCodex:
    def __init__(self, statuses: list[str], submission: str = "Task URL: https://cloud.example/tasks/123\n") -> None:
        self.statuses = iter(statuses)
        self.submission = submission
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append((args, kwargs))
        if args[1:3] == ["cloud", "exec"]:
            return subprocess.CompletedProcess(args, 0, self.submission, "")
        return subprocess.CompletedProcess(args, 0, f"status: {next(self.statuses)}\n", "")


class BridgeTests(unittest.TestCase):
    def test_check_prerequisites_runs_only_non_mutating_version_and_help_commands(self) -> None:
        calls: list[list[str]] = []

        def command(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(args)
            self.assertNotIn("TINES_API_KEY", kwargs["env"])
            self.assertNotIn("TINES_API_URL", kwargs["env"])
            return subprocess.CompletedProcess(args, 0, "tool 1.2.3\n", "")

        with patch.dict(
            os.environ,
            {"TINES_API_KEY": "secret", "TINES_API_URL": "https://tines.example"},
        ):
            checks = check_prerequisites(
                codex_binary="/opt/codex",
                tines_binary="/opt/tines",
                run_command=command,
            )

        self.assertTrue(all(check.ok for check in checks))
        self.assertEqual(
            calls,
            [
                ["/opt/codex", "--version"],
                ["/opt/codex", "cloud", "exec", "--help"],
                ["/opt/codex", "cloud", "status", "--help"],
                ["/opt/tines", "--version"],
            ],
        )

    def test_check_prerequisites_reports_missing_executable_and_nonzero_help(self) -> None:
        def command(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if args[0] == "tines":
                raise FileNotFoundError(args[0])
            if args[2:4] == ["status", "--help"]:
                return subprocess.CompletedProcess(args, 2, "", "unsupported")
            return subprocess.CompletedProcess(args, 0, "ok\n", "")

        checks = check_prerequisites(run_command=command)

        self.assertTrue(checks[0].ok)
        self.assertTrue(checks[1].ok)
        self.assertFalse(checks[2].ok)
        self.assertIn("exit code 2", checks[2].detail)
        self.assertFalse(checks[3].ok)
        self.assertEqual(checks[3].detail, "executable not found on PATH")

    def test_build_cloud_prompt_contains_override_credentials_and_original_prompt(self) -> None:
        prompt = build_cloud_prompt("Tines work\n", "https://tines.example/api", "key'with-quote")

        self.assertIn("Codex Cloud task", prompt)
        self.assertIn("export TINES_API_URL=https://tines.example/api", prompt)
        self.assertIn("export TINES_API_KEY='key'\"'\"'with-quote'", prompt)
        self.assertIn("Tines work", prompt)

    def test_required_environment_does_not_accept_missing_values(self) -> None:
        with self.assertRaisesRegex(CloudCommandError, "must be set"):
            required_tines_environment({"TINES_API_URL": "https://tines.example"})

    def test_extract_task_reference_accepts_url_and_labelled_id(self) -> None:
        self.assertEqual(
            extract_task_reference("submitted https://cloud.example/tasks/123.\n"),
            "https://cloud.example/tasks/123",
        )
        self.assertEqual(extract_task_reference("Task ID: task_123\n"), "task_123")
        self.assertIsNone(extract_task_reference("submission failed"))

    def test_extract_status_handles_labelled_and_standalone_output(self) -> None:
        self.assertEqual(extract_status('{"status": "PENDING"}'), "PENDING")
        self.assertEqual(extract_status("READY\n"), "READY")
        self.assertIsNone(extract_status("still processing"))

    def test_submit_uses_argument_api_and_does_not_pass_tines_key_to_codex(self) -> None:
        fake = FakeCodex([])
        with patch.dict(os.environ, {"TINES_API_KEY": "secret", "TINES_API_URL": "https://tines.example"}):
            runner = CloudRunner("example", "main", run_command=fake)
            task_reference = runner.submit("prompt containing secret")

        self.assertEqual(task_reference, "https://cloud.example/tasks/123")
        args, kwargs = fake.calls[0]
        self.assertEqual(args, ["codex", "cloud", "exec", "--env", "example", "--branch", "main", "-"])
        self.assertEqual(kwargs["input"], "prompt containing secret")
        self.assertNotIn("TINES_API_KEY", kwargs["env"])

    def test_poll_waits_through_pending_and_succeeds_at_ready(self) -> None:
        fake = FakeCodex(["PENDING", "RUNNING", "READY"])
        sleeps: list[float] = []
        output = io.StringIO()
        runner = CloudRunner("example", poll_interval=0.25, run_command=fake, sleep=sleeps.append, output=output)

        self.assertTrue(runner.poll("https://cloud.example/tasks/123"))
        self.assertEqual(sleeps, [0.25, 0.25])
        self.assertEqual(
            output.getvalue().splitlines(),
            [
                "Cloud task status: PENDING",
                "Cloud task status: RUNNING",
                "Cloud task status: READY",
            ],
        )

    def test_poll_returns_false_for_error(self) -> None:
        fake = FakeCodex(["ERROR"])
        runner = CloudRunner(
            "example",
            run_command=fake,
            sleep=lambda _: self.fail("should not sleep"),
            output=io.StringIO(),
        )

        self.assertFalse(runner.poll("task_123"))

    def test_poll_propagates_codex_status_failure_without_logging_stderr(self) -> None:
        def failed_command(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args, 7, "", "contains no useful public detail")

        runner = CloudRunner("example", run_command=failed_command, output=io.StringIO())

        with self.assertRaisesRegex(CloudCommandError, "status failed with exit code 7"):
            runner.poll("task_123")

    def test_run_returns_process_style_exit_code_without_logging_prompt(self) -> None:
        fake = FakeCodex(["READY"])
        output = io.StringIO()
        runner = CloudRunner("example", run_command=fake, output=output)

        self.assertEqual(runner.run("prompt contains an ephemeral secret"), 0)
        self.assertNotIn("ephemeral secret", output.getvalue())
        self.assertIn("Cloud task status: READY", output.getvalue())

    def test_poll_rejects_malformed_status(self) -> None:
        fake = FakeCodex([])
        fake.statuses = iter(["not-a-state"])
        runner = CloudRunner("example", run_command=fake)

        with self.assertRaisesRegex(CloudCommandError, "no recognized task status"):
            runner.poll("task_123")

    def test_submit_rejects_malformed_submission(self) -> None:
        fake = FakeCodex([], submission="submitted\n")
        runner = CloudRunner("example", run_command=fake)

        with self.assertRaisesRegex(CloudCommandError, "no task URL"):
            runner.submit("prompt")
