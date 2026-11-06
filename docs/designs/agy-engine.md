# AGY as a fifth Claive engine

* **Author:** Ahmed Hamza
* **Status:** Implemented against AGY 1.4.12. Contract revisions below resolve the earlier implementation blockers.
* **Implementation:** Complete. Final check passes: 660 tests, 3 opt-in skips. Live AGY acceptance passes. Independent review findings are corrected.
* **Date:** 2026-11-02

**Implementation resolution, 2026-11-03:** The user requests the complete
implementation. Usage records are attempt aggregates, not model calls. State
atomically stores fallback launch and defaults. Explicit effort changes use
model-scoped overrides. Actual read capability drives policy and concurrency.
The two unavailable historical reviews do not count as completed reviews.

**User decision, 2026-11-03:** Read-only requests use verified native
enforcement where available and otherwise use an explicitly reported soft read
mode. AGY has no verified native enforcement, so AGY uses soft read. This
supersedes the earlier decision to refuse AGY read-only requests. A separate
request that explicitly requires enforcement must be refused when unavailable.

--------------------------------------------------------------------------------

## Overview

Claive starts agent CLIs as background workers. Each supported CLI is an engine:
`muse`, `pi`, `claude`, and `codex`. This design adds the Antigravity CLI `agy`
1.4.12 as a fifth engine. `claive run --engine agy` gets the same core lifecycle
as the other engines: launch, tracked job, normalized events, answer file,
resume, follow-up turn, usage, checks, and acceptance.

The target model is Gemini 3.8 Flash at `high`, `medium`, or `low` effort. AGY
already emits typed NDJSON and resumes with `--conversation`. The adapter is
therefore a native engine module, not a wrapper process.

## The Problem

AGY is usable today only through `reference/agy-relay.sh` or an ad-hoc command.
The relay prints plain text, truncates the answer at 12 KB, and hides the
conversation id. Claive requires NDJSON, so the relay cannot be a worker.

Adding one engine module is not sufficient. Four existing assumptions block it:

1. Worktree isolation never reaches an engine outside `codex`, `claude`, or
   `pi`. The child runs in the parent workspace while Claive reports the
   worktree as its workspace.
2. AGY chooses the conversation id after launch. Claive has no engine outside
   the current four that binds a session id from the event stream.
3. Model and effort are separate fields in Claive, but AGY encodes effort in
   the model id. A mismatched pair fails before the model starts.
4. Usage is reported from provider session files after the turn. AGY exposes
   only a cumulative result total, so a follow-up cannot report its own delta
   without stored baselines.

AGY also has no verified read-only mode. `--mode plan` produces a plan for
approval and does not perform the requested investigation, so plan mode is not
a substitute. A read-only request uses a soft instruction and reports that the
restriction is not enforced. The shared scheduler and workspace policy must
distinguish this from an enforced read-only worker
(`bin/claivelib/cli.py:1816-1831`).

The proposed solution is a native `agy` adapter plus three narrow core
capabilities: a worktree-cwd declaration, a model-to-effort hook, and a shared
session/usage boundary. Pi, Claude, Codex, and AGY deliver the worktree through
cwd. Muse does not because its command already carries the worktree through
`--workspace` and `-w existing` (`bin/claivelib/engines/muse.py:220,241`).

The adapter maps AGY events, binds the conversation id after the first turn,
and refuses unsupported requests instead of emulating them.

## Goals

* A write turn with `--engine agy` creates a tracked job, delivers the Claive
  worktree as the process working directory, and leaves the parent repository
  unchanged.
* A later turn resumes the same AGY conversation. A random nonce written in
  turn 1 is echoed in turn 2, so the test cannot pass on a fresh session.
* A fallback from `gemini-3.8-flash-high` to `gemini-3.8-flash-low` starts
  successfully, with effort re-derived from the new model id and saved for the
  next turn.
* A read-only request starts AGY in soft read mode, instructs it not to write,
  and reports the mode as not enforced. Explicit requests that require enforced
  read-only fail before launch when enforcement is unavailable.
* A follow-up turn reports its own token delta, not AGY's cumulative total.
  The delta matches the turn's `result.usage` minus the stored baseline.
* `claive doctor` and `claive engines` show a working `agy` row, and engine
  name validation accepts `agy`.
* An AGY token budget is rejected by Claive configuration before launch, because AGY cannot enforce
  it.
* A live opt-in test proves an edit, a check, resume, usage, and no parent-repo
  writes.
* The other four engines keep their present working-directory behavior. Muse
  stays flag-delivered with `cwd_is_worktree` false.

## Non-Goals

* In-flight steering and session fork.
* Web search, output schema, step cap, sandbox, and provider selection.
* `--no-session-log` support and automatic retention cleanup.
* Budget enforcement. Configuration already rejects budgets for non-Claude engines.
* Orchestrator auto-selection of AGY.
* AGY plan mode and plan-artifact collection as substitutes for read-only.
* Unrelated changes to the other engines. Shared read-mode capability and policy
  changes apply to every adapter as described in `harness-adapter-followups.md`.

## Open Questions and the Phase 0 Gate

Phase 0 answers these before production code is written. Each answer has a
pass condition and a stop action. A failed condition stops the phase and
revises this design; it is not recorded as a footnote.

| # | Question | Method | Pass condition | If it fails |
| --- | --- | --- | --- | --- |
| 1 | Which `step_type` and `state` values mark model text, thinking, tool calls, and tool results? | Two recorded turns | Every value maps to one Claive event or `activity` | Stop; extend the mapping table |
| 2 | Does `--dangerously-skip-permissions` permit edits when `--mode` is omitted? | Edit turn in a temporary repo | The file changes without an approval prompt | Stop; require an explicit mode or refuse the launch |
| 3 | Is `result.usage` monotonic within one conversation, and does `input_tokens` include cache reads? | Two-turn and three-turn runs | Monotonic totals; cache field usable | Stop; use step usage or disable cache accounting |
| 4 | Is stdout pure NDJSON? | Capture raw bytes | Only JSON lines, or banners only before `init` | Stop; add a tested pre-init filter or revise |
| 5 | Is a conversation bound to its starting directory? Can two run at once? | Two jobs in one directory | Resume works; no lost writes or crossed answers | Stop; add the serialization lock |
| 6 | What is the largest prompt this host accepts as one argv value? | 100 KiB and 128 KiB argv probes; separately test a model prompt | Record kernel and AGY results separately | Stop; lower the documented cap if needed |
| 7 | Do flags before `-p` parse correctly? | Run with `-p` last | All options take effect | Stop; find a working order |
| 8 | Do user MCP servers and skills load in print mode, and what do they add? | Compare a bare run to the user config | Token overhead is recorded and bounded | Stop; document the extra cost or disable them |
| 9 | Do AGY subagents inherit the worker's cwd? | One delegated edit | The edit lands in the worktree | Stop; remove the subagent claim from the goal |
| 10 | Which text marks authentication failure and quota exhaustion? | Existing logs and any naturally occurring quota failure | Record available evidence; mark unavailable strings unknown | Record unknown failures as `worker`; do not guess a quota kind |

