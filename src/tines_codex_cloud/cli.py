"""Command-line entry point for tines-codex-cloud."""

from __future__ import annotations

import argparse
import sys
from math import isfinite

from . import __version__
from .bridge import (
    CloudCommandError,
    CloudMapping,
    CloudRunner,
    build_cloud_prompt,
    check_prerequisites,
    default_cloud_state_file,
    extract_issue_reference,
    extract_tines_project,
    fetch_skill_metadata,
    load_cloud_mapping,
    read_prompt,
    required_tines_environment,
    resolve_cloud_target,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tines-codex-cloud",
        description="Submit a Tines prompt to Codex Cloud and wait for completion.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="submit and poll a Codex Cloud task")
    run_parser.add_argument(
        "--env",
        help="Codex Cloud environment ID or label (or configure it in --config)",
    )
    run_parser.add_argument(
        "--repository",
        "--repo",
        dest="repository",
        help="expected repository URL (or configure it in --config)",
    )
    run_parser.add_argument(
        "--config",
        metavar="PATH",
        help="JSON file containing Cloud defaults and Tines project mappings",
    )
    run_parser.add_argument(
        "--project",
        help="Tines project name; otherwise it is read from the issue heading",
    )
    run_parser.add_argument("--prompt-file", required=True, help="path to the Tines-generated prompt")
    run_parser.add_argument("--branch", help="optional branch for the Cloud task")
    run_parser.add_argument(
        "--model",
        help="Tines-resolved model ID to record for the Cloud task",
    )
    run_parser.add_argument(
        "--poll-interval",
        type=float,
        default=5.0,
        help="seconds between status checks (default: 5)",
    )
    run_parser.add_argument(
        "--timeout",
        type=float,
        default=30 * 60,
        help="overall seconds to wait after launch (default: 1800)",
    )
    run_parser.add_argument(
        "--status-retries",
        type=int,
        default=3,
        help="retries for transient status command failures (default: 3)",
    )
    run_parser.add_argument(
        "--retry-backoff",
        type=float,
        default=0.5,
        help="initial seconds for exponential status retry backoff (default: 0.5)",
    )
    run_parser.add_argument(
        "--state-file",
        help=(
            "path for resumable Cloud task state (default: durable runner-managed state "
            "for issue prompts; the prompt's .cloud-task.json sidecar otherwise)"
        ),
    )
    run_parser.add_argument(
        "--cancel-timeout",
        type=float,
        default=5.0,
        help="maximum seconds for best-effort cancellation (default: 5)",
    )
    run_parser.add_argument(
        "--no-cancel",
        action="store_true",
        help="leave the Cloud task running when the local wrapper times out or is interrupted",
    )

    doctor_parser = subparsers.add_parser(
        "doctor",
        help="check local Codex Cloud and Tines CLI prerequisites",
    )
    doctor_parser.add_argument(
        "--codex-binary",
        default="codex",
        help="Codex executable to check (default: codex)",
    )
    doctor_parser.add_argument(
        "--tines-binary",
        default="tines",
        help="Tines executable to check (default: tines)",
    )
    return parser


def run_command(args: argparse.Namespace) -> int:
    if not isfinite(args.poll_interval) or args.poll_interval < 0:
        raise CloudCommandError("--poll-interval must be a finite, non-negative number")
    if not isfinite(args.timeout) or args.timeout <= 0:
        raise CloudCommandError("--timeout must be a finite, positive number")
    if args.status_retries < 0:
        raise CloudCommandError("--status-retries must be a non-negative integer")
    if not isfinite(args.retry_backoff) or args.retry_backoff < 0:
        raise CloudCommandError("--retry-backoff must be a finite, non-negative number")
    if not isfinite(args.cancel_timeout) or args.cancel_timeout <= 0:
        raise CloudCommandError("--cancel-timeout must be finite and positive")
    api_url, api_key = required_tines_environment()
    original_prompt = read_prompt(args.prompt_file)
    mapping = load_cloud_mapping(args.config) if args.config else CloudMapping()
    prompt_project = extract_tines_project(original_prompt)
    if args.project is not None and prompt_project is not None and args.project != prompt_project:
        raise CloudCommandError(
            f"--project {args.project!r} conflicts with the prompt's Tines project {prompt_project!r}"
        )
    project = args.project or prompt_project
    target = resolve_cloud_target(
        mapping,
        project=project,
        runner_environment=args.env,
        runner_repository=args.repository,
        explicit_branch=args.branch,
    )
    issue_ref = extract_issue_reference(original_prompt)
    skill_metadata = (
        fetch_skill_metadata(issue_ref, forbidden_values=(api_key,))
        if issue_ref is not None
        else None
    )
    cloud_prompt = build_cloud_prompt(
        original_prompt,
        api_url,
        api_key,
        cloud_environment=target.environment,
        branch=target.base_branch,
        project=target.project,
        repository=target.repository,
        base_branch=target.base_branch,
        skill_metadata=skill_metadata,
    )
    state_file = args.state_file or default_cloud_state_file(
        args.prompt_file,
        issue_ref=issue_ref,
        environment=target.environment,
        branch=target.base_branch,
    )
    runner = CloudRunner(
        target.environment,
        target.base_branch,
        args.poll_interval,
        model=args.model,
        timeout=args.timeout,
        status_retries=args.status_retries,
        retry_backoff=args.retry_backoff,
        state_file=state_file,
        cancel_timeout=args.cancel_timeout,
        cancel_on_timeout=not args.no_cancel,
        cancel_on_interrupt=not args.no_cancel,
    )
    return runner.run(cloud_prompt, issue_ref=issue_ref)


def doctor_command(args: argparse.Namespace) -> int:
    checks = check_prerequisites(
        codex_binary=args.codex_binary,
        tines_binary=args.tines_binary,
    )
    for check in checks:
        status = "ok" if check.ok else "FAIL"
        command = " ".join(check.command)
        print(f"[{status}] {check.name}: {check.detail} ({command})")
    return 0 if all(check.ok for check in checks) else 1


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            return run_command(args)
        if args.command == "doctor":
            return doctor_command(args)
    except CloudCommandError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
