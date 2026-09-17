from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tines_codex_cloud.bridge import (
    CloudCommandError,
    CloudMapping,
    CloudMappingTarget,
    CloudLaunchConfiguration,
    CloudRunner,
    adapt_supervisor_prompt,
    build_cloud_prompt,
    check_prerequisites,
    extract_status,
    extract_issue_reference,
    extract_pull_request_url,
    extract_task_reference,
    extract_tines_project,
    load_cloud_mapping,
    parse_cloud_status,
    parse_skill_metadata,
    fetch_skill_metadata,
    required_tines_environment,
    resolve_cloud_target,
    SkillMetadata,
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


class ResultCodex:
    def __init__(self, status_output: str, submission: str = "Task URL: https://cloud.example/tasks/123\n") -> None:
        self.status_output = status_output
        self.submission = submission
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append((args, kwargs))
        if args[1:3] == ["cloud", "exec"]:
            return subprocess.CompletedProcess(args, 0, self.submission, "")
        return subprocess.CompletedProcess(args, 0, self.status_output, "")


class FakeTines:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, self.returncode, "", "private diagnostic")


class SkillContextCommand:
    def __init__(self, output: str, returncode: int = 0) -> None:
        self.output = output
        self.returncode = returncode
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, self.returncode, self.output, "private diagnostic")


