# Secure Tines credential delivery

Status: decision record, 2026-09-15

This document formally assesses the credential handoff in this repository's
proof of concept and records the production design. It is intentionally about
the boundary between Tines and a hosted coding-agent provider; it is not a
claim that the current bridge is production-ready.

## Decision

The current bridge is not approved for production Tines credentials. It puts
the plaintext `TINES_API_KEY` and API URL in the input sent to `codex cloud
exec`. Shell quoting prevents a malformed key from changing the command, but
it does not prevent the value from becoming task content.

The production-acceptable design is a first-class Tines remote-runner handoff:

1. Tines remains the credential authority and mints a random, run-bound key.
2. The provider task receives only a run identity, a fixed Tines audience, and
   a credential reference. It does not receive the key in a prompt, event,
   file, command argument, or model-readable environment variable.
3. A provider vault or equivalent egress mechanism adds the key only to an
   `Authorization` header on requests to the Tines API host.
4. Tines enforces the run's allowed operations and resource subject, records
   the run identity rather than the secret, and revokes the key on end,
   cancellation, timeout, or expiry.

An attested, scoped relay is the fallback for a provider without task-scoped
egress secret injection. A short-lived exchange is acceptable only when the
provider can authenticate the task without putting another bearer capability
in model-visible prompt or tool output. A static Cloud environment secret is
not a substitute for either design.

Until the handoff is implemented and verified, this repository remains an
experimental bridge. Use synthetic/test credentials only and keep the Cloud
environment's agent network access disabled or limited to the minimum required
destinations.

## Scope and terminology

- **Run key**: the ephemeral Tines API bearer credential minted for one
  supervisor run. It is not the runner registration token or a provider API
  key.
- **Prompt plane**: prompt text, conversation/task history, initial events,
  model context, tool-call arguments, tool output, and user-visible task
  summaries.
- **Provider plane**: the hosted provider's task service, vault, proxy,
  scheduler, telemetry, support/debug surfaces, and retained task data.
- **Egress injection**: a provider-controlled component adds a secret to a
  permitted outbound request after the request leaves the agent sandbox. The
  agent must not be able to read the value before or after injection.
- **Exact subject**: the one Tines run and issue for which the credential was
  minted. Ownership of the Tines account alone is not an adequate subject for
  a high-impact run credential.

## Current proof-of-concept data flow

```text
Tines supervisor
  ├─ creates a run key and expiry
  └─ starts the local custom runner
       ├─ TINES_API_URL + TINES_API_KEY in the runner environment
       └─ tines-codex-cloud reads both values
            └─ codex cloud exec stdin contains the key in the prompt
                 ├─ task input/history/provider telemetry
                 ├─ model-visible prompt and shell instructions
                 └─ Tines API requests carrying the bearer key
```

The bridge deliberately does not print the key or pass it in the local
`codex` process environment, but that is not sufficient: the prompt is itself
an externally retained input. The compatibility preamble also tells the agent
to export the key, so a shell transcript or tool event can contain it even if
the bridge's own stdout remains clean.

The relevant implementation is `required_tines_environment` and
`build_cloud_prompt` in [bridge.py](src/tines_codex_cloud/bridge.py). The
behavior is covered by a test that verifies the prompt contains the quoted
credential. That test is evidence of the current behavior, not a security
approval.

## Threat model

### Assets

The primary asset is the run key and the authority it confers. Secondary
assets are issue descriptions and comments, context and skills, repository
contents, artifact data, transition authority, and the integrity of the audit
trail tying actions to a run.

### Trust assumptions

- Tines control-plane code, its database, and its TLS termination are trusted
  to protect the key. The database should contain only a key hash, not the
  plaintext value.
- The local runner host is trusted for the duration of a run. A compromise of
  that host is an infrastructure compromise and cannot be solved by changing
  prompt delivery alone.
- The hosted provider is trusted to operate its documented isolation and
  secret-injection boundary, but provider operators, retained task data,
  diagnostics, support/debug surfaces, and task viewers are treated as
  potential observers of prompt content unless the provider contract
  explicitly excludes them.
- Issue descriptions, comments, repository files, dependencies, and web
  content may be attacker-controlled. They are untrusted instructions and may
  attempt prompt injection or data exfiltration.
- A model is not a secret boundary. If a secret is in its context, a malicious
  instruction, tool call, or accidental command may disclose it.

### Threats and required controls

