# Independent review and disposition

The first review run was interrupted. The user restarted the reviewer.
The completed read-only review returned ten findings. The parent checks the
source, applies the corrections, and retains integration ownership.

1. Queue/fallback effort: each queued request records its target model. If that
   model changes before delivery, use the engine's fallback effort hook. An
   explicit effort on an unchanged model still follows normal validation.
2. Malformed result display: `describe_event` returns an error step rather than
   raising for invalid result fields. Unknown statuses already map to protocol
   failures. Tests exercise the actual show and peek commands.
3. Banner boundary: the first valid normalized event ends banner tolerance.
   A missing init cannot leave the whole stream in startup mode.
4. Partial counters: merge baseline fields, preserving counters absent from
   a partial result. The next counter delta retains its last known baseline.
5. Gap recovery: distinguish source-counter completeness from attribution
   completeness. Only the first complete result after a gap spans missing
   attempts. Later attempts recover. Session completeness retains historical gaps.
6. Cache semantics: preserve `native_cache_read_tokens` without subtraction or
   totals. A native cache decrease does not fail a turn or trigger fallback.
7. Invalid fallback: check native model/effort rules before creating the job.
   Current user policy is still checked when a fallback attempt actually starts.
8. Open message: both the guard and CLI help name AGY's reusable fallback support.
9. Missing adapter: read-mode and steering summaries report unavailable
   capability. Show retains the record without requiring peek intent from a
   missing adapter. A regression checks the full CLI read path.
10. Partial policy projection: missing override fields use canonical state
    defaults, avoiding a KeyError on follow-up or effort commands.

Additional checks cover AGY, Pi, and Claude refusal of enforced-read requests
and write-prohibited workspace entries. No replacement review was started.

Focused evidence: 51 AGY/steering tests pass in 23.743 seconds. The earlier
132-test review-fix run exposed the steering summary dependency. The parent
fixed it at that owner, then checked the repaired read path.
Final live acceptance passes against the repaired source. Full regression exposed
one v1 queue-format incompatibility. New model metadata now applies only to
schema v2 requests. The v1 format and the AGY queued-fallback regression both pass.
The final full check passes after that correction: 660 tests, 3 opt-in skips. Real AGY acceptance also passes.
