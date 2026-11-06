# AGY as a fifth Claive engine

* **Author:** Ahmed Hamza
* **Date:** 2026-11-02
* **Status:** Brainstorm complete. Not an implementation plan.

**Updated decision, 2026-11-03:** A read-only request uses verified native
enforcement where available and otherwise uses soft read mode. AGY has no
verified native enforcement, so it uses a clearly reported soft instruction.
This decision supersedes the earlier AGY read-only refusal.

## Overview

Claive starts agent CLIs as background workers. Each supported CLI is an engine:
`muse`, `pi`, `claude`, `codex`. This proposal adds the Antigravity CLI `agy`
1.4.12 as a fifth engine, so `claive run --engine agy` gets the same lifecycle:
launch, tracked job, normalized events, answer file, resume, follow-up turn,
usage, checks, and acceptance.

The target model is Gemini 3.8 Flash with `high`, `medium`, or `low` effort.
Today AGY is reachable only through `reference/agy-relay.sh`, which prints text
and truncates the answer at 12 KB. Claive requires NDJSON, so the relay cannot
be a worker. AGY already emits typed NDJSON and resumes with `--conversation`,
so the adapter is a native engine module, not a wrapper process.

The value is one worker pool with one job model. A caller can choose Gemini
Flash without learning a second CLI, a second log format, or a second session
store. No cross-harness protocol is needed.

## Analysis

### What already fits

- AGY emits `init`, `step_update`, and `result` records in `--output-format
  stream-json`. Claive's normalized vocabulary covers those three shapes.
- `--conversation <id>` gives real resume. Claive already has a `session_bound`
  event for engines that learn their session id after launch (`bin/claivelib/cli.py:615-621`).
- A live probe on this host returned
  `{"conversation_id":"8c76ef2a-…","status":"SUCCESS","response":"OK\n",…,"usage":{"input_tokens":6775,"output_tokens":33,"thinking_tokens":32,"cache_read_tokens":0,"total_tokens":6808}}`.
  Auth works as of 17:18 today. The earlier "not logged into Antigravity" note
  in one review came from a stale log read, not from the CLI.
- The adapter surface is small. `bin/claivelib/engines/pi.py` and
  `bin/claivelib/engines/codex.py` already show the shape: resolve launch,
  validate, build argv, normalize events.

### Finding 1: worktree isolation silently does not reach AGY (blocker)

`prepare_isolation` creates a worktree for every job. Four sites then choose the
child's working directory with a hard-coded set `{"codex","claude","pi"}`:
`bin/claivelib/cli.py:240`, `:673`, `:909`, `:1851`. An engine outside that set
runs in `state["workspace"]`, the original repository, while Claive records and
checks the worktree (`bin/claivelib/cli.py:1897-1898`).

The result is a silent correctness bug. The edit lands in the parent repo. The
worktree stays clean. Acceptance checks the wrong tree. `claive close` removes
the clean worktree, and the parent repo has no record of the change. AGY has no
workspace flag of its own, so cwd is the only delivery mechanism.

The fix is not a fifth literal. Give `WorkerEngine` a capability attribute, for
example `workspace_delivery = "cwd"`, with Muse declaring `"argv"`. Replace all
four sets with that attribute. Then a new engine is correct by default.

### Finding 2: session identity is server-chosen (blocker)

Claive seeds `state["session_id"]` before launch and treats it as the engine
session. Pi, Claude, and Codex can create a session under a caller-chosen id.
AGY cannot. There is no `--session-id`. `--conversation` only resumes an id that
AGY already created.

A naive adapter fails in two ways. Passing `--conversation <fresh-uuid>` on turn
one prints `warning: conversation "…" not found`, exits 0, returns `SUCCESS`,
and starts a new conversation. Never passing it loses context on every follow-up.
Both look successful to the supervisor.

The correct sequence is: no `--conversation` on a fresh turn, bind
`conversation_id` from `init` or `result`, pass it on later turns, and fail the
turn if AGY returns a different id than the one requested. `-c/--continue`
attaches to the most recent conversation on the machine and is unsafe with
parallel workers, so the adapter must never use it.

`--no-session-log` also cannot be honored. AGY always writes a conversation
SQLite file under `~/.gemini/antigravity-cli/conversations/`. The first slice
should refuse that flag with a clear error instead of retaining history
silently.

### Finding 3: model and effort are two conflicting knobs (blocker)

Claive stores `model` and `reasoning_effort` separately (`bin/claivelib/engine.py:12-14`).
AGY encodes effort in the model id (`gemini-3.8-flash-high|medium|low`) and also
accepts `--effort`. The two must agree. A probe with
`--model gemini-3.8-flash-high --effort low` exited 1 with
`status=ERROR` and `error: invalid model selection … conflicts with --effort=low`.