`agy --help` says the default print timeout is `0s`, which waits for the turn
to finish. A one-second probe returned exit zero and `status=SUCCESS`, but its
`finished_reason` was `print_timeout`. The adapter must fail that result as a
timeout. Claive owns the turn deadline and does not pass this flag by default.

Plan mode and `--sandbox` are not probed. Both are non-goals for this
increment.

## Critical User Journeys

### CUJ 1: Write in an isolated worktree

A developer runs `claive run --engine agy --worktree "add a test"`. Claive
creates the job and the worktree, starts `agy` with the worktree as its working
directory, and records the event stream. AGY edits a file in the worktree. The
parent repository stays unchanged. The answer and usage appear under the job.

### CUJ 2: Resume the same conversation

A developer runs `claive followup <job> "now rename that helper"`. Claive passes
the bound conversation id. AGY answers from the earlier context. The follow-up
reports only the tokens used by that turn, not the conversation total.

### CUJ 3: Read with AGY

A developer requests a read-only AGY task. Claive starts AGY with an explicit
instruction to read and not write. Status identifies the request as soft read,
not enforced read-only. Policy and concurrency checks do not treat the worker as
an enforced reader. Exact CLI syntax is part of the shared design.

### CUJ 4: Fall back from high to low effort

A `gemini-3.8-flash-high` turn hits a provider limit. Claive rebuilds the turn
for `gemini-3.8-flash-low`, re-derives the effort from the new model id, saves
it in the job state, and starts a new AGY conversation. The policy check and
the next follow-up both use `low`.

## Alternatives Considered

### Alternative 1: Keep the relay script

The existing `reference/agy-relay.sh` already starts AGY. It was rejected
because it prints plain text, keeps only the last 12 KB, hides the conversation
id, and reports no token usage. Claive cannot normalize, resume, or measure
those turns. The README already calls the relay historical
(`README.md:342,366`).

### Alternative 2: A wrapper that turns text into NDJSON

A small process could run AGY and print synthetic Claive events. It was
rejected because AGY already emits typed NDJSON. A wrapper would invent event
boundaries and token counts, add a second process to supervise, and still need
the conversation id. It adds a parser without removing one.

### Alternative 3: Full Pi parity on day one

AGY could expose steering, fork, enforced read-only, step caps, and output
schema from the start. Those native guarantees are unverified. Steering and
fork stay unsupported until proven; read-only requests use soft read instead.

### Alternative 4: Run AGY through the Codex engine

Pointing the Codex adapter at the `agy` binary was rejected. The flags,
events, session id, and usage shape differ (`codex exec --json` against
`agy --output-format stream-json`). The adapter would branch on the binary and
become two engines in one file.

### Alternative 5: Keep `--effort` separate from the model

Claive could store `gemini-3.8-flash` plus an effort field. It was rejected
because the tested spelling is the suffixed model id. AGY 1.4.12 does list
`--effort`, but Claive does not pass it: a second source of truth would break
fallback, which must re-derive effort from the new id, and a conflicting pair
fails before the model starts. The suffix is the single source of truth.

### Alternative 6: Use plan mode as read-only

`--mode plan` blocks edits, so it looks like a read-only mode. It was rejected
because it returns a plan for approval, not the requested investigation, and
it reports success. Claive would mark that job as a safe concurrent reader
while the answer is only a proposal (`bin/claivelib/cli.py:1816-1831`).

### Alternative 7: Run in the parent workspace with a prompt guard

A prompt could tell AGY not to write outside the repository. It was rejected
because a prompt is not enforcement. Two writers would share the parent, the
worktree checks would inspect a clean tree, and the parent could change. This
is the current accidental behavior that the design removes.

### Alternative 8: Add AGY to the four engine-name branches

Adding `agy` beside `codex`, `claude`, and `pi` at the four sites is the
smallest local edit. It was rejected because it grows three literals, misses
the fallback path, and makes the next engine repeat the work. The boolean
capability states the actual rule once. Muse stays false because it already
receives the worktree through its flags
(`bin/claivelib/engines/muse.py:220,241`).

### Alternative 9: Use `--sandbox` as the isolation boundary

`--sandbox` could replace the worktree. It was deferred because the flag is
unproven on this host and blocked by AppArmor here. A sandbox also gives no
diff and no named worktree to merge or remove. It is not part of this
increment.

## Technical Plan (Architecture)

The design adds one engine module and shared core changes for worktree delivery,
fallback effort, session binding, failure reporting, and usage state. It adds
job-state fields and normalized event fields. Existing job records remain
readable, and the command surface adds `agy` as an engine choice.

```text
claive run --engine agy
  |
  +-- registry.get_engine("agy")
  +-- AgyEngine.resolve_launch      binary, model, session, permissions
  +-- AgyEngine.validate_launch     model, logging, options
  +-- core.prepare_isolation        creates the worktree
  +-- cwd = worktree                engine.cwd_is_worktree is true
  +-- AgyEngine.build_command       flags before -p <prompt>
  +-- driver reads NDJSON           raw events.jsonl
  |     init        -> activity
  |     step/result -> session_bound when a conversation id is present
  |     step_update -> model_step / output_delta / tool events
  |     result      -> terminal_completed, or terminal_failed
  +-- core binds session and usage  state, not the adapter
  +-- result.txt, usage, checks     unchanged
```

### Core change 1: deliver the worktree as the working directory

Add `cwd_is_worktree = True` to `WorkerEngine` in `bin/claivelib/engine.py`.
Pi, Claude, Codex, and AGY inherit it. Set `cwd_is_worktree = False` in the
Muse engine because Muse already receives the worktree path through
`--workspace` and `-w existing` (`bin/claivelib/engines/muse.py:220,241`).

The base default is `True`, not `False`. The registry also permits dynamically
registered engines. This default changes their behavior if they previously used
the parent workspace. Test registered engines explicitly, and document the
opt-out for engines that deliver isolation through their own flags.

Four sites stop naming engines and ask the capability instead:

* `bin/claivelib/cli.py:240`, the launch workspace choice. Use
  `get_engine(state.get("engine") or DEFAULT_ENGINE)` here.
* `bin/claivelib/cli.py:673`, the fallback workspace choice.
* `bin/claivelib/cli.py:909`, the `Popen` working directory.
* `bin/claivelib/cli.py:1851`, the launch state choice.

`prepare_isolation` already returns an existing worktree as mode `existing`,
and `state["actual_workspace"]` already points at it
(`bin/claivelib/cli.py:1897-1898`). `--worktree` is the CLI flag; it requests
the `create` isolation mode. The engine needs no separate worktree code.

