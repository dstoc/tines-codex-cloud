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
    runner = CloudRunner(
        target.environment,
        target.base_branch,
        args.poll_interval,
        model=args.model,
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
