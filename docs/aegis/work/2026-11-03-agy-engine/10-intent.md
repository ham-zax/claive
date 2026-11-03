# AGY engine implementation

The user requests implementation from `docs/designs/agy-engine.md`.
The existing design owns the phase sequence. Start with its Phase 0.
Production changes require resolved usage, fallback, and read-mode contracts.

TaskStartSnapshot: branch `main`, HEAD `85b204c4056de4b9dd110e80d60a53df2ace3b31`.
Existing changes: `docs/design-worker-freedom.md`, `concepts_brief.md`,
`docs/designs/`, and `scratch/`. Preserve those changes.

Baseline read-set: the AGY design, harness adapter follow-ups, second review,
impact report, initial probe report, raw captures in `/tmp/claive-agy-phase0`,
`bin/claivelib/engine.py`, `reference/agy-relay.sh`, `tests/fake_pi.py`,
`tests/test_pi_engine.py`, `tests/hermetic.py`, and `justfile`.
Codebase graph tools are unavailable in this session. Known-file reads supply
the baseline for this test-only slice.

Change necessity: temporary recordings cannot support reproducible adapter tests.
Add sanitized fixtures, provenance, a replay binary, and evidence tests.
No production adapter, shared state, policy, usage consumer, or scheduler changes.
ArchitectureReviewRequired: before production contracts change.
TDD route: light for the test-only preparation slice. Test captured protocol
facts and replay behavior. This does not establish production acceptance.

Success for this slice: hermetic replay, documented capture provenance,
three-turn usage evidence, edit evidence, timeout evidence, and explicit unknowns.
Stop before Phase 1 while required contracts remain unresolved.

## Scope amendment

The user then requests the entire implementation and asks for efficient completion.
Complete Phases 1–6, resolve routine contract choices, and retain honest unknowns.
The earlier Phase 0 stopping point no longer defines the task boundary.
Continuation snapshot: `main` at `7d6a7a1`. Pre-existing user paths remain outside
the implementation commit. The same work record remains the state carrier.

Change necessity: a native adapter and core hooks are required for tracked AGY
turns. The relay cannot provide native events, usage, or strict resume identity.
Production scope includes engine capabilities, failure/session ownership, usage
accounting, fallback persistence, read policy, operator surfaces, tests, and docs.
ArchitectureReviewRequired: yes, independent advisory review before completion.
TDD route for production: mode off, decision skipped. Add meaningful regression
checks after implementation. No strict test-first request exists.

Contract resolutions: report attempt aggregates with unknown model-call count,
retain incomplete usage without invented zero counters, preserve native cache
counts without claiming a normalized ratio, and atomically save fallback launch
and defaults in state. Keep policy.json as a compatibility projection. An explicit
effort command is a model-scoped override. Capability-based soft read cannot
bypass workspace write policy or ownership checks. Claude soft read uses an
instruction instead of a plan-approval turn. No native AGY sandbox is claimed.

The host prompt guard is 100,000 UTF-8 bytes including read instructions.
Unknown isolated-config overhead and cache semantics remain documented limits.
No quota or authentication failure is forced. Native stores remain untouched.
No new provider integration, orchestration auto-selection, steering, or fork.