Do not add a `validate_isolation` override. The base accepts `none`, `create`,
and `existing` (`bin/claivelib/engine.py:156-166`); an override that names
`base` or `existing` would reject the main `--worktree` path.

### Core change 2: derive and persist effort after a fallback

Keep `default_effort_for(model)` for new launches. Add
`fallback_effort_for(model, current)` to `WorkerEngine`: its default returns
`current`, preserving the selected effort for existing engines. AGY overrides
it to parse `high`, `medium`, or `low` from the new model id.

When no flag, role, or policy names a model, the launch uses
`getattr(engine, "default_model", None)` before it calls `default_effort_for`.
This is the fallback that makes `claive run --engine agy` work without `--model`
(`bin/claivelib/cli.py:1742`).

`rebuild_for_fallback` calls the fallback hook after it chooses the new model and before
it builds the `TurnRequest` (`bin/claivelib/cli.py:675-694`). It then writes the
derived value to both `state["reasoning_effort"]` and the job policy, so the
next turn and the policy check use the same value. A fallback from `-high` to
`-low` passes `check_engine_policy`; the old code kept `high`.

The supervisor owns persistence. Validate the rebuilt launch, turn, and policy
before saving. `state.json` atomically holds the new launch, model, effort,
session binding, and `turn_policy`. Then update `policy.json` as a compatibility
projection. If that write fails, retain state and report the projection error.
Follow-up uses state defaults when the projection is missing or stale.
An explicit `claive effort` command writes a model-scoped override without
replacing a running supervisor's state. Fallback invalidates an override for
the previous model. Test projection failure and the next policy read.
AGY reusable workers accept fallback. Other engines retain their prior refusal.
The fallback list is consumed across the worker's lifetime.
Fallback starts a new conversation. Ordinary resume retains the binding.

Both AGY hooks use the same suffix mapping. When the user supplies an explicit
effort, it must equal the suffix.
The launch order is: user or policy effort first, then
`default_effort_for(model)`. A mismatch fails in `validate_turn`.

### Core change 3: bind the session and account for usage in one place

The adapter cannot see job state. Both operations belong in `event_update`
(`bin/claivelib/cli.py:562`).

**Session binding.** On `session_bound`:

* If `state["session_id"]` is empty, write the id.
* If it is set and equal, ignore the duplicate.
* If it is set and different, mark the turn `terminal_failed` with kind
  `protocol`, set a per-attempt failure latch, and stop applying subsequent
  events. Do not overwrite the id or accept an answer after the mismatch.

`event_update` checks the latch before processing any event. The attempt reset
clears it. A mismatched result followed by a success event must remain failed.

A first-turn `init`, `step_update`, or `result` binds; later events compare.
A `result` with a different id fails the same way. The captured `init` carries
`conversation_id` at the envelope's top level, outside its nested `init` body.
This closes the silent fork: the adapter stays
stateless, and the core owns the check. Add a wrong-id regression test.

**Usage.** AGY's `result.usage` is cumulative within one conversation.
The adapter attaches `usage_cumulative=True` and raw counters to its terminal event.
The core stores a baseline per conversation and one aggregate per attempt.
Every record identifies turn, attempt, model, conversation, and completeness.
A missing result creates an incomplete record with unknown counters, not zero.
A declining verified cumulative counter fails the turn and preserves the baseline. Native cache counters remain unverified and are exempt.

`session_usage(state)` returns `model_calls: null`, `calls: []`,
`granularity: "attempt"`, `aggregates`, known `totals`, `usage_complete`,
`cache_hit_ratio: null`, and the latest `turn` with `index` and `complete`.
A result may cover multiple model calls. Per-call advice and growth consumers
use only actual call records. The CLI labels AGY records as attempt aggregates.
Each aggregate preserves the observed cache counter as `native_cache_read_tokens`. Do not subtract it or sum it into normalized totals.
Unknown cache semantics cannot support a cache ratio. Thinking is included
in output for the recorded samples and is not added again.

After missing usage, a later cumulative delta can span unreported attempts.
Mark that record with `spans_unreported_attempts` and incomplete usage.
Known totals remain visible as incomplete counters. Retain baseline fields absent from partial results. Only the first complete result after a gap spans unreported attempts. Later complete attempts recover normal attribution.
`usage_turns` and conversation baselines survive turn and fallback resets.
`turn_usage` and the failure latch reset before each turn and attempt.
No raw log scan is needed to serve usage.

### The AGY engine module

New file `bin/claivelib/engines/agy.py`. The class is `AgyEngine`, name `agy`,
`supports_fork = False`, `default_model = "gemini-3.8-flash-high"`. It
implements the contract as follows.

| Method | Behavior |
| --- | --- |
| Module-level `default_binary()` | `AGY_WORKER_BINARY`, else `~/.local/bin/agy`, else `agy` on `PATH`. Always return a string. Doctor calls this module function. |
| `default_effort_for(model)` | `None` returns `high`; otherwise parse the suffix and return it. |
| `supported_models()` | `{"gemini-3.8-flash-high": ("high",), "gemini-3.8-flash-medium": ("medium",), "gemini-3.8-flash-low": ("low",)}`. |
| `resolve_session_id(value, session_logging)` | Return `None` when `value` is `None`; raise for a user-supplied id. AGY cannot import an existing conversation. |
| `resolve_launch(**options)` | Build the standard launch dict and select soft read when requested. Raise for unsupported web, output schema, provider selection, step cap, session logging off, or a user session id. |
| `validate_launch(launch)` | Check the binary path, model id, session logging, and unsupported options. |
| `validate_turn(request)` | Re-check model and effort as a pair; refuse an explicit enforced-read requirement if enforcement is unavailable. |
| `build_command(request)` | Read the prompt and reject more than 100 KB in UTF-8 bytes; return the argv below. |
| `normalize_event(event)` | Map the envelope to a list of Claive event dicts. |
| `describe_event(raw_event)` | Return a list of `kind`/`text` dictionaries for `peek` and `show`. |
| `dedupe_key(event)` | Stateless raw-event key when unique; return `None` for ambiguous step identities. |
| `session_usage(state)` | Read `state["usage_turns"]` and return the dictionary above. |
| `steering_capability()` | Unsupported. |

`validate_turn` runs at `bin/claivelib/cli.py:1799`, before the job directory
is created, so a refused enforced-read requirement creates no job or worktree.
There is no `validate_isolation` override; worktree creation is core behavior.

### Model identity

| Stored model | Effort | Notes |
| --- | --- | --- |
| `gemini-3.8-flash-high` | high | Default |
| `gemini-3.8-flash-medium` | medium | |
| `gemini-3.8-flash-low` | low | Fallback target |

AGY 1.4.12 lists six models, including Claude and GPT-OSS. Claive's allowlist
is deliberately the three Flash ids; the engine does not claim AGY lacks the
others.

