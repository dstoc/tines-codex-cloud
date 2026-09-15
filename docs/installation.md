# Installation and upgrades

This bridge is distributed as a versioned Python wheel. A standalone native
executable is deliberately not part of the release: the bridge has no runtime
dependencies, its Python package is already a `py3-none-any` artifact, and a
dedicated virtual environment gives the runner an isolated and reversible
installation without compiling platform-specific code.

## Support policy

- CPython 3.11 or newer is required. The release artifact contains Python code
  only, so both x86_64 and arm64 hosts are supported.
- The managed service instructions target Linux with a systemd user service and
  macOS with launchd, matching the service support of `tines runner install`.
- The bridge can be run in the foreground on another Python 3.11+ platform, but
  Windows is not a supported managed runner target in this runbook.
- The `codex` executable must be installed and authenticated for the OS account
  that runs the Tines service. The Tines `tines` CLI must be available to that
  account for runner management and the local prerequisite check. It must also
  be available inside the selected Cloud environment because the Cloud task
  uses it for issue comments, artifacts, and state transitions.

The bridge does not install or update either CLI. This keeps provider
authentication and the Tines runner's own CLI lifecycle under their respective
administrators.

## Build and pin a release

Build a source distribution and wheel from a tagged or otherwise reviewed
commit. The version in `pyproject.toml` is the release identity; do not deploy
an uncommitted checkout or an unpinned VCS URL.

```sh
python3 -m pip install --upgrade build
python3 -m build --sdist --wheel
sha256sum dist/*                         # macOS: shasum -a 256 dist/*
```

Copy the wheel and its recorded SHA-256 through the host's normal artifact
channel. At installation time, refer to the exact wheel filename and use
`--no-deps`; this package intentionally has no runtime dependencies:

```sh
python3 -m venv /opt/tines-codex-cloud/releases/0.1.0
/opt/tines-codex-cloud/releases/0.1.0/bin/python -m pip install \
  --no-deps /srv/artifacts/tines_codex_cloud-0.1.0-py3-none-any.whl
```

Verify the artifact's recorded digest before the `pip install` step. Keep the
wheel, version, digest, Python version, and installer identity in the host
change record.

## Layout and service PATH

Use one virtual environment per installed version and a symlink for the active
one. The old release remains available for rollback:

```text
/opt/tines-codex-cloud/
  releases/0.1.0/bin/tines-codex-cloud
  releases/0.2.0/bin/tines-codex-cloud
  current -> releases/0.2.0
```

Run the prerequisite check as the same OS user that owns the service and with
the same PATH that the service will receive:

```sh
/opt/tines-codex-cloud/current/bin/tines-codex-cloud doctor
```

`doctor` runs only `--version` and `--help` commands. It checks `codex`,
`codex cloud exec`, `codex cloud status`, and `tines`; it does not authenticate,
submit a Cloud task, or contact the Tines API. A non-zero result blocks an
install or upgrade. If either CLI is in a non-standard directory, put that
directory on the service PATH before installing the runner and rerun the check.

Register the custom command with the absolute bridge path so an interactive
shell's PATH is not required:

```sh
tines runner install \
  --name cloud-example \
  --harness custom \
  --command '/opt/tines-codex-cloud/current/bin/tines-codex-cloud run --env example --prompt-file {prompt_file}'
```

`codex` and `tines` still need to be on the service account's PATH. Install the
Tines runner only after that PATH is correct; `tines runner install` captures
the executable locations for its systemd/launchd service. Do not put
`TINES_API_KEY` in a unit file. The Tines daemon supplies a fresh run-scoped key
to the custom command, and the bridge forwards it to the Cloud task according
to the credential-handling policy documented by this project.

## Upgrade

Never run `pip install --upgrade` inside the active virtual environment. Stage
the new version independently, validate it, then change the symlink:

```sh
root=/opt/tines-codex-cloud
python3 -m venv "$root/releases/0.2.0"
"$root/releases/0.2.0/bin/python" -m pip install --no-deps \
  /srv/artifacts/tines_codex_cloud-0.2.0-py3-none-any.whl
"$root/releases/0.2.0/bin/tines-codex-cloud" --version
"$root/releases/0.2.0/bin/tines-codex-cloud" doctor
ln -s releases/0.2.0 "$root/.current-0.2.0"
mv -f "$root/.current-0.2.0" "$root/current"
tines runner restart cloud-example
```

The artifact filename should be replaced with the exact filename produced by
the build. Wait for active runs to settle before restarting when the runner
host's operational policy requires it. Keep at least the previous release
until the new version has completed a real run.

## Rollback

Rollback is a symlink change, not a rebuild. Point `current` at the retained
previous release and restart the runner:

```sh
root=/opt/tines-codex-cloud
ln -s releases/0.1.0 "$root/.current-rollback"
mv -f "$root/.current-rollback" "$root/current"
tines runner restart cloud-example
```

Record the failed version and leave its release directory intact until the
incident is understood. Remove old releases only after no service or active run
references them.
