# Harness adapter follow-ups

* Status: Findings and requirements for future design and implementation
* Date: 2026-11-03
* Scope: shared behavior across adapters, including Pi, Codex, Claude, and AGY.
  Muse was excluded from the earlier review; the user's mode rule applies to
  every adapter when implemented.

## Direction

Claive adapts each harness to a shared worker lifecycle. Preserve useful native
features. Normalize a capability where the underlying behavior supports it.
When a feature cannot be provided, report that limit. When a native behavior
differs, describe the difference rather than pretending it is equivalent.

This document records the user's decisions and the light review. It is not an
implementation plan. No production changes are authorized by this document.

## 1. Read-only requests across all adapters

### Decision

For every harness, a read-only request uses verified native enforcement when
available. Otherwise, Claive falls back to **soft read mode**: an explicit
instruction to read and not write. The agent is expected to follow that
instruction, but the instruction is not an enforced restriction. Claive must
report the actual mode. Pi uses soft read unless enforcement is established.
AGY has no verified native read-only mode, so it uses soft read too.

The design must preserve the useful instruction-based mode. It must distinguish
that mode from an enforced read-only capability in state, status, and decisions
that rely on write prevention. A request that requires enforced read-only must
get a clear unsupported response if the selected harness cannot provide it.
Determine the CLI names, compatibility behavior for existing `--read-only`
callers, and any migration of saved worker state during implementation design.

### Evidence and concern

`bin/claivelib/engines/pi.py` retains Pi's full tool set and adds a read-only
instruction. `bin/claivelib/config.py` accepts a read-only request in a workspace
whose policy disallows writes. `bin/claivelib/cli.py` also excludes read-only
workers from its owned-writer conflict check. The instruction alone does not
prevent writes, so those decisions currently assign a stronger guarantee than
Pi provides. `docs/pi-engine.md` already describes the instruction as advisory.

Check Claude's plan mode separately. The review established that Claive selects
plan mode, not that it provides the same enforcement as Codex's read-only
sandbox. Do not infer equivalence without checking the native behavior.

### Acceptance evidence for future work

Exercise enforced and soft-read selection across all adapters. Check soft-read
prompts on AGY and on both Pi transports. Show the mode honestly in worker
status. Check workspace policy and concurrent-writer decisions using the actual
enforcement level. Check existing saved workers and read-only callers. For an
explicit hard-read requirement, check refusal before launch if enforcement is
unavailable.

## 2. Pi provider and model selection

### Decision

Claive must not restrict Pi to `opencode2api`. Make Pi's available providers
usable through Claive. When the caller gives no provider or model, use Pi's
configured defaults. Honor explicit caller selections and existing Claive
policy. Report a provider or model that Pi cannot resolve; do not silently
switch providers.

### Evidence and concern

`PiEngine.validate_turn` rejects every provider except `opencode2api`.
`resolve_launch` ignores Pi's remembered model when its default provider is
different. Its settings writer also writes `defaultProvider=opencode2api`.
The optional loopback override and doctor checks are specific to that provider
in `bin/claivelib/provider.py`. Simply removing the validation would leave
default selection, model persistence, health checks, and provider overlays
inconsistent.

The design must define precedence for explicit launch flags, role and Claive
policy defaults, and Pi defaults. It must determine how Pi resolves a model
without a provider, including `provider/model` identifiers. It must preserve
the existing `opencode2api` override for that provider without applying it to
other providers. Review the current behavior that remembers explicit model
selections in shared Pi settings. Do not overwrite an unrelated provider or
model as an accidental side effect.

### Acceptance evidence for future work

Test Pi defaults for a non-`opencode2api` provider, explicit provider and model,
role and policy precedence, invalid provider errors, retained sessions and
follow-ups, and the existing loopback override. Check that no credentials or
unrelated settings are changed or printed. Include a real Pi smoke test where
the selected provider is available.

## 3. Codex fallback effort

Claive currently carries the original effort to a fallback model. For example,
Terra at `xhigh` can fall back to Luna, which accepts `medium` or `max`. The
fallback can fail during command construction. The supervisor catches that
failure without reporting its reason as the fallback failure.

Adapt effort to the selected fallback model when necessary. Preserve the
caller-selected effort when the target accepts it. Respect model and effort
policy. Report an incompatible or rejected fallback clearly. Test the resulting
command and failure reporting. See `bin/claivelib/cli.py` and
`bin/claivelib/engines/codex.py`.

## 4. Codex structured output

The installed `codex exec --help` lists `--output-schema`, but Claive's Codex
adapter rejects that option. Investigate support for the ordinary exec path.
Steering and fork use the separate app-server transport, so check those paths
independently. If a transport cannot provide the contract, return an explicit
unsupported response for that combination. Do not remove Claude's existing
structured-output support to make the engines uniform.

## 5. Pi extensions

Claive passes `--no-extensions` to Pi. The installed Pi help says explicit
`--extension` paths still work with that flag, while extension discovery and
built-in extensions are disabled. Review how users can opt into the native
extension features they need. Keep existing behavior compatible and do not
assume every extension is safe to load automatically. Check both print and RPC
transports before promising equivalent behavior.

## 6. Capability discovery and honest refusals

`claive engines` currently reports models, policy, and steering, but it does not
give a complete view of read-only enforcement, soft read, structured output,
forks, provider choices, or other engine-specific limits. Design capability
reporting that distinguishes supported, unsupported, advisory, and unverified
behavior where those distinctions matter.

The shared interface must not force every harness into the least capable
feature set. The CLI and errors should name the selected engine and any
unsupported combination. A capability exposed by one engine remains usable
even if another engine lacks it. Native differences that cannot be normalized
must remain visible to callers.

## Review scope and evidence

The preceding light review read the three adapters and their shared launch and
policy paths. It ran 59 targeted adapter and fallback tests; all passed. Those
tests use fixtures and do not prove all live harness behavior. Codex's native
schema option and Pi's provider and extension flags were checked in the
installed CLIs' help output. The review did not test every provider, transport,
or permission boundary live.

The separate AGY design remains at `docs/designs/agy-engine.md`. These findings
should inform its capability model without blocking its existing scope or
silently changing the user's AGY decisions.
