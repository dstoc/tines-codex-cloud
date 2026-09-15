"""Command-line entry point for tines-codex-cloud."""

from __future__ import annotations

import argparse
import sys
from math import isfinite
from pathlib import Path

from . import __version__
from .bridge import (
    CloudCommandError,
    CloudRunner,
    build_cloud_prompt,
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
    return parser


def run_command(args: argparse.Namespace) -> int:
    if not isfinite(args.poll_interval) or args.poll_interval < 0:
        raise CloudCommandError("--poll-interval must be a finite, non-negative number")
    api_url, api_key = required_tines_environment()
    original_prompt = read_prompt(args.prompt_file)
    cloud_prompt = build_cloud_prompt(
        original_prompt,
        api_url,
        api_key,
        skill_root=Path(args.prompt_file).parent / "skills",
    )
    runner = CloudRunner(args.env, args.branch, args.poll_interval)
    return runner.run(cloud_prompt)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            return run_command(args)
    except CloudCommandError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