`--effort` is never passed to AGY. The suffix is the single source of truth. A
base alias such as `gemini-3.8-flash` is rejected. A launch with model `-high`
and explicit effort `low` fails in `validate_turn` with an error that names both
values. Selecting another effort means selecting another model id.

### Command line

```text
agy --output-format stream-json \
    [--dangerously-skip-permissions] \
    --model <model-id> \
    [--conversation <conversation-id>] \
    -p <prompt>
```

Rules:

* Put flags before `-p` and the prompt last. AGY 1.4.12 also accepted flags
  after `-p` in the probe; the earlier claim that they were ignored was wrong.
* No `--effort`, `--mode`, `--sandbox`, or `--print-timeout`. Claive owns
  cancellation and the turn deadline. AGY's default print timeout is `0`, which
  waits for the turn.
* Pass `--dangerously-skip-permissions` unless `CLAIVE_AGY_YOLO` is `0`,
  `false`, `no`, or `off`. The skip flag is the only way a headless turn avoids
  `request-review`. Without it, AGY can wait for approval until Claive's
  timeout. Keep the opt-out for debugging only.
* The prompt goes through `-p` as one argv value. It is never written to stdin
  and never passed through a shell. Reject a prompt over 100 KB before launch;
  the Linux per-argument limit is about 128 KB (`MAX_ARG_STRLEN`). `OSError`
  with `E2BIG` becomes a clear launch error that names the size.
* Omit `--conversation` on the first turn. Pass it on every later turn.
* The working directory is the worktree. AGY receives no workspace flag.
* Inherit the environment, including proxy and certificate variables. No AGY
  variable is required.
* stderr is kept for diagnosis. Only stdout is parsed.

### Event mapping

AGY wraps every event as `{"event": <name>, <name>: {...}}`. The adapter
rejects a mismatched outer key and raises `ValueError`; the driver counts that
line as malformed. `normalize_event` returns a list of dictionaries with the
fields below.

| AGY event | Claive event | Exact dict fields |
| --- | --- | --- |
| `init` | `activity` | `type`, `text` (model, cwd, tool count), `native_kind` |
| `init`, `step_update`, or `result`, when id is present | `session_bound` | `type`, `session_id`, `native_kind`; emit before other events from the envelope; read init's id from the outer envelope |
| `step_update`, text | `output_delta` | `type`, `text` from `text_delta`, `native_kind` |
| `step_update`, model step | `model_step` | `type`, `native_kind` |
| `step_update`, tool start | `tool_started` | `type`, `tool`, `tool_id`, `native_kind` |
| `step_update`, tool end | `tool_completed` | `type`, `tool`, `tool_id`, `native_kind` |
| `step_update`, `subagent` or `system_message` | `activity` | `type`, `text`, `native_kind`; these types occur in the recorded nested-worker probe |
| `step_update`, unknown | `activity` | `type`, `text`, `native_kind` |
| `result`, `SUCCESS` | `terminal_completed` | `type`, `terminal="completed"`, `text`, `reason`, `usage`, `conversation_id`, `native_kind` |
| `result`, `ERROR` | `terminal_failed` | `type`, `terminal="failed"`, `text`, `reason`, `usage`, `conversation_id`, `native_kind` |
| `result`, unknown | `terminal_failed` | `type`, `terminal="failed"`, `text`, `reason="protocol"`, `native_kind` |
| Unknown event | `activity` | `type`, `text`, `native_kind` |

`reason` is `null` or `completed` on success. `print_timeout` maps to
`timeout`; `interrupted` and `error` map to `worker`; any other nonempty value
maps to `protocol`. `text_delta` is used only when nonempty. The final
`result.response` is not added to `output_delta`, so the answer is not
duplicated.

`finished_reason` is AGY's input field. A failed normalized terminal event
also carries `failure_kind`, one of `protocol`, `timeout`, or `worker`.
`event_update` stores the validated value and the driver uses it before its
existing inference rules. A detected quota emits `quota_exhausted` and keeps
the existing approval semantics. The per-attempt failure latch prevents a
later event from clearing a protocol failure. Tests assert each failure kind
through the supervisor, including a mismatched id followed by success.

`describe_event(raw_event)` receives the AGY envelope and returns a list of
`{"kind": ..., "text": ...}` dictionaries. Use `text` for assistant output,
`tool_call` and `tool_result` for tool events, and `error` for failures. Test
the actual `steps_window` caller (`bin/claivelib/peek.py:52-92,127`).

`dedupe_key` returns `None`. One index covers several text deltas and both
tool boundaries. The peek caller skips whole adjacent events with equal keys.
Deduplicating by index would discard real output and tool results.
The final result also supplies a complete text summary for peek.

A turn fails when the process exits nonzero, when no `terminal_completed`
arrives, or when a malformed line arrives after `init`
(`bin/claivelib/cli.py:997-1008`). The driver keeps a local pre-init flag for
each process attempt. It records a pre-init banner as activity, and counts a
malformed line after init as an error. Reset the flag on resume and fallback.

### Session lifecycle

1. Turn 1 starts without `--conversation`.
2. An `init`, `step_update`, or `result` carrying a conversation id emits
   `session_bound` before its other events. The core stores the id only when
   the binding is empty (`bin/claivelib/cli.py:562`). Every later id is checked.
3. Every later turn passes `--conversation <id>`. The core fails the turn if a
   different id arrives; it does not overwrite the binding.
4. `--continue` is never used. It means the most recent conversation globally
   and is unsafe when jobs run in parallel.
5. A user-supplied `--session-id` is refused at launch. Claive can resume only
   sessions it created.
6. Steering and fork are refused before launch.

The conversation database stays at
`~/.gemini/antigravity-cli/conversations/<id>.db`, outside the job directory.
Claive does not copy or delete it. `--no-session-log` is refused because AGY
always persists the conversation.

### Usage

Three sampled `result.usage` records increased within one conversation. The
first reported 6,779 input and 60 output tokens; the third reported 21,130
input and 133 output tokens. These samples are consistent with cumulative
usage but do not establish all failure and cache semantics. The proposed core subtracts the stored baseline for that
conversation, records the delta in `state["turn_usage"]`, and appends it to
`state["usage_turns"]`.

If a turn has no result usage, its delta is unknown and its aggregate is incomplete. If a total decreases, the
turn fails as `protocol` and the baseline is unchanged. A fallback starts a new
conversation, so its first result becomes a fresh baseline, not a negative
delta.

Fields map as `input_tokens`, `output_tokens`, the native
`cache_read_tokens` counter, and `reasoning_tokens` from `thinking_tokens`. The live
probe reports 6,779 input, 60 output, 59 thinking and 6,839 total. Thinking is
included in output for this sample: do not add it again. Cache semantics remain
unproven because the samples reported zero cache reads. Preserve the native
counter without conversion and keep the normalized cache ratio unknown.

### Failure handling