| Threat or observer | Failure mode | Required control |
| --- | --- | --- |
| Provider task history, prompt viewer, or provider log reader | A copied key is replayed after the task, or is retained beyond Tines' run window. | Never put the plaintext key in prompt-plane data. Use a task-scoped vault/relay, provider retention controls, and end-of-run deletion. |
| Model, prompt injection, malicious repository, or dependency | The agent reads a key from context/environment and sends it to an attacker-controlled endpoint. | Keep the key outside model-readable state; disable internet by default; allow only the Tines host and required methods through a provider proxy. |
| Shell/tool telemetry or diagnostic output | `export`, `echo`, `env`, error text, or a debug dump places the key in provider-visible output. | Header-only egress injection; never ask the model to export or print the key; redact sensitive headers and values in every log path. |
| Stolen task URL or task/session access | A task observer uses the prompt-carried key against Tines. | Treat task references as sensitive, require provider task authorization, and ensure the Tines key is useless outside the exact run subject. |
| Compromised or over-permissioned bridge/provider integration | A long-lived or account-wide key enables control-plane changes or unrelated issue writes. | Per-run TTL, exact audience, endpoint/method allowlist, issue binding, rate/size limits, and default-deny authorization. |
| Cancellation, timeout, worker crash, or provider retry | A key or provider credential remains usable after the run ended. | Revoke on every terminal path, sweep expired keys, cancel/archive provider tasks, and make cleanup idempotent and observable. |
| Resume/follow-up run | The predecessor's key is reused or a stale provider credential acts as the new run. | Rotate the provider-bound credential before sending the continuation; revoke the predecessor; expire retained sessions. |

## Evaluation of the four options

| Option | What it improves | Remaining exposure or limitation | Assessment |
| --- | --- | --- | --- |
| Cloud environment secret | Removes the value from the initial prompt if the provider's secret primitive is used as documented. | Current Codex Cloud documentation says ordinary environment variables last through the agent phase, while secrets are available only to setup scripts and are removed before the agent phase. Persisting a secret from setup into a file or shell profile defeats that boundary and may be copied into cached environments. It also has environment rather than run scope. | Not suitable for runtime Tines API access by itself. It becomes viable only if a provider adds task-scoped, agent-hidden egress injection—in which case it is the first-class handoff option. |
| Short-lived credential exchange endpoint | Keeps the long-lived credential in Tines and can bind issuance to a task nonce. | If the exchange handle is placed in the prompt, it is still a bearer capability that can be copied and replayed. If the exchange returns a bearer token to the agent, the exposure has merely moved. It also adds an availability and replay protocol. | Transitional option only with provider workload identity, one-time redemption, task/run binding, audience restriction, no token returned to model-visible state, and an explicit revocation path. |
| Scoped relay or proxy | Keeps the Tines bearer key server-side and can enforce host, method, path, issue, rate, body-size, and audit policy. It can support providers that have no vault API. | The relay still needs a trustworthy caller identity. A relay token in the prompt has the same basic leak shape, even if its impact is smaller. A public relay must resist replay, SSRF, log leakage, and denial of service. | Good fallback when authenticated task identity (mTLS, OIDC/workload identity, or equivalent) is available. Do not ship a bearer-in-prompt relay. |
| First-class Tines remote-runner handoff | Tines owns minting, authorization, revocation, and audit. The provider task gets no plaintext key in prompt content; the provider injects it only at the Tines egress boundary. | Requires a provider contract and adapter support for task-scoped vaults or an equivalent secretless identity. Provider retention, logs, and resume semantics still need verification. | Preferred production design. |

The companion `tines` repository already contains a reference implementation of
the preferred shape for its managed Claude runner. Its adapter creates a
per-run vault with a Tines key, limits networking to the Tines host, injects
the value as a header, and sends the prompt separately. The supervisor hashes
the key, gives it an expiry, revokes it when the run ends, and rotates the
provider credential during a resume. This is a useful implementation pattern,
not proof that an arbitrary Cloud task provider offers the same guarantees.

## Production design

### Launch and request path

The production path should have these properties:

1. The Tines supervisor creates a cryptographically random run key, stores only
   its hash plus run ID, issue ID, owner, creation time, expiry, and revocation
   state, then passes the plaintext only to the trusted provider adapter in
   memory.
2. The adapter creates or obtains a task-scoped provider credential binding.
   The binding contains a non-secret run reference, the fixed Tines API
   audience, and a short expiry. The provider task receives no plaintext
   credential in its prompt, events, metadata, files, argv, or model-readable
   environment.
3. The task uses the normal Tines CLI/API contract. A provider egress vault or
   relay adds the `Authorization` header only when the destination is the
   configured Tines host. The API URL is configured by the adapter, not
   supplied as an arbitrary prompt instruction.
4. Tines maps every authenticated request to the run. It checks both the
   allowed action and its exact issue/project subject before serving or
   mutating data. The policy is default-deny and does not accept credentials in
   query parameters, request bodies, redirects, or alternate hosts.
5. The provider task result and Tines audit events contain run ID, key ID,
   endpoint, method, status, and timing as needed for investigation, never
   the key or an unredacted `Authorization` header.

### Minimum run-key authorization

The run key should be a capability for a narrowly defined action set, not a
temporary copy of a user's normal API key. The exact list should be encoded in
server-side policy and tested. At minimum, it should cover only the reads
needed to assemble the issue prompt and the writes needed to report work:

- read the bound issue, its launch prompt, effective context, labels, and
  relevant artifacts;
- add or correct the run's own comments, journal entry, and artifacts;
- apply permitted existing labels and make the normal, validated issue
  transition required by the workflow;
- read status needed to recover a run, if recovery is part of the contract.

