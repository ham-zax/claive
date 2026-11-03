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
