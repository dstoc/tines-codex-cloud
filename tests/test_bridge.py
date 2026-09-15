from __future__ import annotations

import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tines_codex_cloud.bridge import (
    CloudCommandError,
    CloudRunner,
    append_forwarded_skills,
    build_cloud_prompt,
    extract_relevant_skill_names,
    extract_status,
    extract_task_reference,
    read_relevant_skills,
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
    def test_build_cloud_prompt_contains_override_credentials_and_original_prompt(self) -> None:
        prompt = build_cloud_prompt("Tines work\n", "https://tines.example/api", "key'with-quote")

        self.assertIn("Codex Cloud task", prompt)
        self.assertIn("export TINES_API_URL=https://tines.example/api", prompt)
        self.assertIn("export TINES_API_KEY='key'\"'\"'with-quote'", prompt)
        self.assertIn("Tines work", prompt)

    def test_extract_relevant_skill_names_uses_the_generated_skill_index(self) -> None:
        prompt = """### Skills

- Skill "review-checklist" (issue demo/1): read `skills/review-checklist/SKILL.md` when this applies.
- Skill "release" (project Demo): read `skills/release/SKILL.md` when this applies.

The issue can mention skills/review-checklist/SKILL.md in ordinary prose too.
"""

        self.assertEqual(
            extract_relevant_skill_names(prompt),
            ("review-checklist", "release"),
        )

    def test_build_cloud_prompt_forwards_selected_skill_files_with_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "skills"
            (root / "review-checklist" / "nested").mkdir(parents=True)
            (root / "unrelated").mkdir()
            (root / "review-checklist" / "SKILL.md").write_text("Review safely.\n", encoding="utf-8")
            (root / "review-checklist" / "nested" / "guide.txt").write_text(
                "Nested guide\n", encoding="utf-8"
            )
            (root / "unrelated" / "SKILL.md").write_text("Do not include me\n", encoding="utf-8")
            original = (
                "### Skills\n\n"
                '- Skill "review-checklist" (issue demo/1): read `skills/review-checklist/SKILL.md` '
                "when this applies.\n"
            )

            prompt = build_cloud_prompt(
                original,
                "https://tines.example/api",
                "ephemeral-key",
                skill_root=root,
            )

        self.assertIn("## Forwarded Tines skill files", prompt)
        self.assertIn("### skills/review-checklist/SKILL.md", prompt)
        self.assertIn("### skills/review-checklist/nested/guide.txt", prompt)
        self.assertIn("Review safely.", prompt)
        self.assertIn("Nested guide", prompt)
        self.assertNotIn("Do not include me", prompt)
        self.assertLess(prompt.index(original), prompt.index("## Forwarded Tines skill files"))

    def test_read_relevant_skills_fails_closed_on_secret_like_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "skills" / "unsafe"
            root.mkdir(parents=True)
            (root / "SKILL.md").write_text("export API_KEY=super-secret-value\n", encoding="utf-8")
            prompt = '### Skills\n\n- Skill "unsafe" (issue demo/1): read `skills/unsafe/SKILL.md`.'

            with self.assertRaisesRegex(CloudCommandError, "secret-like content"):
                read_relevant_skills(Path(temporary) / "skills", prompt)

    def test_build_cloud_prompt_enforces_the_cloud_prompt_size_limit(self) -> None:
        with self.assertRaisesRegex(CloudCommandError, "exceeds the size limit"):
            append_forwarded_skills("prompt", (("skills/a/SKILL.md", "content"),), max_prompt_bytes=10)

    def test_missing_skills_directory_is_only_an_error_when_prompt_references_one(self) -> None:
        prompt = build_cloud_prompt(
            "No skill applies.\n",
            "https://tines.example/api",
            "ephemeral-key",
            skill_root=Path("/path/that/does/not/exist"),
        )
        self.assertNotIn("Forwarded Tines skill files", prompt)

    def test_read_relevant_skills_preserves_file_bytes_as_utf8_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "skills" / "docs"
            root.mkdir(parents=True)
            content = "accent: café\n"
            (root / "SKILL.md").write_text(content, encoding="utf-8")
            prompt = '### Skills\n\n- Skill "docs" (issue demo/1): read `skills/docs/SKILL.md`.'

            skills = read_relevant_skills(Path(temporary) / "skills", prompt)

        self.assertEqual(skills, (("skills/docs/SKILL.md", content),))

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
