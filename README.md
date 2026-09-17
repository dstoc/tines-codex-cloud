# tines-codex-cloud

> **Experimental:** this is a proof of concept for running a Tines custom runner
> through Codex Cloud. The credential handoff is not yet suitable for production
> without the follow-up work listed below.

`tines-codex-cloud` is a small command-line bridge. It receives the prompt that
Tines generated for a local runner, submits a Codex Cloud task, and stays alive
until that task reports success or failure.

## Architecture

```text
Tines issue
    │ routing
    ▼
Tines custom runner
    │ mapping + prompt_file + TINES_API_URL + TINES_API_KEY
    ▼
tines-codex-cloud
    ├── resolves the Tines project to a Cloud target
    ├── replaces the local-only preamble with a Cloud compatibility preamble
    ├── gives bounded, on-demand guidance for loading relevant Tines skills
    ├── codex cloud exec --env <environment> [--branch <branch>] -
    └── polls codex cloud status <task-url>
             │
             ▼
        Codex Cloud environment
        (repository checkout and agent work happen here)
```

The Cloud environment owns repository selection and the checkout. The wrapper
does not clone Tines repository context locally. An optional mapping file can
select the environment and expected repository from the Tines project in the
issue prompt; the expected repository is a checkout guardrail, not a request to
the Cloud CLI to select a repository. Tines skill bodies are not copied into
the Cloud prompt or checkout; the agent fetches selected skills from the Tines
CLI only when needed.

## Prerequisites

- Python 3.11 or newer.
- The `codex` CLI installed and authenticated on the Tines runner host, with
  access to `codex cloud exec` and `codex cloud status`.
- The Tines `tines` CLI available on the runner host for bridge bookkeeping and
  for the pre-launch effective-context query. It must also be available in the
  Codex Cloud environment when the agent needs to comment, attach artifacts,
  or transition the originating issue.
- A Codex Cloud environment already configured with the intended repository.

## Setup

From a checkout of this repository:

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install .
tines-codex-cloud --help
```

### Install a GitHub Release

Releases are distributed as GitHub Release assets; this project is not
published to PyPI. For a wheel downloaded from a release:

```sh
python -m pip install ./tines_codex_cloud-0.1.0-py3-none-any.whl
```

To install directly from a GitHub Release, replace `0.1.0` with the desired
release version:

```sh
python -m pip install \
  https://github.com/dstoc/tines-codex-cloud/releases/download/v0.1.0/tines_codex_cloud-0.1.0-py3-none-any.whl
```

For a CLI-oriented installation, `pipx` is also supported:

```sh
pipx install \
  https://github.com/dstoc/tines-codex-cloud/releases/download/v0.1.0/tines_codex_cloud-0.1.0-py3-none-any.whl
```

For the supported runner-host layout, version pinning, prerequisite checks,
service PATH behavior, upgrades, and rollback, see
[docs/installation.md](docs/installation.md).

Codex authentication is intentionally outside this project. Authenticate the
`codex` CLI as the OS user that runs the Tines runner, following the Codex CLI
documentation for the deployment environment. Do not put the Tines run key in
Codex configuration or a service unit.

## CLI

```sh
tines-codex-cloud run \
  --config /etc/tines-codex-cloud/mapping.json \
  --prompt-file /path/to/prompt.md \
  --model gpt-5.6-sol \
  --poll-interval 5 \
  --timeout 1800
```

`--branch` is optional; omit it when the mapping or Cloud environment's
configured base branch should be used. For a single fixed environment, the
backwards-compatible form remains:

```sh
tines-codex-cloud run \
  --env example \
  --repository https://github.com/you/app.git \
  --prompt-file /path/to/prompt.md \
  --branch main