Fallback makes this worse. `rebuild_for_fallback` keeps the stored effort while
swapping the model (`bin/claivelib/cli.py:675,684-693`). A fallback from
`gemini-3.8-flash-high` to `gemini-3.8-flash-low` would be a guaranteed failure
at the CLI.

One representation must be canonical. The stored model is the exact suffixed
id, the effort is derived from that suffix, `--effort` is never passed, and a
conflicting explicit pair is rejected at `validate_turn`. The base alias is not
accepted. Fallback tokens are suffixed ids.

Because `rebuild_for_fallback` keeps the old effort, a cross-effort fallback
must re-derive effort from the new model id. That needs a small engine hook, or
the rebuild path must use the launch's derived effort. Without it, a fallback
from `-high` to `-low` still fails `check_engine_policy` before AGY starts.

### Finding 4: stdout purity and terminal status decide success (blocker)

The supervisor parses every stdout line as JSON and fails the turn if any line
is malformed, the exit code is nonzero, or no `terminal_completed` event arrives
(`bin/claivelib/cli.py:955-1008`). A banner, warning, or blank line on stdout
fails a turn that may have made the correct edit.

`result.status` uses `SUCCESS` and `ERROR`. A naive adapter that maps every
`result` to `terminal_completed` records failures as success. Newer AGY versions
also add `finished_reason` values `print_timeout`, `interrupted`, and `error`,
with a partial answer. Those must map to `terminal_failed`, not completed.

The first slice should use `--output-format stream-json`, send diagnostics to
`--log-file`, fail closed on unknown status, and emit a terminal event only from
`result`. Tool steps need a stable id from `step_index` so `claive show` can
clear active tools. Failed tool steps should produce `task_warning`, and
quota/rate-limit text should map to `quota_exhausted` so fallback backoff works.

### Finding 5: AGY has no verified enforced read-only mode

`--mode plan` blocks workspace edits. It is not a read-only worker mode. It
writes a plan artifact under `~/.gemini/antigravity-cli/brain/<conversation_id>/`
and returns `SUCCESS` with "created the implementation plan… please review". A
verifier that is asked to read tests and report would instead ask a human to
approve a plan.

`--sandbox` might be a real boundary, but its interaction with
`--dangerously-skip-permissions` is unprobed. AGY's terminal sandbox also cannot
run on this host because AppArmor restricts unprivileged user namespaces.

The user's updated decision supersedes the prior refusal policy. A read-only
request uses verified native enforcement where available. Otherwise Claive
instructs the agent to read and not write, and reports soft read mode. AGY uses
soft read while its native enforcement remains unverified. Plan mode is not a
substitute. Workspace policy and concurrency decisions must not treat an
instruction as enforced write prevention.

### Secondary constraints

- **Permissions.** The default `permission_mode` is `request-review`. Claive has
  no approval channel, so a headless turn would stall until timeout, possibly
  forever. Always passing `--dangerously-skip-permissions` is a liveness
  requirement, not a convenience. Keep a one-line environment opt-out, mirroring
  Codex's `_yolo()` gate (`bin/claivelib/engines/codex.py:35-37`).
- **Blocking tools.** `init.tools` includes `ask_question` and
  `invoke_subagent`. AGY has no tool-selection flag. `ask_question` can block a
  headless turn. Mitigate with a prompt-level ban and Claive's turn timeout, and
  state the residual risk.
- **Usage.** AGY reports cumulative per-conversation usage. Reporting the
  `result` total per turn overcounts quadratically (7.5k, 16.1k, 25.7k over three
  turns). Sum `step_update` deltas into the job's `events.jsonl`, dedupe by
  `(conversation_id, step_index)`, and never read the protobuf conversation DB.
  A trivial turn already costs about 6.8k input tokens, so silence would hide
  real cost.
- **Host state.** Claive passes the full environment. AGY reads the user's
  settings, MCP servers, and global conversation store. Codex and Claude isolate
  their user config. AGY has `--settings`, but its isolation behavior is
  unprobed. The first slice documents the shared-store risk and must not run two
  concurrent workers until a lock test passes.
- **Retention.** Every turn adds roughly 64–96 KB to a conversation DB whose
  baseline is about 425 KB. Claive's session GC does not cover
  `~/.gemini/antigravity-cli/`. A purge-by-conversation option on `claive close`
  is needed before regular use.