| Condition | Result |
| --- | --- |
| Missing binary or not executable | Launch error before the job starts. |
| Prompt over 100 KB | Launch error naming the size. |
| Explicit enforced-read requirement, base model alias, unsupported option | Validation error before the job starts when unavailable. |
| Nonzero exit without a recognized quota or timeout | `worker`, unless malformed output or a missing terminal establishes `protocol`. |
| Missing `result` or malformed line after `init` | `terminal_failed`, kind `protocol`. |
| `result.status=ERROR` | `terminal_failed`, kind `worker` or `timeout`. |
| Conversation id changed between turns | `terminal_failed`, kind `protocol`. |
| Usage total decreased | `terminal_failed`, kind `protocol`. |
| Claive timeout | Claive kills the process; kind `timeout`. |
| Authentication message | `worker`; exact string from Phase 0. |
| Quota message | `quota` when Phase 0 records the string; otherwise `worker`. |

Claive's `FALLBACKABLE_FAILURES` decides whether a failed turn is retried
(`bin/claivelib/cli.py:627`). AGY adds no retry inside the adapter. A quota
failure is fallbackable only when the Phase 0 string marks it as one.

### Worktree, permissions, and concurrency

`--dangerously-skip-permissions` removes AGY's approval prompts. Claive's own
timeout and cancellation still apply. The worktree is the write boundary. It is
not a sandbox: AGY, like Pi and Claude, can run commands that write outside it
if the prompt asks. The design does not claim otherwise.

AGY's own subagents are allowed. `init.tools` lists 68 tools, including
`invoke_subagent`, `define_subagent`, and `manage_subagents`. No engine flag
restricts them, and Pi, Claude, and Codex also leave their subagents enabled.
Phase 0 verifies that AGY subagents inherit the worker's working directory. If
they do not, the worktree goal is weakened and the design must be revised.
One 2026-11-03 probe used `invoke_subagent` and wrote a file in the parent's
temporary cwd. This sample does not establish filesystem confinement.

AGY soft-read workers are not counted as enforced readers. Update workspace
policy and concurrency decisions to use the actual enforcement level. A soft
instruction is not proof that a worker cannot write.

AGY shares a global conversation store and user settings. Phase 0 tests two
jobs in one directory. If they interfere, Claive serializes AGY turns with an
advisory lock on `~/.gemini/antigravity-cli/.claive.lock`. The lock is held for
the turn, and a waiter that cannot acquire it within the start timeout is
refused with a clear error. If Phase 0 shows isolation, the lock is not added.
One 2026-11-03 probe started two prompt-only turns concurrently in one directory.
They returned distinct conversation IDs and the correct answers on sequential
resume. Concurrent edits and broader isolation remain untested.

Claive configuration already rejects `engines.agy.budget`: budgets are only
available for Claude. Do not add unreachable AGY budget checks. Test the actual
configuration rejection before launch.

### Registration and operator surfaces

* Add `agy` to `_MODULES` in `bin/claivelib/engines/__init__.py`.
* Derive one `ENGINE_NAMES` tuple from `_MODULES` and use it in the doctor loop
  (`bin/claivelib/cli.py:2195`) and `engines_report` (`:2342`). This removes the
  second and third hard-coded lists instead of growing them.
* Add `agy` to `OWNED_FLAGS` in `bin/claivelib/config.py:54-63`: `-p`, `--print`,
  `--output-format`, `--model`, `--conversation`, `--print-timeout`,
  `--dangerously-skip-permissions`.
* `claive engines` shows only the fields it already renders: engine, policy,
  model, and steering (`bin/claivelib/cli.py:2342-2370`). Do not promise binary,
  fork, or worktree columns. `claive doctor` reports binary availability.
* `default_binary()` returns a path string even when the binary is missing, as
  Codex does. The doctor's `ImportError`-only guard
  (`bin/claivelib/cli.py:2200-2204`) then needs no new exception class. Add a
  missing-binary test.
* Update `docs/command-reference.md:32,1114`, the engine table in
  `skills/claive-orchestration/SKILL.md:46`, `README.md:3`, the relay note in
  `README.md`, `docs/harness-integration.md:185-190`, and
  `skills/claive-routing-codex/SKILL.md:3`.
* Assert registry and CLI engine-list consistency in `tests/test_features.py`.
* `reference/agy-relay.sh` stays historical and is not wired into Claive.

Rollback keeps the module registered and removes `agy` from the allowed engine
policy, so `peek` and `claive usage` can still open existing jobs
(`bin/claivelib/peek.py:127`; `bin/claivelib/cli.py:3089`). Full module removal
happens only after every AGY job is closed.

### Verification

Unit tests with a fake `agy` binary that writes recorded NDJSON:

* Binary resolution from `AGY_WORKER_BINARY`, the home path, and `PATH`.
* `default_model`, the three-id mapping, `default_effort_for(None)`, a launch
  without `--model`, the base-alias refusal, and the mismatch error.
* Soft-read instruction and reported mode, enforced-read refusal when requested,
  and configuration rejection of `engines.agy.budget`.
* Conversation bind on turn 1, `--conversation` on turn 2, and a changed id
  failing the turn.
* `text_delta` becomes `output_delta`; the terminal text is not duplicated.
* Malformed output after `init`, unknown result status, and a decreasing usage
  total fail the turn.
* A pre-init banner is logged and does not fail the turn.
* Per-turn usage delta across two turns and across a fallback conversation;
  the exact `session_usage` dictionary.
* The command contains no `--effort`, `--mode`, `--sandbox`, or
  `--print-timeout`; flags come before `-p`.
* A prompt over 100 KB fails with a clear launch error.
* `CLAIVE_AGY_YOLO=0` omits the skip flag.
* `cwd_is_worktree` puts the worktree in `Popen` for AGY and keeps the parent
  workspace for Muse.

Regression tests: add AGY to the policy cases in
`tests/test_engine_policy.py`, and add a Muse case plus AGY to
`tests/test_isolation_all_engines.py`. The existing isolation test covers only
`codex`, `claude`, and `pi`. Add the engine-name expectation to
`tests/test_features.py:117`. Keep `tests/test_peek.py:454` for the
`describe_event` fallback; it is not an engine-list test. Add a `peek` case for
an AGY `step_update`.

Live opt-in test, gated by `CLAIVE_AGY_LIVE=1`:

1. Run a trivial job and assert `SUCCESS`.
2. Run a worktree job that writes a random nonce to a file, assert the edit is
   in the worktree, and assert `git status` in the parent is unchanged.
3. Run a follow-up asking for the nonce and assert the answer contains it.
4. Assert the follow-up's `turn.input_tokens` equals the exact delta from
   `result.usage`, not the cumulative total.
5. Run a read-only AGY task and check that soft read is reported. Do not claim
   the prompt enforces filesystem access. Check hard-read refusal separately.

