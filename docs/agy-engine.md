# AGY engine

The initial adapter targets AGY 1.4.12, checked on 2026-11-03.
The machine-readable baseline is [agy-1.4.12.json](compatibility/agy-1.4.12.json).
Recorded streams retain their CLI version and provenance in the fixture manifest.
The implementation commit also names the tested CLI version.

For an upgrade, compare the new version's flags, event shapes, session identity,
usage counters, and timeout behavior against that baseline. Run its hermetic
and live checks. Preserve the old fixtures and record the new version with
the behavior changes. Add a version-named record instead of replacing the old one.
Include a `Tested-AGY-Version` footer in commits that change AGY behavior.
The tested version is a comparison baseline, not a runtime pin.

Run `claive run --engine agy --worktree --turn-timeout 180 "implement the task"`.
Use `claive open --detach --engine agy ...` for a reusable worker, then
`claive followup ID "next task"`. Claive resumes the exact bound conversation.
A different returned ID fails the turn as a protocol error.

The binary comes from `AGY_WORKER_BINARY`, `~/.local/bin/agy`, or PATH.
`claive doctor` checks availability without requesting a model.
`claive engines` lists AGY's policy, models, efforts, and read capability.

| Model | Required effort |
| --- | --- |
| gemini-3.8-flash-high | high, the default |
| gemini-3.8-flash-medium | medium |
| gemini-3.8-flash-low | low |

An explicit effort must match the model suffix. The base alias is unsupported.
`--fallback-models gemini-3.8-flash-low` re-derives effort on retry and retains
the low model and effort for follow-up. Steering cannot accompany fallback.
The fallback list is consumed across the worker's lifetime.
Queued follow-ups retain the model they targeted. If fallback changes that
model before delivery, AGY derives the queued effort from the current model.
Invalid fallback models are refused before the first job starts.

AGY receives stream-json flags before `-p`. The prompt is one argv value,
without a shell. The UTF-8 limit is 100,000 bytes, including read instructions.
That limit is a conservative host guard, not a provider context guarantee.
An OS `E2BIG` launch error asks the caller to reduce the prompt.
Claive owns the deadline and does not pass AGY's print-timeout flag.
AGY can return exit zero and SUCCESS after a native print timeout. Claive
recognizes `finished_reason=print_timeout` as failure.

`--read-only` adds an explicit read instruction. Status reports `soft` and
states that enforcement is unavailable. Soft readers count as possible writers
for workspace policy and ownership conflicts. A `write: false` workspace
entry refuses AGY, including read-only requests. `--require-read-only` returns
`agy cannot enforce read-only; --require-read-only is unsupported` before job creation.
The worktree changes cwd. It does not confine commands or nested agents.

AGY does not support steering, fork, web selection, output schemas, step caps,
provider selection, imported session IDs, or disabled session logging in this
adapter. Configuration rejects AGY token budgets. Unknown authentication and
quota errors remain worker failures. No guessed quota marker triggers retry policy.
AGY inherits user configuration, MCP servers, skills, proxy settings, and certificates.
Configuration overhead and nonzero cache semantics remain unmeasured.

`claive usage ID --json` returns `model_calls: null`, `calls: []`, an
`aggregates` list, totals, and the latest turn. Each aggregate identifies its
turn, attempt, model, and conversation. A result can include several provider
calls. It does not receive a heavy-call warning or a per-call growth estimate.
Usage totals subtract the prior conversation baseline. Thinking is part of
output and is not added again. Each record's `native_cache_read_tokens` preserves
the observed cache counter. Claive neither subtracts it nor sums it into totals.
The normalized cache ratio stays unknown until native semantics are established.

Missing usage produces an incomplete record without invented zero counters.
Known totals remain visible. After a missing result, a later cumulative delta
may span unreported attempts. That record reports the gap and incomplete usage.
Declining counters fail the turn and preserve the previous baseline.
The unverified cache counter is exempt from that monotonicity rule.
Partial results retain earlier baseline fields. The first complete result
after a gap reports incomplete attribution. Later complete attempts recover
normal attribution. Session completeness still records the historical gap.

State atomically stores the fallback launch and `turn_policy`. `policy.json`
is a compatibility projection. Follow-up reads state if that projection is
missing or stale. An explicit `claive effort` command writes a model-scoped
override. A later fallback invalidates the old model's override. A failed
projection write cannot restore high effort to a low model.

AGY stores native conversations outside the job directory. Claive does not
remove them. Unknown-conversation recovery that silently creates another session
fails Claive's strict ID check. The historical relay remains unused.

Run `just test-one tests/test_agy_engine.py tests/test_agy_fixtures.py` for
hermetic checks. Run `CLAIVE_AGY_LIVE=1 just test-one tests/test_agy_live.py`
for real edit, check, resume nonce, usage delta, and soft-read acceptance.
Live test artifacts remain in the reported temporary directory.