```

`--model` is optional and accepts the concrete model resolved by Tines for the
custom runner. The wrapper requires these environment variables:

```text
TINES_API_URL
TINES_API_KEY
```

The key is read only for prompt construction and is never printed by the
wrapper. It is not written to disk. A successful Cloud task returns exit code
0; a task in `ERROR`, a malformed Cloud response, or a local `codex` failure
returns a non-zero exit code.

The wrapper retries transient status-command failures three times with
exponential backoff, then stops at the overall timeout (30 minutes by default).
Use `--status-retries`, `--retry-backoff`, `--timeout`, `--cancel-timeout`, and
`--no-cancel` to tune that behavior. Codex versions that provide
`codex cloud cancel` are cancelled on timeout or interruption; older versions
are reported as still active.

For issue prompts, the CLI writes state in a durable runner-managed directory
by default: `$TINES_CONFIG_DIR/tines-codex-cloud/` when the Tines runner
config directory is supplied, `$XDG_STATE_HOME/tines-codex-cloud/` when that
variable is set, or `~/.local/state/tines-codex-cloud/` otherwise. Set
`TINES_CODEX_CLOUD_STATE_DIR` to choose an explicit service-account directory
and override those defaults.
The filename is a hash of the Tines issue reference, Cloud environment, and
branch, so retries from fresh `prompt.md` workspaces use the same state file.
Prompts without an issue reference retain the prompt-adjacent
`<prompt-file>.cloud-task.json` default; use `--state-file` when those tasks
also need cross-process recovery.

The state contains a fingerprint and, after a successful submission response,
the Cloud task reference. The fingerprint uses the issue reference, Cloud
environment, branch, and state-file identity, not volatile run headers,
credentials, or refreshed issue context. The bridge writes a durable
submission intent before calling `codex cloud exec` and serializes the
load/claim/submit transition for concurrent retries. If the wrapper stops while
submission is ambiguous, a retry refuses to submit a duplicate task; inspect
the provider and remove the state file only after confirming that no task was
accepted. Once a known task exists, a later run resumes status polling instead
of submitting again. Terminal tasks clear the state file.

Before installing or upgrading the bridge on a runner host, run the local
prerequisite check as the service account:

```sh
tines-codex-cloud doctor
```

This checks the installed `codex` and `tines` executables and the two Codex
Cloud subcommands without authenticating or submitting a task.

### Target mapping

The optional JSON mapping file describes the Cloud target for each Tines
project. The `defaults` object is the runner-wide configuration; a project
entry may override any default field:

```json
{
  "defaults": {
    "environment": "shared-codex-environment",
    "repository": "https://github.com/you/shared.git",
    "base_branch": "main"
  },
  "projects": {
    "billing": {
      "environment": "billing-codex-environment",
      "repository": "https://github.com/you/billing.git",
      "base_branch": "trunk"
    }
  }
}
```

The bridge extracts the project from the standard issue heading:

```text
## Issue: <project>/<number>
```

`--project` is available for prompts without that heading and must agree with
it when both are present. A configured project must resolve to an environment
and repository; a missing base branch delegates to the Cloud environment's
default.

Resolution and validation are strict:

1. The selected project entry overrides defaults for environment, repository,
   and base branch.
2. `--env` and `--repository` may fill missing mapping values, but a mismatch
   with a selected mapping fails rather than selecting an ambiguous target.
   Repository comparison ignores URL casing and a trailing `.git`.
3. An explicit `--branch` wins over the project entry and defaults and is
   passed to `codex cloud exec --branch`.
4. The expected repository is included in the Cloud prompt as a checkout
   guardrail. The Cloud environment must already be configured with it.

One Tines runner per Cloud environment is the simplest deployment: route a
project to a command with fixed `--env` and `--repository`, using the mapping
as a consistency check. Mapping-file mode is preferable when several projects
share runner capacity or use different Cloud environments; it centralizes the
decision while still rejecting runner drift.

## Tines runner configuration

Install the bridge on the runner host, then register a custom runner. The
`{prompt_file}` placeholder is expanded by Tines for each run:

```sh
tines runner install \
  --name cloud-example \
  --harness custom \
  --command '/path/to/tines-codex-cloud run --env example --repository https://github.com/you/app.git --prompt-file {prompt_file}'
```

For a foreground process, use the same flags with `tines runner daemon`:

```sh
tines runner daemon \
  --name cloud-example \
  --harness custom \
  --command '/path/to/tines-codex-cloud run --env example --repository https://github.com/you/app.git --prompt-file {prompt_file}'
```

Route this project to the runner with a project-scoped rule:

```sh
tines routing set cloud-example --project tines-codex-cloud
```

The runner daemon supplies `TINES_API_URL` and the ephemeral
`TINES_API_KEY` to the custom command. The bridge passes the prompt file's
contents to Codex Cloud rather than relying on the local Tines workspace being
available in Cloud. With `--config`, the runner command can omit `--env` and
`--repository`; the selected project mapping supplies them.

Custom runners do not have a built-in tier-to-model table, so configure the
mapping explicitly before using the `{model}` placeholder:

```sh
tines runners tiers cloud-example \
  --set smartest=gpt-6-astra \
  --set balanced=gpt-5.6-sol \
  --set cheapest=gpt-5.6-luna \
  --default balanced

tines runner install \
  --name cloud-example \
  --harness custom \
  --command '/path/to/tines-codex-cloud run \
    --env example \
    --prompt-file {prompt_file} \
    --model {model}'
