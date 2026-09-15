# tines-codex-cloud

> **Experimental:** this is a proof of concept for running a Tines custom runner
> through Codex Cloud. The credential handoff and lifecycle handling are not yet
> suitable for production without the follow-up work listed below.

`tines-codex-cloud` is a small command-line bridge. It receives the prompt that
Tines generated for a local runner, submits a Codex Cloud task, and stays alive
until that task reports success or failure.

## Architecture

```text
Tines issue
    │ routing
    ▼
Tines custom runner
    │ prompt_file + TINES_API_URL + TINES_API_KEY
    ▼
tines-codex-cloud
    ├── adds a Cloud compatibility preamble
    ├── forwards effective skills from skills/<name>/ when advertised by the prompt
    ├── codex cloud exec --env <environment> [--branch <branch>] -
    └── polls codex cloud status <task-url>
             │
             ▼
        Codex Cloud environment
        (repository checkout and agent work happen here)
```

The Cloud environment owns repository selection and the checkout. The wrapper
does not clone Tines repository context locally and does not infer an
environment from the Tines prompt. The local runner workspace is still used as
a read-only source for the skill files materialized by Tines.

## Prerequisites

- Python 3.11 or newer.
- The `codex` CLI installed and authenticated on the Tines runner host, with
  access to `codex cloud exec` and `codex cloud status`.
- The Tines `tines` CLI available to the Codex Cloud agent when the agent needs
  to comment, attach artifacts, or transition the originating issue.
- A Codex Cloud environment already configured with the intended repository.

## Setup

From a checkout of this repository:

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install .
tines-codex-cloud --help
```

Codex authentication is intentionally outside this project. Authenticate the
`codex` CLI as the OS user that runs the Tines runner, following the Codex CLI
documentation for the deployment environment. Do not put the Tines run key in
Codex configuration or a service unit.

## CLI

```sh
tines-codex-cloud run \
  --env example \
  --prompt-file /path/to/prompt.md \
  --branch main \
  --poll-interval 5
```

`--branch` is optional; omit it when the Cloud environment's configured base
branch should be used. The wrapper requires these environment variables:

```text
TINES_API_URL
TINES_API_KEY
```

The key is read only for prompt construction and is never printed by the
wrapper. It is not written to disk. A successful Cloud task returns exit code
0; a task in `ERROR`, a malformed Cloud response, or a local `codex` failure
returns a non-zero exit code.

## Tines runner configuration

Install the bridge on the runner host, then register a custom runner. The
`{prompt_file}` placeholder is expanded by Tines for each run:

```sh
tines runner install \
  --name cloud-example \
  --harness custom \
  --command '/path/to/tines-codex-cloud run --env example --prompt-file {prompt_file}'
```

For a foreground process, use the same flags with `tines runner daemon`:

```sh
tines runner daemon \
  --name cloud-example \
  --harness custom \
  --command '/path/to/tines-codex-cloud run --env example --prompt-file {prompt_file}'
```

Route this project to the runner with a project-scoped rule:

```sh
tines routing set cloud-example --project tines-codex-cloud
```

The runner daemon supplies `TINES_API_URL` and the ephemeral
`TINES_API_KEY` to the custom command. The bridge passes the prompt file's
contents to Codex Cloud rather than relying on the local Tines workspace being
available in Cloud.

## Run lifecycle

1. Tines launches the custom command with its generated prompt file and
   ephemeral credentials in the environment.
2. The bridge prepends a Cloud-specific compatibility override. It tells the
   agent to export the supplied credentials and use the Cloud environment's
   repository. It reads only the skills named in the prompt's generated
   `### Skills` index, then appends their UTF-8 files under their original
   `skills/<name>/...` paths.
3. The bridge submits that prompt to `codex cloud exec` with the selected
   environment and optional branch.
4. It extracts the returned task URL and polls `codex cloud status` in the
   foreground. `PENDING` and other recognized in-progress states continue
   polling; `READY` succeeds and `ERROR` fails.
5. The Cloud agent remains responsible for ordinary Tines comments, artifacts,
   and issue transitions using the exported credentials.

## Security notes

The initial proof of concept puts the ephemeral Tines API URL and run key in
the Codex Cloud task prompt. This is deliberate so the remote agent can use
the `tines` CLI, but it means the key may be visible to the Cloud task's prompt
and retained in Cloud task history or provider logs. Treat the selected Cloud
environment and repository as trusted, minimize access granted by the Tines
key, and never reuse the key after the Tines run ends. The wrapper avoids
printing the key and removes it from the environment inherited by the local
`codex` subprocess, but this does not remove the prompt exposure.

## Known limitations and follow-up work

- Status parsing is intentionally simple text matching; transient status
  failures, retries, timeouts, cancellation, and killed-wrapper recovery are
  not implemented.
- The local Tines supervisor prompt is only prefixed with an override; local
  runner-only instructions are still present.
- Skill forwarding is bounded to 20 files and 100 KiB of selected skill data;
  the complete Cloud prompt is bounded to 256 KiB. Paths must remain inside
  the local skills workspace, files must be UTF-8 text, and secret-like
  content causes a fail-closed error.
- Environment, repository, and branch mapping is configured outside the
  wrapper.
- The wrapper does not yet attach the Cloud task URL or a Cloud result summary
  to the Tines issue.
- Integration tests use mocked command execution; no real Codex Cloud task is
  created by the test suite.
- Installation and upgrade packaging is only a basic Python package.

These concerns are tracked in the Tines project as separate follow-up issues:

- Cloud task lifecycle robustness
- Tines prompt adaptation
- Skill forwarding
- Repository and branch mapping
- Secure Tines credential delivery
- Result and artifact integration
- Testing with a fake `codex` executable
- Packaging and installation

## Development

The project has no runtime dependencies. Run its test suite with:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
```