The high-to-low fallback runs in the fake-binary unit test, because a live AGY
turn succeeds on the primary and never triggers fallback. A manual
`claive show` and `claive usage` check confirms the rendered events and
numbers.

### Rollout and rollback

No feature flag is needed. The new module is inactive until `--engine agy` is
used. Pi, Claude, and Codex inherit `cwd_is_worktree = True`; Muse declares
`False`. Registered third-party engines must check the changed default.

Rollback disables `agy` in the allowed engine policy and keeps the module
registered; existing jobs stay readable. If the module must be removed later,
do it only after all AGY jobs are closed, because `peek` and `claive usage` call
`get_engine("agy")`.

## Non-Functional Requirements

### Security and privacy

`--dangerously-skip-permissions` lets AGY run tools without approval prompts.
The worktree is the write boundary for the task, but it is not a sandbox: a
command can still write outside it. The documentation must say this next to
the engine entry. `CLAIVE_AGY_YOLO=0` disables the skip flag, and the
documentation must warn that approvals can stall a headless turn. The design
adds no network service, no credential store, and no secret handling.

Claive's copy of the prompt, normalized events, answer, and stderr stay in the
job directory. AGY also records the prompt in its conversation database under
`~/.gemini/antigravity-cli/conversations/`, outside Claive's retention. The
design does not copy, encrypt, or delete that store. Soft read is advisory and
must not be treated as enforced access by the scheduler or workspace policy.

### Performance and cost

Each turn starts one AGY process and parses its stdout as a stream. AGY can load
user settings, tools, and instructions that add token overhead. Startup,
first-token latency, and available overhead measurements belong in Phase 0.
Memory stays bounded by the event line size, not the conversation size.

Usage is reported per turn. A configured budget is refused, not enforced.
AGY's free tier can refuse a turn; that becomes a worker or quota failure once
Phase 0 records the string. Cache-hit accounting depends on whether the usage
records separate cache reads from input tokens. If the ratio cannot be
calculated, `cache_hit_ratio` is null, not zero.

### Reliability

The adapter fails closed: malformed output after `init`, an unknown status, a
changed conversation id, or a decreasing usage total ends the turn as
`protocol`. It does not retry inside the adapter. Claive's timeout and
cancellation remain the only stop mechanism. A fallback starts a new AGY
conversation, so it cannot inherit a broken session.

### Compatibility

The design targets `agy` 1.4.12 and uses only documented command options. It
uses the same Python interpreter and standard library as the other engines and
adds no dependency. The event schema and job state stay backward compatible.
Usage is additive state; an older job without `usage_turns` reports zeros.

### Release and rollback

No feature flag is needed. The engine is inactive until `--engine agy` is used.
Rollback disables the engine in policy and keeps the module, so existing jobs
stay readable. Removal follows after all AGY jobs are closed.

### Additional Resources

* [concepts_brief.md](../../concepts_brief.md), the approved brainstorm brief.
* `agy --help` and `agy models` output from version 1.4.12.
* The Phase 0 probe transcripts and fixtures under `tests/fixtures/agy/`.
* `bin/claivelib/engines/pi.py`, `claude.py`, and `codex.py`, used as the
  contract reference.
* `docs/command-reference.md` and `skills/claive-orchestration/SKILL.md`.

### Future Improvements

* **Read-only support**: adopt `--sandbox` only if a later probe proves it is a
  write barrier that composes with permission skipping.
* **Steering and fork**: add them if AGY exposes stable flags and a forkable
  conversation id.
* **Retention**: add cleanup for AGY conversation databases when a supported
  delete command exists.
* **Cache accounting**: separate cache reads from input tokens if AGY exposes
  them.
* **Plan artifacts**: collect the plan file when a future workflow needs it.
* **Orchestrator selection**: let orchestration choose AGY after the engine has
  a live acceptance record.

## Detailed Implementation Plan

### Required checks during implementation

These checks take precedence over conflicting examples and phase text below.
They are release criteria, not claims that the current draft implements them.
Use recorded AGY streams where available and label synthetic failure streams.
Exercise the supervisor and its real consumers, not only adapter functions.

1. **Conversation identity:** On every resume, compare each reported
   conversation ID with the saved ID. A mismatch must fail the attempt and
   must not replace the saved ID or accept a later success. Drive a fake AGY
   process through a follow-up that emits a different ID and then success.
   Assert the failed job, retained ID, and absence of an accepted answer.
   Also run the live follow-up nonce check in Phase 5.
2. **Timeout:** Inspect `finished_reason` even when the process exits zero
   and `status` is `SUCCESS`. Replay the captured `print_timeout` result
   through the supervisor. Assert a timeout failure and no completed answer.
   Test the precedence of cancellation, Claive timeout, protocol failure,
   provider failure, and process exit with combined signals. Specify that
   precedence before coding it, and preserve existing engines' behavior.
3. **Usage:** Resolve the aggregate usage contract before implementing usage
   output. AGY result records do not establish the provider's model-call
   count. Do not label result counts as model calls or pass aggregate output
   to per-call advice. Represent missing usage and unknown cache ratios as
   unknown, not zero. Exercise `claive usage --json`, its text output, and
   wait advice for two turns, missing usage, and fallback. Check other engines'
   existing output and consumers, including orchestration. Assert that no
   unsupported call count, cache ratio, or heavy-call claim is displayed.
4. **Fallback recovery:** Before implementing persistence, specify a
   recoverable source of truth or recovery protocol for `state.json` and
   `policy.json`. Validate the new launch and request before any persistence.
   With a fake binary, test high-to-low fallback and a subsequent follow-up.
   Inject a write failure or interruption at each persistence boundary. Assert
   that the next attempt or follow-up uses a consistent model and effort, or
   fails clearly before launch. Never assume both files update atomically.
5. **Launch and isolation:** Catch `E2BIG` at process launch and report a
   useful error. Test that path independently of the prompt-size precheck.
   Test soft-read selection and enforced-read refusal before job creation.
   Check a live worktree edit and
   the parent repository. Treat the working directory as a placement mechanism,
   not filesystem confinement. Use separate worktrees for concurrent edits.
6. **Evidence and completion:** Run the targeted tests after each affected
   phase, then `just check` and the opt-in live acceptance test. Record the
   commands, results, unavailable cases, and any remaining assumptions. Do not
   call the integration complete if a required check fails or is not run.

### Target Files to Create/Modify