```

The bridge records the `--model` value as `requested/resolved by Tines` in
safe launch diagnostics and result metadata. The current `codex cloud exec`
command does not expose a per-task model option, so the bridge does not add it
to the Cloud command or agent prompt. It records the separate delivery value
as `provider/default configuration`; Cloud continues using the model configured
by the environment or provider default until Codex supports a per-task
override.

## Run lifecycle

1. Tines launches the custom command with its generated prompt file and
   ephemeral credentials in the environment.
2. The bridge resolves the Tines project against the optional mapping and
   validates the expected repository before submitting. It then recognizes the
   Tines supervisor envelope and replaces its
   local-only authentication and workspace sections. For an issue prompt it
   runs `tines issues context <project>/<number> --json` on the runner, keeps
   only each effective skill's safe `item_id`, name, bounded description, and
   file count, and adds that compact index to the Cloud prompt. It tells the
   agent to export the supplied credentials, use the Cloud environment's
   repository, and treat local Tines paths in the original prompt as
   unavailable. Skill bodies are not embedded in the Cloud launch prompt; the
   agent fetches a selected item with
   `tines context show <context-item-id> --json`. The contract and
   issue/workflow block are preserved unchanged.
3. The bridge writes a durable, issue-scoped submission intent, then submits that
   prompt to `codex cloud exec` with the selected environment and optional
   branch. The command receives the remaining overall deadline.
4. It extracts the returned task URL, persists it, and polls
   `codex cloud status` in the foreground. Structured JSON and legacy text
   statuses are normalized. Transient command failures and malformed responses
   are retried with bounded exponential backoff; `PENDING` and other
   recognized in-progress states continue polling; `READY` succeeds and
   `ERROR` fails.
5. If the wrapper is interrupted or killed, the durable state file remains even
   when Tines deletes the failed run workspace. A known task reference can be
   resumed without a second `cloud exec`; an interrupted submission remains an
   explicit recovery stop that refuses automatic replay. Concurrent retries
   serialize the submission claim before either can call `cloud exec`.
   The bridge attempts `codex cloud cancel` on timeout or interruption when
   available, with a separate bounded cleanup timeout.
6. When the generated prompt contains an issue header and the submission
   returned a URL, the bridge best-effort attaches that URL as the
   bridge-owned `cloud-task` link artifact. It does not attach a task ID as a
   link because Tines link artifacts require an `http(s)` URL.
7. At a terminal state, the bridge best-effort adds one result comment. JSON or
   labelled status details are reduced to a bounded summary; an `ERROR` state
   without detail is reported explicitly as having no provider reason. A PR
   URL reported by Cloud is included in that comment when available.
8. The Cloud agent remains responsible for ordinary Tines progress and
   implementation-summary comments, work product artifacts (including the
   required `pr` artifact), and issue transitions using the exported
   credentials. The bridge never fabricates a diff or PR artifact and never
   transitions the issue. The task link is the durable pointer to the provider
   task; the agent's PR artifact is the reviewable code result.

## Security notes

This proof of concept puts the ephemeral Tines API URL and run key in the
Codex Cloud task prompt. The wrapper avoids printing the key and removes it
from the environment inherited by the local `codex` subprocess, but the key
can still enter task history, provider logs, model-visible tool output, or
other retained task data. Do not use this path with production credentials.

The formal threat model, option comparison, production design, and rollout gate
are in [SECURITY.md](SECURITY.md). The recommended production path is a
first-class Tines remote-runner handoff that injects a per-run credential only
at the provider's Tines egress boundary. An attested scoped relay is the
fallback when the provider cannot provide that capability; a static Cloud
environment secret is not sufficient.

## Known limitations and follow-up work

- Recognized supervisor prompts are adapted by replacing the preamble. Older
  or hand-written prompts without the `## The contract` marker use a
  conservative additive compatibility wrapper instead.
- The bridge does not yet validate that the selected Cloud environment matches
  every repository attached to the Tines issue; mapping is explicit and
  external.
- Tines skill bodies are not forwarded into Cloud. The bridge's pre-launch
  context query fails closed on an unavailable or malformed skill index; the
  Cloud task is not submitted with an unverified skill list. The Cloud
  preamble gives the agent on-demand loading guidance: select only relevant
  skills, fetch them by context item ID, and keep the selected response bounded
  to 20 files and 100 KiB of UTF-8 content. The complete launch prompt is
  bounded to 256 KiB.
- Environment, repository, and branch mapping is explicit in the optional JSON
  configuration; the Cloud environment still owns the actual repository
  checkout.
- Result bookkeeping is best effort: a Tines API/CLI outage does not change the
  Cloud task exit code, and a provider's full transcript or diff is not copied
  into Tines. Use the task URL for provider details and the agent-created PR
  artifact for the reviewable diff.
- Integration tests use the tracked `tests/fixtures/codex` executable; no real
  Codex Cloud task is created by the test suite.
- Release packaging and runner-host upgrade/rollback procedures are documented
  in [docs/installation.md](docs/installation.md).

These concerns are tracked in the Tines project as separate follow-up issues:

- Cloud task lifecycle robustness
- Tines prompt adaptation
- Repository and branch mapping
- Secure Tines credential delivery
- Result and artifact integration
- Testing with a fake `codex` executable

See [docs/prompt-adaptation.md](docs/prompt-adaptation.md) for the design
decision, resource contract, alternatives, and native Tines runner shape.

## Development

The project has no runtime dependencies. Run its test suite with:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Development installs are also supported from a source checkout:

```sh
python -m pip install .
python -m pip install -e .
```

The integration tests prepend `tests/fixtures` to `PATH`, so the fake
executable receives the same arguments and prompt stdin as the real `codex`
command without making a network request.