- **Prompt transport.** AGY has no prompt-file flag. The working form is
  `agy -p "<text>"`, as the relay and Codex already do. Claive can compose up to
  ten 256 KiB context layers, so the adapter needs an argv length guard. Stdin
  transport is unverified and stays out of the first slice.
- **Unsupported flags.** The adapter must reject `--web`, `--output-schema`,
  `--max-model-steps`, `--fork-session-id`, foreign `--provider`, `--session-id`,
  `--no-session-log`, and `--sandbox` in the first slice. Silent acceptance is
  worse than refusal.
- **Subagents.** The decision is no restriction, matching Pi's `--tools "*"`
  (`bin/claivelib/engines/pi.py:171`), Claude's `--tools default`
  (`bin/claivelib/engines/claude.py:175`), and Codex's absence of a subagent
  flag. The `CLAIVE_WORKER_ID` recursion guard stays in force. Subagent writes
  remain unattributed and are caught at acceptance, as with Claude.
- **Drift.** Engine names are duplicated in `bin/claivelib/cli.py:2195` and
  `:2342`, `bin/claivelib/config.py:54-63`, `docs/command-reference.md:32,1114`,
  `docs/harness-integration.md:185-190`, `skills/claive-orchestration/SKILL.md:46`,
  `skills/claive-routing-codex/SKILL.md:3`, and tests. The registry is never
  iterated for reporting. Doctor additionally calls a module-level
  `default_binary()` and catches only `ImportError`
  (`bin/claivelib/cli.py:2199-2204`). A missing function crashes doctor.
- **Orchestration.** `implementer_choice` considers only Muse and Pi
  (`bin/claivelib/orchestration.py:531-560`). AGY is explicit-engine only in the
  first slice. It must not be added to the `claive-orch` roster, which authorizes
  only Muse and Pi.

### Capability matrix

| Capability | AGY target | Mechanism | Evidence |
|---|---|---|---|
| Write turn | supported | default mode plus skip-permissions | probe created files |
| Worktree | supported after core fix | process cwd, capability attribute | code path verified |
| Resume | supported | bind id, then `--conversation` | unknown id warns and restarts |
| Read-only | soft read | explicit instruction, reported as not enforced | no verified native enforcement; plan mode returns a plan |
| Web | refused | no provider search flag | help surface |
| Steering | unclaimed | `--input-format stream-json` is queued turns, not in-flight input | docs |
| Fork | refused | none, copying SQLite is not a fork | help surface |
| Step cap | refused | no flag | help surface |
| Output schema | refused in slice 1 | `--json-schema` exists | help surface |
| Usage | supported | sum step deltas | cumulative probe |
| Session log off | refused | AGY always persists | state store |
| Subagents | unrestricted | no tool flag, recursion guard stays | `init.tools` |

## Goals vs. Non-Goals

**In scope**

- Register `agy` and run a write turn with `--engine agy`.
- Deliver the Claive worktree as the process cwd for AGY.
- Bind and resume an AGY conversation across turns.
- Run read-only requests in clearly reported soft read mode until AGY has verified native enforcement.
- Usage from normalized step deltas.
- Doctor and `claive engines` rows, owned flags, docs, and tests.
- A live opt-in test that proves an edit, a check, resume, and no parent-repo
  writes.

**Out of scope**

- In-flight steering and session fork.
- Web search, output schema, step cap, sandbox, provider selection.
- `--no-session-log` support and automatic retention GC.
- Budget accounting beyond usage.
- Orchestrator auto-selection of AGY.
- AGY plan mode and plan-artifact collection as substitutes for read-only.
- Unrelated engine changes. Shared read-mode handling applies to all adapters.

## Alternatives Weighed

- **Extend `reference/agy-relay.sh`.** Rejected. It prints text, truncates at
  12 KB, and hides the conversation id. Non-JSON stdout fails every Claive turn.
  Keep the relay as documentation of the invocation shape.
- **Write a wrapper process, as Codex needed.** Rejected. AGY already emits
  NDJSON. A wrapper would reimplement job semantics outside Claive's state
  machine.
- **Pursue full Pi parity.** Rejected for the first slice. Steering, fork,
  transcript parsing, and budget would double or triple the diff without
  improving the stated success test.
- **One base model plus `--effort`.** Rejected. AGY treats the effort suffix as
  part of the model id, and a mismatch is a hard error. It also makes fallback
  tokens and resume state ambiguous.
- **Three suffixed model ids, one effort each.** Adopted. Changing effort means
  selecting another suffixed model. The adapter never passes `--effort`, so the
  two knobs cannot disagree.