| File | Action | Scope |
| --- | --- | --- |
| `bin/claivelib/engines/agy.py` | Create | The `AgyEngine` adapter: binary, model, launch, command, events, session, usage. |
| `bin/claivelib/engine.py` | Modify | Add `cwd_is_worktree` and `fallback_effort_for` on `WorkerEngine`. |
| `bin/claivelib/cli.py` | Modify | Four cwd sites, fallback validation and persistence, session binding, usage state, failure latch, stream parsing, doctor and engine-list loops. |
| `bin/claivelib/engines/__init__.py` | Modify | Register `agy`; expose `ENGINE_NAMES`. |
| `bin/claivelib/engines/muse.py` | Modify | Declare `cwd_is_worktree = False`. |
| `bin/claivelib/engines/pi.py`, `claude.py`, `codex.py` | Unchanged | Inherit `cwd_is_worktree = True`. |
| `bin/claivelib/config.py` | Modify | Add the `agy` entry to `OWNED_FLAGS`. |
| `tests/test_agy_engine.py` | Create | Unit tests and fixture replay for the adapter. |
| `tests/test_agy_live.py` | Create | Opt-in live acceptance test. |
| `tests/fixtures/agy/` | Create | Recorded NDJSON, probe notes, and the edit proof. |
| `tests/test_isolation_all_engines.py`, `tests/test_engine_policy.py` | Modify | Muse and AGY worktree cases; AGY budget config rejection. |
| `tests/test_worker_fallback.py` | Modify | Fake AGY high-to-low fallback, state and policy persistence, and follow-up. |
| `tests/test_features.py`, `tests/test_peek.py` | Modify | Engine-name and `peek` expectations. |
| `tests/hermetic.py` | Modify | Capture `AGY_LIVE`; scrub `AGY_*` and `CLAIVE_AGY_*`. |
| `docs/command-reference.md`, `skills/claive-orchestration/SKILL.md`, `README.md`, `docs/harness-integration.md`, `skills/claive-routing-codex/SKILL.md` | Modify | Engine entry, refusal text, and relay status. |

### Verification Plan

* **Compilation**: `python3 -m compileall -q bin/claivelib tests`.
* **Unit tests**: `just test-one tests/test_agy_engine.py` plus the touched
  regression files.
* **Full suite**: `just test` (about four minutes).
* **Lint**: `just lint`, which runs compileall, `ruff` on `F` rules, and
  `shellcheck`.
* **Live test**: `CLAIVE_AGY_LIVE=1 just test-one tests/test_agy_live.py`.
* **Manual checks**: `claive run --engine agy`, `claive show <job>`,
  `claive usage <job>`, and `git status` in the parent repository.
* **Pass criteria**: the worktree edit lands only in the worktree; the parent
  is clean; the follow-up echoes the nonce; the usage delta matches the turn;
  soft read is reported honestly; enforced-read refusal creates no job; the
  fake fallback starts.
* **Coverage**: the repository has no numeric coverage gate. Every new failure
  branch has a fixture or fake-binary test.

Each phase ends with its focused tests. The user instruction to complete all phases authorizes implementation of the
revised contracts in this document. The design adds normalized event fields;
test old-engine events and historical job records for compatibility.

### Phase 0: Probe AGY and capture fixtures

This phase answers the Open Questions and writes no production code.

1. Record `agy --version` and `agy models` in `tests/fixtures/agy/README.md`.
2. Run one turn with `--output-format json` and one with `stream-json`.
   Save both streams as `success.ndjson` and `success.json`.
3. Run a two-turn conversation with `--conversation` and save both streams as
   `resume.ndjson`. Confirm that usage grows and that text and tool steps are
   distinguishable.
4. Run an edit turn in a temporary git repository. Confirm the file changes,
   the exit code, and whether `--dangerously-skip-permissions` permits edits
   without `--mode`.
5. Run `--conversation` with an unknown id and record the warning and exit
   code as `unknown_conversation.txt`.
6. Check the OS argument limit without a model request, then separately send
   an accepted-size model prompt. Record the argv and model results separately.
7. Run with the flags before `-p` and with `-p` first. Record both results.
8. Start two conversations in one directory and record whether they interleave
   or serialize.
9. Capture a run with a startup banner if one occurs; save `banner.ndjson`.
10. Record the authentication failure text already present in the probe log,
    without revoking credentials. If a quota failure occurs naturally, record
    its exact string.
11. Compare print-mode runs with a minimal configuration and with the user
    configuration. Record MCP and skill loading and token overhead.
12. Run a delegated edit in a temporary worktree and inspect the child cwd.
13. Extend the resumed conversation to three turns. Record whether step indices
    reset and whether token usage is cumulative for each attempt.

Fixtures: `fake_agy.py`, `success.ndjson`, `resume.ndjson`, `error.ndjson`,
`timeout.ndjson`, and `banner.ndjson`. The fake binary reads a scenario name
from `FAKE_AGY_SCENARIO` and writes the matching stream. If an error, timeout,
or banner cannot be captured, synthesize the fixture and label it synthetic.
Synthetic data is not evidence that AGY emits that shape.

Exit criteria: every Open Question has a recorded result or an explicit unknown;
the edit proof and sanitized fixtures are stored. Error, timeout, and banner
fixtures must identify whether they are synthetic. The accepted prompt limit,
flag order, and unavailable cases are documented. Required unknowns use conservative limits: a 100,000-byte host prompt guard,
unknown cache ratios, and inherited user configuration without an overhead
guarantee. These revisions avoid unsupported claims. An unavailable quota string
is not a reason to provoke one.

### Phase 1: Core capabilities

1. Add `cwd_is_worktree = True` and `fallback_effort_for(model, current)` to
   `bin/claivelib/engine.py`. The fallback default returns `current`.
2. Set `cwd_is_worktree = False` in `muse.py`. The other engines inherit `True`.
3. Replace the four engine-name checks with the capability:
   `bin/claivelib/cli.py:240`, `:673`, `:909`, and `:1851`. At `:240`, use
   `get_engine(state.get("engine") or DEFAULT_ENGINE)`.
4. In `bin/claivelib/cli.py:1742`, fall back to
   `getattr(engine, "default_model", None)` before calling
   `default_effort_for`.
5. Call `fallback_effort_for` in `rebuild_for_fallback` after the new model is
   chosen (`bin/claivelib/cli.py:675-694`). Persist the result in
   `state["reasoning_effort"]` and the job policy.
6. Add the core session binding and usage state in `event_update`
   (`bin/claivelib/cli.py:562`).

Tests: a newly registered fake engine with no attribute gets the worktree;
Muse keeps the parent; Pi, Claude, and Codex keep the worktree.
`tests/test_agy_engine.py` covers high-to-low fallback with a fake binary. Existing fallback regression remains in `tests/test_worker_fallback.py`.
Assert both invocations, low model and effort in state and policy, the answer,
and a subsequent low-effort follow-up. Run `just test-one` on that file,
`tests/test_engine_policy.py`, and `tests/test_isolation_all_engines.py`.

Exit criteria: all four existing engines behave as before; Muse still uses
`--workspace`; no engine-name branch remains at the four sites; a fallback
follow-up keeps the derived effort.

### Phase 2: The AGY adapter

1. Create `bin/claivelib/engines/agy.py` with the class and methods in the
   Technical Plan. Use only standard-library imports, as the other engines do.