class BridgeTests(unittest.TestCase):
    def test_launch_configuration_keeps_resolved_model_out_of_current_exec_argv(self) -> None:
        configuration = CloudLaunchConfiguration(
            environment="example",
            branch="main",
            resolved_model="gpt-5.6-sol",
        )

        self.assertEqual(
            configuration.codex_exec_arguments(),
            ["cloud", "exec", "--env", "example", "--branch", "main", "-"],
        )
        self.assertEqual(
            configuration.diagnostic_metadata(),
            {
                "cloud_environment": "example",
                "branch": "main",
                "requested_model": "gpt-5.6-sol",
                "delivered_model": "provider/default configuration",
            },
        )

    def test_launch_configuration_supports_omitting_model(self) -> None:
        configuration = CloudLaunchConfiguration(environment="example")

        self.assertIsNone(configuration.requested_model)
        self.assertIsNone(configuration.diagnostic_metadata()["requested_model"])

    def test_cloud_runner_records_model_without_adding_it_to_exec_argv(self) -> None:
        fake = FakeCodex(["READY"])
        output = io.StringIO()
        runner = CloudRunner("example", model="gpt-5.6-sol", run_command=fake, output=output)

        self.assertEqual(runner.run("prompt"), 0)
        self.assertEqual(
            fake.calls[0][0],
            ["codex", "cloud", "exec", "--env", "example", "-"],
        )
        self.assertNotIn("gpt-5.6-sol", fake.calls[0][0])
        self.assertIn(
            "requested/resolved by Tines: gpt-5.6-sol; delivered to Codex Cloud: provider/default configuration",
            output.getvalue(),
        )
        self.assertEqual(runner.launch_metadata["requested_model"], "gpt-5.6-sol")

    def test_cloud_runner_rejects_unsafe_model_diagnostic_values(self) -> None:
        with self.assertRaisesRegex(CloudCommandError, "control characters"):
            CloudRunner("example", model="gpt-5.6-sol\nnext")

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

    def test_adapt_supervisor_prompt_replaces_local_preamble_and_preserves_contract(self) -> None:
        original = """# Supervisor run

This is run arun_123 on runner "cloud-example" for issue Demo/7; it times out after 30 minutes. The Tines supervisor dispatched you to work the issue described at the end of this prompt.

## Authentication

The runner daemon supplied the Tines key in the environment.

## Workspace

Your working directory is a fresh per-run workspace containing:

- `prompt.md`
- `skills/<name>/…`
- `repos.json`

## The contract

- Comment progress on the issue.
- Transition the issue before finishing.

## Issue: Demo/7

Fix the reported behavior.
"""
        adapted = adapt_supervisor_prompt(
            original,
            "https://tines.example/api",
            "ephemeral'key",
            cloud_environment="demo-environment",
            branch="codex/demo-7",
        )
        contract_start = adapted.index("## The contract")
        original_contract_start = original.index("## The contract")

        self.assertEqual(
            adapted[contract_start:],
            original[original_contract_start:],
        )
        self.assertIn("Codex Cloud checked out the repository selected for this task", adapted)
        self.assertIn("`demo-environment`", adapted)
        self.assertIn("`codex/demo-7`", adapted)
        self.assertIn("export TINES_API_KEY='ephemeral'\"'\"'key'", adapted)
        self.assertNotIn("The runner daemon supplied the Tines key in the environment.", adapted)
        self.assertNotIn("fresh per-run workspace", adapted)
        self.assertIn("`AGENTS.md`", adapted)
        self.assertIn("tines issues context Demo/7 --json", adapted)
        self.assertIn("tines context show <context-item-id> --json", adapted)
        self.assertIn("Tines skill files are not copied into this Cloud prompt", adapted)

    def test_adapt_resumed_prompt_does_not_make_a_new_cloud_task_claim_continuity(self) -> None:
        original = """# Supervisor run (resumed)

You are resuming your own previous session. This is run arun_new on runner "cloud-example" for issue Demo/7; it times out after 30 minutes. It continues run arun_prev, whose
workspace you are still in and whose conversation you are still holding.

## The contract

- Continue the work.

## Issue: Demo/7

Fix the reported behavior.
"""
        adapted = adapt_supervisor_prompt(original, "https://tines.example", "run-key")

        self.assertTrue(adapted.startswith("# Supervisor run\n\nThis is run arun_new"))
        self.assertNotIn("resuming your own previous session", adapted)
        self.assertNotIn("workspace you are still in", adapted)
        self.assertIn("## The contract\n\n- Continue the work.", adapted)

    def test_build_cloud_prompt_contains_override_credentials_and_original_prompt(self) -> None:
        prompt = build_cloud_prompt("Tines work\n", "https://tines.example/api", "key'with-quote")

        self.assertIn("Codex Cloud task", prompt)
        self.assertIn("export TINES_API_URL=https://tines.example/api", prompt)
        self.assertIn("export TINES_API_KEY='key'\"'\"'with-quote'", prompt)
        self.assertIn("Tines work", prompt)
        self.assertIn("bridge-owned `cloud-task` link artifact", prompt)
        self.assertIn("Do not fabricate a diff or PR artifact", prompt)

    def test_build_cloud_prompt_describes_selected_target(self) -> None:
        prompt = build_cloud_prompt(
            "Tines work\n",
            "https://tines.example/api",
            "secret",
            project="billing",
            repository="https://github.com/example/billing.git",
            base_branch="main",
        )

        self.assertIn("Tines project: `billing`", prompt)
        self.assertIn("Expected repository: `https://github.com/example/billing.git`", prompt)
        self.assertIn("Selected base branch: `main`", prompt)

    def test_build_cloud_prompt_guides_on_demand_skill_loading_without_embedding_files(self) -> None:
        prompt = build_cloud_prompt(
            "## Issue: demo/7 — use the checklist\n",
            "https://tines.example/api",
            "ephemeral-key",
        )

        self.assertIn("tines issues context demo/7 --json", prompt)
        self.assertIn("tines context show <context-item-id> --json", prompt)
        self.assertIn("at most 20 selected files and 100 KiB", prompt)
        self.assertNotIn("## Forwarded Tines skill files", prompt)
        self.assertNotIn("skills/<name>/SKILL.md", prompt)

    def test_build_cloud_prompt_lists_prefetched_skill_metadata_without_file_bodies(self) -> None:
        prompt = build_cloud_prompt(
            "## Issue: demo/7 — use the checklist\n",
            "https://tines.example/api",
            "ephemeral-key",
            skill_metadata=(
                SkillMetadata(
                    "ctx_review",
                    "review-checklist",
                    "Check the implementation before review.",
                    2,
                ),
            ),
        )

        self.assertIn("tines issues context demo/7 --json", prompt)
        self.assertIn("`review-checklist`", prompt)
        self.assertIn("context item `ctx_review`", prompt)
        self.assertIn("2 file(s)", prompt)
        self.assertIn("Check the implementation before review.", prompt)
        self.assertNotIn("file body", prompt)

    def test_build_cloud_prompt_redacts_secret_like_prefetched_description(self) -> None:
        prompt = build_cloud_prompt(
            "## Issue: demo/7 — use the checklist\n",
            "https://tines.example/api",
            "ephemeral-key",
            skill_metadata=(
                SkillMetadata("ctx_unsafe", "unsafe", "api_key=ephemeral-key", 1),
            ),
        )

        self.assertIn("context item `ctx_unsafe`", prompt)
        self.assertIn("description omitted", prompt)
        self.assertNotIn("description \"api_key=ephemeral-key\"", prompt)

    def test_fetch_skill_metadata_uses_effective_context_and_discards_bodies(self) -> None:
        command = SkillContextCommand(
            json.dumps(
                {
                    "skills": [
                        {
                            "item_id": "ctx_review",
                            "name": "review-checklist",
                            "description": "Check the implementation.",
                            "files": [
                                {"path": "SKILL.md", "content": "private body"},
                                {"path": "notes/extra.txt", "content": "more body"},
                            ],
                        }
                    ],
                    "prompt": {"text": "unrelated prompt"},
                }
            )
        )
        with patch.dict(
            os.environ,
            {"TINES_API_KEY": "ephemeral-key", "TINES_API_URL": "https://tines.example"},
        ):
            metadata = fetch_skill_metadata(
                "demo/7",
                tines_binary="/opt/tines",
                run_command=command,
                forbidden_values=("ephemeral-key",),
            )

        self.assertEqual(
            metadata,
            (SkillMetadata("ctx_review", "review-checklist", "Check the implementation.", 2),),
        )
        self.assertEqual(command.calls[0][0], ["/opt/tines", "issues", "context", "demo/7", "--json"])
        self.assertIn("TINES_API_KEY", command.calls[0][1]["env"])
        self.assertNotIn("private body", repr(metadata))

    def test_parse_skill_metadata_omits_secret_like_descriptions(self) -> None:
        metadata = parse_skill_metadata(
            json.dumps(
                {
                    "skills": [
                        {
                            "item_id": "ctx_unsafe",
                            "name": "unsafe",
                            "description": "password=long-secret-value",
                            "files": [{"path": "SKILL.md", "content": "body"}],
                        }
                    ]
                }
            )
        )

        self.assertEqual(metadata[0].description, None)

    def test_fetch_skill_metadata_fails_without_leaking_cli_diagnostics(self) -> None:
        command = SkillContextCommand("private diagnostic", returncode=17)

        with self.assertRaisesRegex(CloudCommandError, "exit code 17"):
            fetch_skill_metadata("demo/7", run_command=command)
        self.assertNotIn("private diagnostic", str(command.calls))

    def test_build_cloud_prompt_enforces_the_launch_prompt_size_limit(self) -> None:
        with self.assertRaisesRegex(CloudCommandError, "exceeds the size limit"):
            build_cloud_prompt("x" * 500, "https://tines.example/api", "key", max_prompt_bytes=100)

        with self.assertRaisesRegex(CloudCommandError, "must be positive"):
            build_cloud_prompt("prompt", "https://tines.example/api", "key", max_prompt_bytes=0)

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

    def test_extract_issue_reference_uses_the_generated_issue_header(self) -> None:
        self.assertEqual(
            extract_issue_reference("preamble\n## Issue: demo/7 — A task\nnext"),
            "demo/7",
        )
        self.assertIsNone(extract_issue_reference("no issue block"))

    def test_extract_tines_project_reads_standard_issue_heading(self) -> None:
        self.assertEqual(
            extract_tines_project("intro\n## Issue: billing/42 — fix it\n"),
            "billing",
        )
        self.assertIsNone(extract_tines_project("no issue heading"))

    def test_mapping_selects_project_and_explicit_branch_has_precedence(self) -> None:
        with self.subTest("mapping file"):
            with tempfile.TemporaryDirectory() as directory:
                path = os.path.join(directory, "mapping.json")
                with open(path, "w", encoding="utf-8") as mapping_file:
                    json.dump(
                        {
                            "defaults": {
                                "environment": "shared-env",
                                "repository": "https://github.com/example/shared.git",
                                "base_branch": "main",
                            },
                            "projects": {
                                "billing": {
                                    "environment": "billing-env",
                                    "repository": "https://github.com/example/billing.git",
                                    "base_branch": "trunk",
                                }
                            },
                        },
                        mapping_file,
                    )
                mapping = load_cloud_mapping(path)

        target = resolve_cloud_target(
            mapping,
            project="billing",
            runner_environment="billing-env",
            runner_repository="https://github.com/example/billing.git",
            explicit_branch="release/next",
        )
        self.assertEqual(target.environment, "billing-env")
        self.assertEqual(target.repository, "https://github.com/example/billing.git")
        self.assertEqual(target.base_branch, "release/next")
        self.assertEqual(target.project, "billing")

    def test_mapping_rejects_runner_environment_or_repository_conflicts(self) -> None:
        mapping = CloudMapping(
            projects={
                "billing": CloudMappingTarget(
                    environment="billing-env",
                    repository="https://github.com/example/billing.git",
                )
            }
        )
        with self.assertRaisesRegex(CloudCommandError, "runner environment"):
            resolve_cloud_target(mapping, project="billing", runner_environment="other-env")
        with self.assertRaisesRegex(CloudCommandError, "runner repository"):
            resolve_cloud_target(
                mapping,
                project="billing",
                runner_repository="https://github.com/example/other.git",
            )

    def test_mapping_requires_project_selection_when_project_entries_exist(self) -> None:
        mapping = CloudMapping(
            projects={
                "billing": CloudMappingTarget(
                    environment="billing-env",
                    repository="https://github.com/example/billing.git",
                )
            }
        )
        with self.assertRaisesRegex(CloudCommandError, "project could not be determined"):
            resolve_cloud_target(mapping)

    def test_mapping_rejects_repository_credentials_and_malformed_url(self) -> None:
        cases = [
            ("https://user:secret@example.com/repo.git", "URL credentials"),
            ("https://[invalid/repo", "valid repository URL"),
        ]
        for repository, message in cases:
            with self.subTest(repository=repository), tempfile.TemporaryDirectory() as directory:
                path = os.path.join(directory, "mapping.json")
                with open(path, "w", encoding="utf-8") as mapping_file:
                    json.dump({"defaults": {"repository": repository}}, mapping_file)

                with self.assertRaisesRegex(CloudCommandError, message):
                    load_cloud_mapping(path)

    def test_parse_cloud_status_preserves_summary_error_and_pr_url(self) -> None:
        status = parse_cloud_status(
            '{"status":"READY","summary":"Tests passed","pr_url":"https://github.com/acme/app/pull/42"}'
        )

        self.assertIsNotNone(status)
        assert status is not None
        self.assertEqual(status.state, "READY")
        self.assertEqual(status.summary, "Tests passed")
        self.assertEqual(status.pull_request_url, "https://github.com/acme/app/pull/42")
        self.assertEqual(
            parse_cloud_status("status: ERROR\nreason: tests failed\n").error,
            "tests failed",
        )
        self.assertEqual(
            extract_pull_request_url("PR: https://github.com/acme/app/pull/42."),
            "https://github.com/acme/app/pull/42",
        )

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

    def test_run_records_task_link_and_terminal_summary_on_the_issue(self) -> None:
        codex = ResultCodex(
            '{"status":"READY","summary":"Tests passed","pr_url":"https://github.com/acme/app/pull/42"}'
        )
        tines = FakeTines()
        with patch.dict(
            os.environ,
            {"TINES_API_KEY": "secret", "TINES_API_URL": "https://tines.example"},
        ):
            runner = CloudRunner(
                "example",
                model="gpt-5.6-sol",
                run_command=codex,
                run_tines_command=tines,
                sleep=lambda _: None,
                output=io.StringIO(),
            )
            self.assertEqual(runner.run("## Issue: demo/7 — ship it\n"), 0)

        self.assertEqual(tines.calls[0][0], [
            "tines",
            "issues",
            "artifacts",
            "attach",
            "demo/7",
            "cloud-task",
            "--link",
            "https://cloud.example/tasks/123",
            "--title",
            "Codex Cloud task",
        ])
        comment = tines.calls[1][0][4]
        self.assertIn("completed successfully", comment)
        self.assertIn("Summary: Tests passed", comment)
        self.assertIn("https://github.com/acme/app/pull/42", comment)
        self.assertIn(
            "Model: requested/resolved by Tines: gpt-5.6-sol; delivered to Codex Cloud: provider/default configuration.",
            comment,
        )
        self.assertNotIn("secret", comment)
        self.assertEqual(tines.calls[0][1]["env"]["TINES_API_KEY"], "secret")
        self.assertNotIn("TINES_API_KEY", codex.calls[0][1]["env"])

    def test_run_records_an_explicit_failure_reason_without_changing_cloud_result(self) -> None:
        codex = ResultCodex('{"status":"ERROR","error":"tests failed"}')
        tines = FakeTines()
        with patch.dict(os.environ, {"TINES_API_KEY": "secret", "TINES_API_URL": "https://tines.example"}):
            runner = CloudRunner(
                "example",
                run_command=codex,
                run_tines_command=tines,
                output=io.StringIO(),
            )
            self.assertEqual(runner.run("## Issue: demo/7 — ship it\n"), 1)

        self.assertEqual(len(tines.calls), 2)
        self.assertIn("Reason: tests failed", tines.calls[1][0][4])

    def test_result_comment_redacts_the_ephemeral_tines_key(self) -> None:
        codex = ResultCodex('{"status":"READY","summary":"secret was not used"}')
        tines = FakeTines()
        with patch.dict(os.environ, {"TINES_API_KEY": "secret", "TINES_API_URL": "https://tines.example"}):
            runner = CloudRunner(
                "example",
                run_command=codex,
                run_tines_command=tines,
                output=io.StringIO(),
            )
            self.assertEqual(runner.run("## Issue: demo/7 — ship it\n"), 0)

        self.assertIn("[redacted] was not used", tines.calls[1][0][4])
        self.assertNotIn("secret was not used", tines.calls[1][0][4])

    def test_result_bookkeeping_is_best_effort(self) -> None:
        codex = ResultCodex('{"status":"READY","summary":"done"}')
        tines = FakeTines(returncode=9)
        output = io.StringIO()
        with patch.dict(os.environ, {"TINES_API_KEY": "secret", "TINES_API_URL": "https://tines.example"}):
            runner = CloudRunner(
                "example",
                run_command=codex,
                run_tines_command=tines,
                output=output,
            )
            self.assertEqual(runner.run("## Issue: demo/7 — ship it\n"), 0)

        self.assertEqual(len(tines.calls), 2)
        self.assertEqual(output.getvalue().count("warning:"), 2)

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
