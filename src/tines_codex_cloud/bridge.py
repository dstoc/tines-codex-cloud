"""Prompt construction, Cloud task submission, and status polling."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from math import isfinite
from pathlib import Path
from typing import TextIO


class CloudCommandError(RuntimeError):
    """Raised when the local Codex CLI cannot complete a command."""


NON_TERMINAL_STATES = {
    "CREATED",
    "EXECUTING",
    "IN_PROGRESS",
    "PENDING",
    "QUEUED",
    "RUNNING",
    "STARTING",
    "SUBMITTED",
    "WORKING",
}
TERMINAL_STATES = {"ERROR", "READY"}
KNOWN_STATES = NON_TERMINAL_STATES | TERMINAL_STATES


def build_cloud_prompt(original_prompt: str, api_url: str, api_key: str) -> str:
    """Add the Cloud compatibility instructions and Tines credentials."""

    quoted_url = shlex.quote(api_url)
    quoted_key = shlex.quote(api_key)
    preamble = f"""## tines-codex-cloud compatibility override

You are running as a Codex Cloud task launched by a Tines custom runner.
The repository checkout for this task is owned and configured by the selected
Codex Cloud environment. Work in that checkout; do not expect the local Tines
workspace, `repos.json`, or `skills/...` paths mentioned in the original prompt
to exist here.

Before using the Tines CLI, export the ephemeral credentials for this run:

```sh
export TINES_API_URL={quoted_url}
export TINES_API_KEY={quoted_key}
```

The key is scoped to this Tines run. Do not persist it, print it, commit it, or
reuse it after the run. Follow the original Tines supervisor prompt below for
the issue workflow, adapting any local-runner-only instructions to this Cloud
environment.

--- Original Tines supervisor prompt ---

"""
    return f"{preamble}{original_prompt}\n"


def required_tines_environment(environment: dict[str, str] | None = None) -> tuple[str, str]:
    """Return the Tines URL and key, failing without revealing either value."""

    values = os.environ if environment is None else environment
    api_url = values.get("TINES_API_URL", "").strip()
    api_key = values.get("TINES_API_KEY", "")
    if not api_url or not api_key:
        raise CloudCommandError("TINES_API_URL and TINES_API_KEY must be set")
    return api_url, api_key


def extract_task_reference(output: str) -> str | None:
    """Extract a Cloud task URL or explicitly labelled task identifier."""

    urls = re.findall(r"https?://[^\s]+", output)
    if urls:
        return urls[-1].rstrip(".,;:)]}>")

    labelled_id = re.findall(
        r"(?:task(?:\s+|[_-])?id|task\s+reference)\s*[:=]\s*([A-Za-z0-9][A-Za-z0-9._-]*)",
        output,
        flags=re.IGNORECASE,
    )
    return labelled_id[-1] if labelled_id else None


def extract_status(output: str) -> str | None:
    """Extract a known status from the simple text output of `codex cloud status`."""

    labelled = re.findall(
        r"[\"']?(?:status|state)[\"']?\s*[:=]\s*[\"']?([A-Za-z][A-Za-z0-9_-]*)",
        output,
        flags=re.IGNORECASE,
    )
    candidates = labelled or re.findall(
        r"\b(?:CREATED|ERROR|EXECUTING|IN_PROGRESS|PENDING|QUEUED|READY|RUNNING|STARTING|SUBMITTED|WORKING)\b",
        output,
        flags=re.IGNORECASE,
    )
    if not candidates:
        return None
    status = candidates[-1].upper()
    return status if status in KNOWN_STATES else None


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


class CloudRunner:
    """Synchronous bridge around the local ``codex`` executable."""

    def __init__(
        self,
        environment: str,
        branch: str | None = None,
        poll_interval: float = 5.0,
        *,
        codex_binary: str = "codex",
        run_command: CommandRunner = subprocess.run,
        sleep: Callable[[float], None] = time.sleep,
        output: TextIO = sys.stdout,
    ) -> None:
        if not environment.strip():
            raise CloudCommandError("Cloud environment must not be empty")
        if not isfinite(poll_interval) or poll_interval < 0:
            raise CloudCommandError("poll interval must be a finite, non-negative number")
        self.environment = environment
        self.branch = branch
        self.poll_interval = poll_interval
        self.codex_binary = codex_binary
        self.run_command = run_command
        self.sleep = sleep
        self.output = output

    def _safe_environment(self) -> dict[str, str]:
        child_environment = os.environ.copy()
        child_environment.pop("TINES_API_KEY", None)
        return child_environment

    def _execute(
        self,
        arguments: Sequence[str],
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = [self.codex_binary, *arguments]
        try:
            return self.run_command(
                command,
                input=input_text,
                text=True,
                capture_output=True,
                env=self._safe_environment(),
                check=False,
            )
        except FileNotFoundError as exc:
            raise CloudCommandError(
                f"unable to execute {self.codex_binary!r}; install and authenticate the Codex CLI"
            ) from exc
        except OSError as exc:
            raise CloudCommandError(f"unable to execute {self.codex_binary!r}: {exc.strerror or exc}") from exc

    def submit(self, prompt: str) -> str:
        arguments = ["cloud", "exec", "--env", self.environment]
        if self.branch is not None:
            arguments.extend(["--branch", self.branch])
        arguments.append("-")
        result = self._execute(arguments, input_text=prompt)
        if result.returncode != 0:
            raise CloudCommandError(f"codex cloud exec failed with exit code {result.returncode}")
        task_reference = extract_task_reference(result.stdout)
        if task_reference is None:
            raise CloudCommandError("codex cloud exec returned no task URL or task identifier")
        return task_reference

    def poll(self, task_reference: str) -> bool:
        """Poll until READY or ERROR; return whether the task succeeded."""

        while True:
            result = self._execute(["cloud", "status", task_reference])
            if result.returncode != 0:
                raise CloudCommandError(
                    f"codex cloud status failed with exit code {result.returncode}"
                )
            status = extract_status(result.stdout)
            if status is None:
                raise CloudCommandError("codex cloud status returned no recognized task status")

            print(f"Cloud task status: {status}", file=self.output, flush=True)
            if status == "READY":
                return True
            if status == "ERROR":
                return False
            self.sleep(self.poll_interval)

    def run(self, prompt: str) -> int:
        """Submit and synchronously wait for the Cloud task."""

        task_reference = self.submit(prompt)
        print("Cloud task submitted; polling until completion.", file=self.output, flush=True)
        return 0 if self.poll(task_reference) else 1


def read_prompt(path: str | Path) -> str:
    """Read a Tines prompt from disk without echoing its contents."""

    prompt_path = Path(path)
    try:
        return prompt_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CloudCommandError(f"unable to read prompt file {prompt_path}: {exc.strerror or exc}") from exc