2. Add `agy` to `_MODULES` in `bin/claivelib/engines/__init__.py`.
3. Derive `ENGINE_NAMES` from `_MODULES` and use it in the doctor loop
   (`bin/claivelib/cli.py:2195`) and `engines_report` (`:2342`).
4. Add the `agy` flag set to `OWNED_FLAGS` in
   `bin/claivelib/config.py:54-63`.
5. Build the command with the flags before `-p`, `stream-json`, the skip flag,
   and the model. Omit `--conversation` on the first turn. Omit
   `--print-timeout`.

Tests in `tests/test_agy_engine.py` with a fake binary:

* Binary resolution from the environment, home path, and `PATH`.
* Model and effort mapping, base-alias refusal, and mismatch refusal.
* A subprocess test checks soft-read launch, instruction and reported mode. An
  explicit enforced-read request leaves no job directory when unsupported. A
  separate configuration test checks that an AGY budget is rejected.
* First turn omits `--conversation`; later turns include the bound id.
* The command has no `--effort`, `--mode`, `--sandbox`, or `--print-timeout`;
  flags come before `-p`.
* A missing binary and a prompt over 100 KB produce clear launch errors.

Run `just test-one tests/test_agy_engine.py`.

### Phase 3: Events, sessions, and usage

1. Implement `normalize_event`, `describe_event`, and `dedupe_key` for `init`,
   `step_update`, and `result`, with the fields and failure rules in the
   mapping table.
2. Emit `session_bound` from `init`, `step_update`, or `result` when an id is present;
   the core binds only when the state is empty.
3. Record usage deltas in `usage_turns` from the terminal event, using the
   baseline for the conversation.
4. Emit `terminal_failed` for malformed lines after `init`, unknown status, a
   changed id, and a decreasing usage total.
5. Fail any nonempty `finished_reason` other than `completed`; map
   `print_timeout` to the timeout failure kind.
6. Skip a non-JSON banner before `init` as `activity`; fail a malformed line
   after `init`.

Tests in `tests/test_agy_engine.py` replay the Phase 0 fixtures:

* `text_delta` becomes `output_delta`; the terminal text is present once.
* A two-turn conversation resumes and reports the turn delta.
* A fallback conversation resets the usage baseline.
* Step usage does not double count; the exact `session_usage` dictionary is
  returned.
* A nonempty `finished_reason` other than `completed` fails the turn.
* `CLAIVE_AGY_YOLO=0` omits the skip flag.
* Malformed NDJSON after `init`, unknown status, and missing `result` fail the
  turn.
* A changed conversation id fails before the answer is accepted.
* A pre-init banner passes; a post-init banner fails.
* `peek` renders an AGY `step_update` through `describe_event`.

Run `just test-one tests/test_agy_engine.py tests/test_peek.py`.

### Phase 4: Operator surfaces and documentation

1. Add the `agy` row to `claive engines` and `claive doctor`, including a
   missing-binary failure that does not crash the doctor loop.
2. Update the docs and skill files listed in the target table.
3. Add AGY to the engine-name tests in `tests/test_features.py:117`.
4. Extend `tests/hermetic.py` to capture `AGY_LIVE` beside `LIVE` and to scrub
   `AGY_*` and `CLAIVE_AGY_*`. Set `AGY_WORKER_BINARY` to a nonexistent sandbox
   path by default. Tests that require AGY set it to their fake executable.
5. Add the Muse case and AGY to `tests/test_isolation_all_engines.py`. Its
   AGY fixture emits AGY init and result envelopes and explicitly sets
   `AGY_WORKER_BINARY` to the fixture.
6. Add an engine enumeration assertion to `tests/test_features.py`: compare
   the exported engine names to the registry and check both CLI surfaces.

Run `just test-one tests/test_features.py tests/test_isolation_all_engines.py`.

Exit criteria: `claive engines` shows engine, policy, model, and steering;
`claive doctor` reports AGY without running a model; the docs name the engine
and the refusal text; the engine enumeration assertion passes.

### Phase 5: Live acceptance

Add `tests/test_agy_live.py`, skipped unless `AGY_LIVE` (captured from
`CLAIVE_AGY_LIVE`) is set and `agy` is executable. It runs the five checks in
the Verification section: trivial job, worktree edit with a clean parent,
follow-up nonce, exact usage delta, and soft-read behavior with honest reporting.

Run `CLAIVE_AGY_LIVE=1 just test-one tests/test_agy_live.py`, then
`claive show <job>` and `claive usage <job>` on the created job.

Exit criteria: all five checks pass on this host with `agy` 1.4.12, and the
parent repository is unchanged after the worktree edit.

### Phase 6: Full regression and review

Run `just check`. It compiles, runs `ruff` on `F` rules, runs `shellcheck`,
and runs the full suite. Review the diff against this design before any merge.

Exit criteria: `just check` passes; no skipped AGY unit test; the live test is
opt-in only; the design and the command reference agree on the refusal text.

### Dependencies

| Phase | Depends on | Reason |
| --- | --- | --- |
| 0 | None | Probes settle the unknowns. |
| 1 | None | Core hooks do not need AGY. |
| 2 | 0, 1 | The adapter needs fixtures, model answers, and the worktree hook. |
| 3 | 0, 2 | Event shapes come from Phase 0; the module comes from Phase 2. |
| 4 | 2 | Surfaces need a registered engine. |
| 5 | 1-4 | Live checks use the full path. |
| 6 | 5 | The full suite follows acceptance. |

### Requirement to test matrix

| Requirement | Test | Phase |
| --- | --- | --- |
| Worktree is the AGY working directory | `tests/test_isolation_all_engines.py`, live test | 1, 5 |
| Muse keeps flag delivery | `tests/test_isolation_all_engines.py` | 1 |
| Fallback re-derives and saves effort | `tests/test_worker_fallback.py` | 1 |
| Soft read reported; enforced read refused when unsupported | `tests/test_agy_engine.py`, live test | 2, 5 |
| AGY budget rejected by config | `tests/test_engine_policy.py` | 2 |
| Model suffix is canonical | `tests/test_agy_engine.py` | 2 |
| Resume uses the bound id and a nonce | `tests/test_agy_engine.py`, live test | 3, 5 |
| Per-turn usage delta | `tests/test_agy_engine.py`, live test | 3, 5 |
| Doctor and engine list show AGY | `tests/test_features.py` | 4 |
| Full suite and lint | `just check` | 6 |

### Rollback points

* Phase 1: remove `cwd_is_worktree` from the base and Muse, and restore the four branches.
  No state changes.
* Phase 2: remove `agy` from the allowed engine policy; keep the module so
  `peek` and `usage` can read existing jobs.
* Phase 3: usage state is additive. Keep it; removing it changes history for
  new jobs only.
* Phase 4: documentation and surfaces can revert without touching jobs.
