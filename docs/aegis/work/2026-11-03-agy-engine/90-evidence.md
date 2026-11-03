# Phase 0 preparation evidence

Date: 2026-11-03. Scope: fixtures and test infrastructure only.

- `just test-one tests/test_agy_fixtures.py tests/test_engine_policy.py`:
  exit 0, 28 tests pass in 12.302 seconds.
- `uvx ruff check --select F tests/fake_agy.py tests/test_agy_fixtures.py tests/hermetic.py`:
  exit 0, all checks pass.
- `git diff --check`: exit 0.
- Source-hash comparison: all 17 captured files match the original temporary
  recordings. The fixture test checks sanitized hashes too.
- Prior edit proof: the temporary repository still contains `probe.txt` with
  `after\n` and `nested-probe.txt` with `nested-ok`. Both content checks pass.
  These checks inspect prior probe artifacts. They do not rerun live edits.
- Fresh `agy --version`: 1.4.12. Fresh `agy models`: the same six model IDs
  as the stored model listing.

The first sanitization check failed on a home-directory log URI. Inspecting
the nested stream also exposed paths split across text chunks. The correction
sanitizes the URI and joined text before repartitioning. A regression test
checks the joined text, and all tests pass after that correction.

Covered: observed session-ID locations, three-turn cumulative usage, multiple
model steps in one result, tool pairs, concurrent conversation identity,
unknown-conversation replacement, timeout with zero exit, replay selection,
synthetic failure provenance, and hermetic AGY environment handling.

Uncovered: production adapter behavior, supervisor integration, live nonce
acceptance, nonzero cache reads, native authentication/quota strings, isolated
configuration overhead, model prompt limit, concurrent edit safety, and
recoverable fallback persistence. The production implementation remains open.
Confidence: B for preparation, C for the complete AGY engine.

## Full implementation evidence

- `just check`: exit 0. Compile, Ruff F rules, and ShellCheck pass.
  The suite runs 652 tests in 375.506 seconds, with 3 opt-in skips.
- `just test-one tests/test_agy_engine.py`: exit 0, 17 tests in 6.880 seconds.
- Compatibility set: ask, parent ergonomics, workers, teams, startup, fork,
  and Claude steering. Exit 0, 121 tests in 78.132 seconds, 2 live skips.
- Receipt-corruption recovery: 8 controlled repetitions pass with lock-ordered
  injection. The earlier injection could race with an acknowledgement write.
- `CLAIVE_AGY_LIVE=1 just test-one tests/test_agy_live.py`: exit 0, 1 real
  acceptance test in 35.723 seconds, using AGY 1.4.12.
- Live acceptance covers a tracked job, an isolated edit, a required check,
  exact conversation resume with a random nonce, cumulative usage deltas,
  soft-read reporting, and unchanged parent Git status.
- Live artifacts: `/tmp/claive-agy-live-i6zy0ojo`. Editing worker
  `78de1d259ab9`, soft-read worker `6513cad9a057`. Both finish successfully.
  Subsequent source CLI `show` and `usage` commands also pass.
- `bin/claive engines --json` includes AGY, all three Flash model/effort
  pairs, no steering, and `read_only_enforced: false`.
- `git diff --check`: exit 0.

The initial full suite failed on historical record rendering, legacy policy
shape, fixture capability assumptions, Claude's expected permission mode, and
nondeterministic receipt corruption. The final full suite includes their fixes.

Covered: production launch, resume, fallback plus low-effort follow-up,
projection-write recovery, policy refusal, session mismatch followed by success,
declining/invalid/missing usage, malformed streams, banner boundaries, timeout,
prompt limits and OS E2BIG, peek output, operator enumeration, and worktrees.
Retained limits: nonzero cache conversion, isolated-config overhead, native
quota/authentication markers, and filesystem confinement remain unclaimed.
The host prompt guard and explicit unsupported-option refusals bound those limits.
This initial full implementation check precedes advisory review. The final reviewed evidence appears below.

## Review corrections

The user restarted the interrupted advisory review. The completed report returns
ten findings. `80-review.md` records their source checks and disposition.
The parent fixes them directly without starting another reviewer.

`just test-one tests/test_agy_engine.py tests/test_steering.py`: exit 0,
51 tests in 23.743 seconds. The new regressions exercise queued explicit effort
across fallback, partial/missing cumulative counters, cache decreases, invalid
fallback models, malformed-result display, partial policy projections, retired
adapters, and the soft-read refusal policy across AGY, Pi, and Claude.

Final source live acceptance: exit 0, 1 test in 34.489 seconds. Artifacts:
`/tmp/claive-agy-live-4kflovhg`. The real edit/check/resume/usage/soft-read journey
passes after the review corrections. Final full regression is running.

The reviewed 660-test full run found one v1 request-format failure. Preserve
its four-field format and add queued-model metadata only to schema v2.
The focused v1 compatibility and explicit AGY queued-fallback checks both pass
after that correction. The final full check runs against the corrected source.

Version provenance: `docs/compatibility/agy-1.4.12.json` records the tested CLI
version, date, model mapping, observed wire behavior, fixture manifest, and
upgrade checks. Its version matches the captured manifest. Commit metadata
also records `Tested-AGY-Version: 1.4.12`. The user authorizes commit and push.

## Final checked state

`just check`: exit 0. Compile, Ruff F rules, and ShellCheck pass.
The complete suite runs 660 tests in 572.072 seconds, with 3 opt-in skips.
The focused v1 queue-format and explicit AGY queued-fallback regressions pass
in 1.122 seconds. The final full suite includes both corrected paths.

Real AGY acceptance passes in 34.489 seconds. Subsequent CLI show and usage
checks report completed worker `78a29ec2c2d5`, the same conversation over two
turns, and the exact turn delta. Soft-read worker `7a06584a5362` also completes.
Artifacts remain in `/tmp/claive-agy-live-4kflovhg`.

All ten advisory findings have source-checked corrections and regression coverage.
The versioned JSON baseline parses and agrees with fixture version 1.4.12.
`git diff --cached --check` passes. The user authorizes the scoped commit and
push to origin/main. Git records the resulting delivery receipt.
Confidence: B, with the adapter's explicit unsupported capabilities and
unmeasured cache/configuration semantics retained as documented limits.
