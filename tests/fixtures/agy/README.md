# AGY Phase 0 recordings

Implementation starts with protocol evidence. The production adapter is not
implemented. Required design decisions still block Phase 1.

AGY version: 1.4.12. Capture date: 2026-11-03.
`models.txt` records six models, including Gemini 3.8 Flash high, medium, and low.
The original recordings came from `/tmp/claive-agy-phase0`.
The probe report is `scratch/egm-agy-review/phase0-2026-11-03.md`.
These are existing captures, not new live acceptance runs.

`manifest.json` records provenance, source hashes, fixture hashes, and recorded
process exits. Sanitization replaces local paths and conversation UUIDs.
The same source UUID receives the same replacement across every fixture.
The unknown requested UUID remains unchanged. The nested capture includes a
home-directory log URI and paths split across text deltas. Sanitize that URI
and assembled text, then repartition the text across the same delta events.
Delta boundaries can therefore differ. Tools, field placement, token counts,
durations, model names, and response content otherwise remain intact.
Source hashes allow comparison with the original temporary captures.

Only `error.ndjson` and `banner.ndjson` are synthetic. They test replay branches.
They do not establish real AGY error or banner formats. The timeout is captured,
including its zero exit, `SUCCESS` status, and `print_timeout` reason.
No quota or authentication failure string is established by these fixtures.

## Correction to the earlier probe interpretation

The captured init event contains a **top-level** `conversation_id`.
Its nested `init` object has no such field. The design and earlier probe note
mistakenly describe the whole event as lacking an ID. Future normalization
must consider the top-level ID, then compare every later session ID.
The fixture tests preserve the actual field placement.

## Evidence and remaining gates

| Question | Recorded result | Limit or next action |
| --- | --- | --- |
| Event types and states | `user_input`, `agent_response`, `tool`, `subagent`, and `system_message`; `ACTIVE` and `DONE` | No separate thinking-text event observed; extend the design table for the two additional types |
| Edit without mode | Captured read/write tools and final `after` response | Prior probe reports the actual file edit; replay does not edit files |
| Usage and cache | Three turns have increasing totals; output includes thinking in these samples | All cache reads are zero; nonzero semantics remain unknown |
| Stdout | Captured streams contain only JSON lines | Startup banner is synthetic |
| Resume and concurrency | Three-turn conversation and two separate conversations with resumes | No concurrent edit safety or confinement claim |
| Argument size | Prior OS probe accepted 102,400 bytes and rejected 131,072 bytes | No accepted-size model request recorded; no model limit established |
| Flag order | `success` and `flags-after` capture both orders | Replay parsing alone does not prove native parsing |
| User configuration overhead | Unknown | Need a proven isolated configuration before comparison |
| Nested worker cwd | `nested` records `invoke_subagent` and the temporary repository path | Prior probe reports the nested file; cwd is not confinement |
| Authentication and quota | Unknown | Do not revoke credentials or provoke quota |

The usage consumer contract remains blocked. One edit result contains several
model steps. A turn result cannot be labelled one provider call. Missing usage
must remain distinct from zero. Cache ratios remain unknown for these samples.
Fallback persistence also needs a recoverable source of truth before code changes.
Read-mode policy needs the shared soft-read design before AGY registration.

## Replay

Run `just test-one tests/test_agy_fixtures.py`.
The executable `tests/fake_agy.py` selects a fixture through
`FAKE_AGY_SCENARIO`. The default is `success`. Supported scenarios match the
NDJSON filenames without their extension. `--output-format json` supports
`success.json`. Unknown scenarios return exit 2 without output.

The replay accepts future adapter launch arguments but does not execute prompts,
create sessions, enforce permissions, or rewrite conversation IDs. Resume tests
must use the fixture IDs. A future lifecycle fake must add explicit session
behavior before it can prove supervisor integration.
