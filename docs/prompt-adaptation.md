# Tines supervisor prompts in Codex Cloud

This document records the prompt boundary for the experimental bridge. It
addresses prompt adaptation only; credential delivery, repository mapping,
skill forwarding, lifecycle handling, result integration, and packaging remain
separate follow-up concerns.

## Decision

The Cloud adapter replaces the execution-specific part of a recognized Tines
supervisor prompt. It does not append instructions after the local preamble.
The bridge recognizes the `## The contract` heading, generates a Cloud
authentication/workspace preamble, and copies everything from that heading to
the end unchanged. That suffix contains the durable contract, project and
state instructions, available transitions, issue comments, and issue
description. Keeping it unchanged prevents a provider adapter from silently
changing workflow semantics.

The bridge still has an additive compatibility path for hand-written or older
prompts that do not contain the heading. It is intentionally not treated as
the first-class format: it preserves user content, but cannot prove that
local-only sections were removed.

Codex Cloud is the execution boundary, not a second local runner workspace.
The selected Cloud environment checks out the repository and branch. The
current directory and its `AGENTS.md` files are authoritative for code setup
and tests. A Tines `repos.json` file, local `skills/<name>/` directories, and
machine-local Git credentials must not be described as if they were present
in the Cloud container.

## Resource contract

| Resource | Local runner | Cloud bridge now | Native Tines Cloud target |
| --- | --- | --- | --- |
| Repository | Tines materializes every effective repo and branch in the run workspace. | The configured Cloud environment owns checkout; the explicit environment and optional branch are passed to `codex cloud exec`. | Resolve and validate one Cloud environment against the effective Tines repository before launch. A multi-repository issue needs an explicit future design; do not silently choose one. |
| Durable instructions | Tines prompt plus seeded skill files. | Cloud checkout `AGENTS.md` supplies repository instructions; the Tines project conventions remain in the preserved prompt. | Keep repository instructions in `AGENTS.md` and pass Tines workflow context as launch material. |
| Tines skills | Files under `skills/<name>/…`. | Skill bodies are not embedded or mounted. The Cloud preamble tells the agent to read `tines issues context <project>/<number> --json`, select relevant `skills` entries by metadata, and fetch selected items with `tines context show <context-item-id> --json`. | Keep skills available through an explicit, bounded Cloud-native mechanism. Never claim a local path exists when it does not, and do not copy unrelated or secret-like skill content into prompts or issue artifacts. |
| Credentials | Run-scoped `TINES_API_KEY` and `TINES_API_URL` are in the daemon process environment. | The bridge embeds the run key in the prompt as a temporary compatibility measure and tells the agent to export it. | Deliver the key out-of-band through a provider-supported per-task environment or egress proxy. The current prompt transport is tracked separately as a security issue and is not production-safe. |
| Issue workflow | Agent uses the Tines CLI from the local workspace. | The preserved contract tells the agent to comment, transition, attach artifacts, and hand off through Tines. | Keep these instructions provider-neutral and make the run key available without adding credentials to prompt content. |

The Cloud environment may contain setup-time variables and secrets, but a
per-run Tines key must not be treated as a persistent environment setting.
OpenAI's Cloud environment documentation says configured secrets are removed
before the agent phase, while environment variables persist for the task.
That distinction is why the bridge's current prompt handoff is explicit and
temporary rather than pretending a static environment secret solves run
authentication.

## Alternatives considered

| Approach | Result |
| --- | --- |
| Keep the local preamble and append a Cloud override | Rejected as the primary format. Contradictory claims about `repos.json`, `skills/`, and a fresh local workspace remain in the task prompt. |
| Strip text with a list of regular expressions | Rejected. Prompt wording changes would make removal incomplete or could delete issue context. |
| Replace known execution sections at the bridge boundary | Chosen for this bridge. It is small, testable, preserves the workflow suffix exactly, and has a conservative fallback for unknown prompts. |
| Make the whole supervisor preamble user-configurable | Rejected for safety-critical text. A runner adapter may select a code-owned, versioned variant; users may customize project context, not remove authentication, handoff, or transition rules. |
| Upstream a native Codex Cloud runner in Tines | Recommended long term. Tines should launch a provider adapter with structured materials instead of making a local custom runner reinterpret a prompt. |

## Native Tines shape

The upstream runner should model the boundary explicitly:

```text
LaunchEnvelope
  run: id, issue ref, timeout, resolved model/effort
  workflow: contract and current issue/state context
  repository: one validated Cloud environment plus branch/commit
  skills: named, bounded files or an explicit Cloud skill reference
  credentials: run-scoped delivery handle, never prompt text
  preamble: adapter-owned variant and version
```

The provider-specific preamble should be selected from the runner type and
included in the launch/resume compatibility fingerprint. It should not be an
arbitrary database string: changing it can alter what an agent is allowed to
do. A `codex_cloud` adapter would replace the local runner's filesystem
section, advertise the Cloud checkout, and preserve the workflow suffix. It
would also need explicit support for task cancellation, status reconciliation,
and result/PR links before replacing the bridge in production.

For the current bridge, `adapt_supervisor_prompt` is the executable form of
this decision. Its tests assert both removal of local-session claims and exact
preservation of the contract and issue block.

## References

- [Codex Cloud](https://learn.chatgpt.com/docs/cloud) — isolated tasks,
  environment selection, and reviewable diffs.
- [Cloud environments](https://learn.chatgpt.com/docs/environments/cloud-environment)
  — checkout, setup, `AGENTS.md` discovery, variables, and secret lifetime.
- [AGENTS.md](https://learn.chatgpt.com/docs/agent-configuration/agents-md) —
  repository instruction discovery and precedence.
