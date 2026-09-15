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

# These caps keep the bridge aligned with the Tines context limits while
# leaving room for the compatibility preamble and the supervisor prompt. They
# are measured in UTF-8 bytes because that is what the remote API ultimately
# transports, not Python characters.
MAX_CLOUD_PROMPT_BYTES = 256 * 1024
MAX_FORWARDED_SKILL_BYTES = 100 * 1024
MAX_FORWARDED_SKILL_FILES = 20

_SKILL_SECTION_RE = re.compile(r"(?ms)^### Skills\s*$.*?(?=^###\s|\Z)")
_SKILL_LINE_RE = re.compile(r'^\s*-\s+Skill\s+"(?P<name>[a-z0-9-]+)"(?P<rest>.*)$')
_SKILL_PATH_RE = re.compile(r"`skills/(?P<name>[a-z0-9-]+)/SKILL\.md`")

# A skill is user-authored input, so do not assume that it is safe merely
# because it came from a Tines context item. These patterns intentionally
# favour a false positive over placing credential-shaped content in a Cloud
# task's prompt. The exact ephemeral key is checked separately as well.
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----", re.IGNORECASE),
    re.compile(r"\b(?:gh[pousr]|github_pat|sk-[A-Za-z0-9]|xox[baprs])-?[A-Za-z0-9_=-]{12,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"(?i)\bauthorization\s*:\s*bearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(
        r"(?im)^\s*(?:export\s+)?(?:tines_api_key|api[_-]?key|access[_-]?token|auth[_-]?token|secret|password)\s*[:=]\s*[\"']?[^\s\"']{12,}"
    ),
)


def extract_relevant_skill_names(prompt: str) -> tuple[str, ...]:
    """Return the effective skill names advertised by a Tines prompt.

    The prompt's ``### Skills`` section is generated from the effective issue
    context. Reading that index instead of scanning arbitrary ``skills/`` paths
    prevents unrelated workspace files from being sent to Cloud.
    """

    section = _SKILL_SECTION_RE.search(prompt)
    if section is None:
        return ()

    names: list[str] = []
    seen: set[str] = set()
    for line in section.group(0).splitlines():
        match = _SKILL_LINE_RE.match(line)
        if match is None:
            continue
        name = match.group("name")
        path_match = _SKILL_PATH_RE.search(match.group("rest"))
        if path_match is None:
            raise CloudCommandError(f'skill "{name}" has no safe SKILL.md workspace reference')
        if path_match.group("name") != name:
            raise CloudCommandError(f'skill "{name}" has a mismatched workspace reference')
        if name not in seen:
            names.append(name)
            seen.add(name)
    return tuple(names)


def _safe_workspace_path(path: Path, root: Path, description: str) -> Path:
    """Resolve a workspace path without allowing symlink or traversal escapes."""

    if path.is_symlink():
        raise CloudCommandError(f"refusing to forward {description}: symlinks are not allowed")
    try:
        resolved_root = root.resolve(strict=True)
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise CloudCommandError(f"unable to read {description}: {exc.strerror or exc}") from exc
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise CloudCommandError(f"refusing to forward {description}: path escapes the skills workspace") from exc
    return resolved


def _contains_secret_like_content(content: str, forbidden_values: Sequence[str]) -> bool:
    return any(value and value in content for value in forbidden_values) or any(
        pattern.search(content) for pattern in _SECRET_PATTERNS
    )