It must deny API-key management, runner registration and configuration,
routing, supervisor settings, project archive operations, workflow/context
administration, arbitrary project transfer, and any operation that changes the
run's own budget, runner, tier, or authorization. Reads and writes for an
unrelated issue must fail even when that issue belongs to the same Tines user.

The current Tines run-key implementation already has useful control-plane
fences and run lifecycle accounting, but its generic bearer actor is not by
itself an exact issue capability. The adapter rollout must either add the
subject checks or explicitly accept and review that residual authority before
production use.

### Revocation and cleanup

Revocation is part of correctness, not best-effort hygiene:

- revoke in the same terminal path that ends the run, including cancellation,
  timeout, launch failure, and provider error;
- retain a short expiry backstop so an undetected end cannot leave a live key;
- archive/cancel the provider task and delete its vault credential after the
  run, with an idempotent sweep for worker or provider failures;
- on resume, rotate the credential before sending any continuation and revoke
  the predecessor's authority;
- record cleanup failures as an operational alert without logging the secret.

The maximum validity window should be the configured run timeout plus a small
bounded delivery slack, not an environment lifetime or an account lifetime.

### Logging and retention contract

Before production approval, the provider and Tines operators must answer these
questions in writing:

- Can any provider employee, task viewer, support tool, trace, analytics feed,
  or retention export retrieve the secret value?
- Are initial events, prompts, tool calls, stdout/stderr, error messages,
  task summaries, and network diagnostics guaranteed not to contain it?
- Does provider caching copy secret material into a shared image, workspace,
  follow-up task, or resume session?
- Can a task's provider identity be bound to exactly one Tines run and revoked
  immediately?
- Are outbound requests restricted to the Tines host and required methods,
  and are redirect/alternate-host paths blocked?

If the answer is unknown, assume the surface can retain the value and reject
the integration for production credentials. A retention policy is not a
substitute for keeping the value out of prompt and tool data in the first
place.

## Provider-specific notes

Codex Cloud's documented environment behavior is relevant to option 1: regular
environment variables are present for setup and the agent phase, while Cloud
secrets are removed before the agent phase. The POC needs a runtime credential,
so moving the value into a setup-created file or profile would reintroduce a
model-readable secret and can interact badly with container caching.

If agent internet access is enabled, configure the narrowest possible domain
and method allowlist. Tines should be the only non-dependency destination for
the credential path, and write methods should be limited to the documented
issue-reporting API. Internet access must not be treated as an authorization
control: a prompt-injected agent can still attempt to exfiltrate any secret it
can read.

See the current [Codex Cloud environment documentation](https://learn.chatgpt.com/docs/environments/cloud-environment)
and [agent internet access guidance](https://learn.chatgpt.com/docs/cloud/internet-access)
when validating provider behavior; re-check both before implementation because
provider capabilities and retention controls can change.

## Acceptance tests and rollout gate

The implementation is not production-ready until all of the following are
demonstrated with a canary run key and a fake/intercepting provider:

- the exact secret is absent from the task prompt, initial events, metadata,
  task URL, files, argv, environment visible to the agent, stdout/stderr,
  status responses, and Tines/Cloud logs;
- a request to an allowed Tines endpoint succeeds with the secret injected
  only in the outbound header;
- a request to another host, method, path, issue, project, control-plane
  surface, or redirect is denied and does not leak the key;
- prompt injection that runs `env`, searches the workspace, or posts to an
  attacker-controlled endpoint cannot obtain the key;
- terminal, timeout, cancellation, launch failure, worker crash recovery, and
  provider retry cases revoke the Tines key and clean provider credentials;
- resume rotates the credential before the first continuation request and the
  old credential fails;
- audit records identify the run and action without storing bearer material;
- provider documentation or a contractual test confirms secret isolation from
  prompt history, tool telemetry, support surfaces, caches, and retained
  follow-ups;
- the Cloud environment has no unrestricted agent network access on the
  credential-bearing path.

Rollout should proceed in this order:

1. Keep the current bridge disabled for real credentials and rotate any key
   that has ever been sent through a Cloud prompt.
2. Add the Tines-side adapter/relay and exact run-subject authorization.
3. Add the provider interception, revocation, resume, and prompt-injection
   tests above.
4. Run a non-production canary and inspect provider task history, logs,
   diagnostics, cache behavior, and Tines audit data.
5. Deprecate prompt-carried credentials and make the bridge fail closed for
   production configuration. Preserve an explicitly marked local/test mode
   only if it cannot be selected by production configuration.

## Evidence reviewed

- This repository's [bridge implementation](src/tines_codex_cloud/bridge.py),
  [README](README.md), and [tests](tests/test_bridge.py).
- The companion Tines supervisor's [run-key lifecycle](https://github.com/tbuckley/tines/blob/main/apps/web/src/lib/server/supervisor/engine.ts),
  [managed provider handoff](https://github.com/tbuckley/tines/blob/main/apps/web/src/lib/server/supervisor/claude-adapter.ts),
  and [managed launch preamble](https://github.com/tbuckley/tines/blob/main/apps/web/src/lib/server/supervisor/preamble.ts).
- Codex Cloud's provider documentation linked above, reviewed on 2026-09-15.