- **Use `--mode plan` for read-only turns.** Rejected. Plan mode returns a plan
  for approval, not a read-only investigation, and it would weaken Claive's
  read-only concurrency guarantee.
- **Refuse all read-only AGY turns.** Superseded by the user's decision to use
  clearly identified soft read when enforced read-only is unavailable.
- **Use `--sandbox` as read-only.** Deferred. It is unverified and blocked on
  this host.
- **Pass `--mode accept-edits` or `--mode auto` for writes.** Rejected. The
  default mode with skip-permissions was verified to create files. Do not pass a
  mode speculatively.
- **Read usage from the SQLite store.** Rejected. The payload tables are opaque
  protobuf blobs. Use the event stream.
- **Add AGY to the orchestration roster now.** Rejected. The roster authorizes
  Muse and Pi only, and AGY has no reviewer history.

## Key Decisions & Staging for Next Steps

**Decisions carried into `/egm-draft`**

1. Stored model id is the exact suffixed id (`gemini-3.8-flash-high`,
   `-medium`, or `-low`). Effort is derived from the suffix. `--effort` is never
   emitted. The base alias is not accepted. A mismatched explicit pair fails at
   validation. Changing effort means selecting another suffixed model. Fallback
   re-derives effort from the new model id through a small engine hook.
2. Worktree delivery becomes an engine capability, not a fifth literal. All four
   cwd sites use it.
3. Fresh turn omits `--conversation`. Later turns pass the bound id. A returned
   id that differs from the requested id fails the turn.
4. `--continue`, `--session-id`, and `--no-session-log` are refused. AGY session
   files are documented as living outside Claive.
5. Output is `stream-json` only. A terminal event comes only from `result`.
   `SUCCESS` completes. `ERROR`, unknown status, or a `finished_reason` fails.
6. Permissions are skipped by default, with an environment opt-out. Claive owns
   the turn timeout. AGY's `--print-timeout` is not passed.
7. AGY has no verified enforced read-only mode. Read-only requests use soft
   read and report the lack of enforcement. `--mode plan` is not a substitute.
   Workspace and concurrency decisions distinguish soft from enforced read.
8. Prompt text goes in argv with a length guard. Stdin stays unverified.
9. Unsupported flags are rejected by `validate_turn`, not ignored.
10. Usage sums step deltas and dedupes by conversation and step.
11. Doctor and `claive engines` iterate the registry. `OWNED_FLAGS` gets a full
    AGY entry.
12. AGY is explicit-engine only. No orchestrator auto-selection.

**Staging**

0. **Probes, no code.** Capture a tool-using `stream-json` turn for stdout
   purity and exit-after-result. Record the status and `finished_reason`
   vocabulary for success, model error, quota, auth failure, and timeout.
   Confirm no genuine read-only mode exists: test `--sandbox` as a write
   barrier, and test plan mode only to document that it is planning, not a
   read-only turn. Check whether a conversation is cwd-scoped. Test two
   concurrent conversations. Measure argv limits with a 200 KB prompt.
1. **Core decoupling.** Add the worktree capability, replace the four literals,
   iterate the registry in doctor and `claive engines`, extend `OWNED_FLAGS`, and
   add tests that fail when any registered engine is missing from these surfaces.
2. **Adapter.** New `bin/claivelib/engines/agy.py` with validation, command
   construction, event normalization, and usage. Add stream-json fixtures and a
   hermetic fake `agy`.
3. **Surfaces.** Doctor row, engines row, docs, policy line, skill name lists,
   and a live opt-in test. Update `tests/hermetic.py:18-20` to scrub `AGY_*`.
4. **Draft.** Run `/egm-draft` with this brief. The design document should carry
   the event mapping table, the exact argv per turn type, and the failure
   taxonomy.

**Acceptance test for the first slice**

```
claive run --engine agy --worktree \
  --check "test -f hello.txt" \
  "Create hello.txt with the word hello."
```

The file exists in the worktree, the parent repo is unchanged, the check passes,
the job completes with usage, and a follow-up turn proves context reuse. A
`--read-only` launch fails before a job is created with the stated error.

**Open questions**

- Is AGY's conversation store cwd-scoped? This decides whether cross-workspace
  resume must be refused, as Pi does.
- Is `--sandbox` a real write barrier, and does it compose with permission
  skipping? If yes, read-only support becomes possible later.
- What is the full `result.status` vocabulary, and does 1.4.12 emit
  `finished_reason` for interrupted and timed-out turns?
- Should `claive close` purge the AGY conversation, and who owns that policy?
- Do user MCP servers and skills load in print mode, and how many tokens do they
  add to the 6.8k baseline?
