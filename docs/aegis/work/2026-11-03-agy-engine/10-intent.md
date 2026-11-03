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
