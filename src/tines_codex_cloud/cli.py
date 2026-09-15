"""Command-line entry point for tines-codex-cloud."""

from __future__ import annotations

import argparse
import sys
from math import isfinite

from . import __version__
from .bridge import (
    CloudCommandError,
    CloudRunner,
    build_cloud_prompt,
    check_prerequisites,
    extract_issue_reference,
    read_prompt,
    required_tines_environment,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tines-codex-cloud",
        description="Submit a Tines prompt to Codex Cloud and wait for completion.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="submit and poll a Codex Cloud task")
    run_parser.add_argument("--env", required=True, help="Codex Cloud environment ID or label")
    run_parser.add_argument("--prompt-file", required=True, help="path to the Tines-generated prompt")
    run_parser.add_argument("--branch", help="optional branch for the Cloud task")
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
    cloud_prompt = build_cloud_prompt(original_prompt, api_url, api_key)
    runner = CloudRunner(args.env, args.branch, args.poll_interval)
    return runner.run(cloud_prompt, issue_ref=extract_issue_reference(original_prompt))


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