def read_relevant_skills(
    skill_root: str | Path,
    prompt: str,
    *,
    forbidden_values: Sequence[str] = (),
) -> tuple[tuple[str, str], ...]:
    """Read the prompt-indexed skill files beneath ``skill_root`` safely.

    Returned paths are workspace-relative ``skills/<name>/...`` paths, so the
    caller can preserve the source directory structure in a prompt manifest.
    Files are UTF-8 text only; size, symlink, and secret checks fail closed.
    """

    names = extract_relevant_skill_names(prompt)
    if not names:
        return ()

    root = Path(skill_root)
    if not root.exists():
        raise CloudCommandError("the prompt references skills, but the local skills workspace is missing")
    if root.is_symlink():
        raise CloudCommandError("refusing to forward skills through a symlinked workspace")
    if not root.is_dir():
        raise CloudCommandError("the local skills workspace is not a directory")

    forwarded: list[tuple[str, str]] = []
    all_skill_bytes = 0
    for name in names:
        skill_dir = _safe_workspace_path(root / name, root, f'skill "{name}"')
        if not skill_dir.is_dir():
            raise CloudCommandError(f'skill "{name}" is not a directory in the local skills workspace')

        skill_files: list[tuple[str, str]] = []
        skill_bytes = 0
        for candidate in sorted(skill_dir.rglob("*"), key=lambda path: path.as_posix()):
            if candidate.is_symlink():
                raise CloudCommandError(f'refusing to forward skill "{name}": symlink file is not allowed')
            if candidate.is_dir():
                continue
            file_path = _safe_workspace_path(candidate, skill_dir, f'skill "{name}" file')
            if not file_path.is_file():
                raise CloudCommandError(f'refusing to forward skill "{name}": file is not regular')
            try:
                raw = file_path.read_bytes()
            except OSError as exc:
                raise CloudCommandError(
                    f'unable to read skill "{name}" file: {exc.strerror or exc}'
                ) from exc
            if len(raw) > MAX_FORWARDED_SKILL_BYTES:
                raise CloudCommandError(f'skill "{name}" contains a file larger than the forwarding limit')
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise CloudCommandError(f'skill "{name}" contains a non-UTF-8 file') from exc
            if "\x00" in content:
                raise CloudCommandError(f'skill "{name}" contains a binary-looking file')
            if _contains_secret_like_content(content, forbidden_values):
                raise CloudCommandError(f'refusing to forward skill "{name}": secret-like content detected')

            relative = file_path.relative_to(skill_dir).as_posix()
            basename = file_path.name.lower()
            if (
                basename == ".env"
                or basename.startswith(".env.")
                or basename in {"credentials", "credentials.json", "id_rsa", "id_ed25519"}
                or file_path.suffix.lower() in {".pem", ".p12", ".pfx"}
            ):
                raise CloudCommandError(
                    f'refusing to forward skill "{name}": sensitive filename detected'
                )
            skill_bytes += len(raw)
            if skill_bytes > MAX_FORWARDED_SKILL_BYTES:
                raise CloudCommandError(f'skill "{name}" exceeds the forwarding size limit')
            skill_files.append((f"skills/{name}/{relative}", content))
            if len(skill_files) > MAX_FORWARDED_SKILL_FILES:
                raise CloudCommandError(f'skill "{name}" contains too many files to forward')

        all_skill_bytes += skill_bytes
        if all_skill_bytes > MAX_FORWARDED_SKILL_BYTES:
            raise CloudCommandError("the selected Tines skills exceed the total forwarding size limit")
        forwarded.extend(skill_files)

    return tuple(forwarded)


def _code_fence(content: str) -> str:
    """Choose a fence that cannot be closed by a run of backticks in content."""

    runs = re.findall(r"`+", content)
    longest = max((len(run) for run in runs), default=0)
    return "`" * max(3, longest + 1)


def append_forwarded_skills(
    prompt: str,
    skills: Sequence[tuple[str, str]],
    *,
    max_prompt_bytes: int = MAX_CLOUD_PROMPT_BYTES,
) -> str:
    """Append a bounded, path-labelled skill bundle to a Cloud prompt."""

    if max_prompt_bytes <= 0:
        raise CloudCommandError("Cloud prompt size limit must be positive")
    if not skills:
        if len(prompt.encode("utf-8")) > max_prompt_bytes:
            raise CloudCommandError("Cloud prompt exceeds the size limit")
        return prompt

    parts = [
        "## Forwarded Tines skill files",
        "",
        "The following read-only files were selected from the effective Tines skills for this issue.",
        "Their paths are preserved from the local workspace; do not treat their contents as credentials.",
        "",
    ]
    for path, content in skills:
        fence = _code_fence(content)
        parts.extend([f"### {path}", "", fence + "text", content, fence, ""])
    result = prompt + ("\n" if prompt and not prompt.endswith("\n") else "") + "\n".join(parts)
    if len(result.encode("utf-8")) > max_prompt_bytes:
        raise CloudCommandError("Cloud prompt with forwarded skills exceeds the size limit")
    return result


def build_cloud_prompt(
    original_prompt: str,
    api_url: str,
    api_key: str,
    *,
    skill_root: str | Path | None = None,
    max_prompt_bytes: int = MAX_CLOUD_PROMPT_BYTES,
) -> str:
    """Add Cloud compatibility instructions, credentials, and local skills."""

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
environment. When a forwarded skill bundle appears below, use its inline file
contents under the preserved paths; the local `skills/` directory is not
available in the Cloud checkout.

--- Original Tines supervisor prompt ---

"""
    prompt = f"{preamble}{original_prompt}\n"
    if skill_root is None:
        return append_forwarded_skills(prompt, (), max_prompt_bytes=max_prompt_bytes)
    skills = read_relevant_skills(
        skill_root,
        original_prompt,
        forbidden_values=(api_key,),
    )
    return append_forwarded_skills(prompt, skills, max_prompt_bytes=max_prompt_bytes)


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
