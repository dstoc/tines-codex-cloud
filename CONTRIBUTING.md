# Contributing

## Commit messages

Use Conventional Commits for all commits. Release Please uses the commit
history on `main` to determine versions and changelog entries.

Examples:

```text
feat: add model forwarding
fix: handle malformed Cloud status
docs: explain runner installation
test: cover failed Cloud submission
refactor: separate prompt adaptation
ci: build release artifacts
```

Scopes may be used where useful:

```text
feat(bridge): add model option
fix(release): attach wheel to GitHub release
```

Use `feat!:` for a breaking change, or add a `BREAKING CHANGE` footer. Prefer
these types where applicable:

`feat`, `fix`, `docs`, `test`, `refactor`, `perf`, `build`, `ci`, and `chore`.

Keep commit subjects concise and imperative.

## Development

The project has no runtime Python dependencies. From a checkout, use either:

```sh
python -m pip install .
python -m pip install -e .
```

Run the dependency-free test suite with:

```sh
PYTHONPATH=src python -m unittest discover -s tests -v
```
