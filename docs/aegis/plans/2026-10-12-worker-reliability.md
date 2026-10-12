# Worker reliability repairs

Goal: implement all seven investigation findings plus supported live steering.
Architecture: supervisor owns execution and normalized observations; independent parent commands own verification evidence and acceptance. Engines expose real delivery capabilities. Completion never implies acceptance.
Tech stack: dependency-free Python CLI, shell installer, unittest/pytest fixtures.
Baseline: user investigation; dfc163fd on main, initially clean; cli.py, engine.py, engines/*, turns.py, install.sh, existing tests and harness-integration docs. Codebase Memory generation 2026-10-11T22:12:58Z; source read for engines/tests coverage gaps.
Change Necessity: code-change; counters and phases require producer fixes, deployment needs provenance, and checks/ownership need an independent gate.
ArchitectureReviewRequired: yes, acceptance/control boundaries. Preserve orch verifier ownership; ordinary workers expose parent-controlled records rather than self-acceptance.
Compatibility: retain existing lifecycle statuses, cumulative task_failures, queue semantics, exit codes and default engine invocations. New checks and ownership opt in. No historical state migration. User later authorized commit, push, and ARM repository/runtime/Skillshare synchronization; preserve active ARM work.
TDD Route: off/skipped (no explicit strict authority); meaningful regression tests required after changes.

1. Runtime lane: installer provenance and source/installed drift smoke check, Codex opt-in regression, README correction. Own install.sh, new runtime module/tests, tests/test_codex_engine.py, README.md. Never patch installed files manually; synchronize through installer after verification.
2. Steering lane: inspect installed provider CLI/source and primary docs; implement real steering where supported, with explicit unsupported errors otherwise. Own engine.py, engines/* except codex.py, new steering.py and steering tests/docs. Parent integrates CLI and supervisor hooks. Avoid claim that queued work alters current request.
3. Supervisor lane (parent): required-check manifest; verification passed/failed/blocked/not_run evidence with environment; pending/accepted/rejected independent parent record bound to turn; ownership snapshot including tracked/untracked content and deletions; violations block acceptance. Provide scratch execution for verifier commands. Per-turn warning records and previous-turn counts; honest tool completion phase, pending queue display; recent usage growth summary.
4. Integration: adversarial regressions for blocked checks, missing verification, ownership violations, dirty baselines, turn reset, steering receipts/capabilities, drift/config. Run suite, review final diff, independently review changed contracts, install with project installer and smoke installed runtime.

Repair: fixes at canonical installer, engine normalization, supervisor and parent verification owners.
Retirement: retain lifecycle/queue compatibility; retire inferred acceptance and stale active-tool phase. No destructive historical cleanup.
Verification: pytest -q; focused new tests; bash -n install.sh; source and installed doctor/drift smoke. Live provider proof where practical, record unavailable checks rather than fabricate success.
Stop: all requested behavior implemented and checks pass; document provider limitations accurately.
