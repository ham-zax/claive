# Checkpoint

Phase 0 preparation is implemented. The full Phase 0 gate remains open.

Added 19 fixtures with provenance and hashes, an executable replay binary,
12 protocol/replay tests, and AGY environment isolation for hermetic tests.
Seventeen fixtures come from existing captures. Error and banner are synthetic.
The fixtures occupy 48,743 bytes including their README and manifest.

Corrected the design and prior probe note locally. Init has a top-level
conversation ID. Nested-worker captures also contain `subagent` and
`system_message` steps. Those two types map to activity in the revised design.
These documents were already untracked at task start. Keep their full contents
outside the scoped implementation commit to preserve ownership of prior work.

Evidence: `90-evidence.md`. Drift decision: continue with design resolution,
stop before production implementation. No engine registration or production
behavior changed. No workers, branches, or worktrees were created.

Next: resolve aggregate usage and missing-data semantics across usage consumers.
Then specify fallback recovery and soft-read policy. Establish an isolated
configuration probe and a conservative prompt-limit contract. Reconcile the
design review before Phase 1. Do not infer approval from replay tests.

Resume read order: intent, this checkpoint, evidence, fixture README,
AGY design, impact report, owning production code for the next contract.
