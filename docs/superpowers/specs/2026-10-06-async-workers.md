# Async workers: detach, inbox, wait-any, batches, missions

Goal: let any parent harness (Claude Code, Codex, a Pi agent, a plain shell
script) run several claive workers in the background, learn when they finish,
run simple multi-worker plans, and recover its place after losing context.
Inspired by pi-subagents' background runs, `bg_wait`, scripted lanes and
missions; adapted to claive's file-based, CLI-only design.

**Harness neutrality is the main design rule.** Nothing may rely on one host's
features (Claude Code background notifications, Codex shell sessions, Pi
extension APIs). Every capability is a CLI command with plain text output, a
`--json` form, and documented exit codes. Every launch command returns quickly
unless the caller asked to block. All state lives under `root()`.

Exit codes used by the new commands (same meaning everywhere):
`0` success, `1` failed / error, `3` a worker needs a parent decision,
`124` timed out (the work is still running), `130` cancelled.

New code goes in new modules where practical (`bin/claivelib/inbox.py`,
`bin/claivelib/batch.py`, `bin/claivelib/mission.py`); `cli.py` keeps the parser
and dispatch. Stdlib only.

## A. Detached reusable workers

`claive open ... --detach` creates the same reusable worker as `open`, but
runs its loop (`reusable_worker`) in a detached background process (like
`start` does for `supervise`) and returns at once with exit 0. Output: the usual
`Worker <id> | <label> | <path>` first line, then hint lines for `wait`,
`followup`, `answer`, `close`. State is identical to a foreground `open`
(`reusable: true`). Without `--detach`, `open` is unchanged.

## B. Recursion guard

Every worker child process (the engine command started by `supervise`) gets
`CLAIVE_WORKER_ID=<job id>` in its environment. `run`, `start`, `open` and
`batch start` refuse when `CLAIVE_WORKER_ID` is set in the caller's environment,
unless `CLAIVE_ALLOW_NESTED=1`, with
`ValueError("claive workers may not launch workers (CLAIVE_WORKER_ID is set)")`.
Reason: Pi workers read `~/.pi/agent` skills, which may include claive's own
skills; a worker must not start an orchestration of its own.
The batch runner (section E) is not a worker and must not set the variable.

## C. Completion inbox

At the end of every worker turn (completed, failed or cancelled; including a
rejected queued follow-up/answer), append one JSON line to `root()/inbox.jsonl`:

    {"type": "turn", "seq": <time.time_ns()>, "at": "<ISO-8601 UTC>",
     "id": "<job>", "label": "...", "turn": <n>, "status": "completed|failed|cancelled",
     "code": <outcome_code for that turn>, "failure_kind": <str|null>,
     "needs_parent": <{"kind","question"}|null>, "report_status": <report status|null>,
     "batch": <batch id|null>, "mission": <mission id|null>}

`code` uses `outcome_code` semantics (0, 3, 130, exit code, or 1). A turn event
is written after the turn's final `state.json` is saved. Write each line with a
single `os.write` on an `O_APPEND` descriptor so concurrent writers never
interleave. A batch that finishes appends
`{"type": "batch", "seq", "at", "id": <batch id>, "label", "status", "code", "mission"}`.

`claive inbox [--consumer NAME] [--peek] [--json]`:
- Shows events appended since this consumer's cursor, oldest first, then
  advances the cursor (unless `--peek`). Cursors are byte offsets stored in
  `root()/inbox-cursors/<NAME>`. Default consumer: `default`. NAME must match
  `[A-Za-z0-9_.-]{1,64}`, else ValueError. Several harnesses can each read
  every event by using different consumer names.
- Text: `No new events` or one line per event:
  `<id> <BADGE> code=<n> <label>`, BADGE is `ASK` when needs_parent is set,
  otherwise the upper-case status; then ` kind=<failure_kind>` when set and
  ` question: <question>` for ASK. Batch events start with `batch <id>`.
- `--json`: a JSON list of the events. Exit 0 in both forms.

`CLAIVE_NOTIFY_CMD` (optional hook for any harness): if set in the supervisor's
environment when an event is written, run it with `/bin/sh -c`, the event JSON on
stdin, and `CLAIVE_EVENT_ID` / `CLAIVE_EVENT_STATUS` / `CLAIVE_EVENT_CODE` in its
environment. Start it detached (new session, stdout/stderr to DEVNULL), never
wait for it, and ignore every error from it. It must never fail a turn.

## D. wait on several workers, with a timeout

`claive wait ID [ID ...] [--any] [--timeout SECONDS]`:
- Several IDs require `--any` (else ValueError
  `use --any to wait for several workers`). Unknown IDs fail before waiting.
- Returns as soon as one of the IDs is settled (the existing single-ID
  predicate: not active, or idle with no queued requests). If several are
  already settled, the first one in argument order wins. Prints the existing
  `report()` for it and exits with its `outcome_code` (0/3/130/other).
- `--timeout` (also valid with one ID): when it expires first, print
  `Timed out after <N>s; still running: <id> <id>` and exit 124.
  `--timeout` must be > 0.
- One ID without `--timeout` behaves exactly as today.

## E. Batches

A batch runs a JSON plan of **lanes**. Lanes run in parallel; the **stages**
inside a lane run in order. Every stage is a single-turn worker (like `start`).

Plan file (absolute path):

    {"label": "review-x",              // optional
     "workspace": "/abs/dir",          // default workspace for every stage
     "mission": "<mission id>",        // optional, see F
     "defaults": {<stage options>},    // optional, applied to every stage
     "lanes": [
       {"key": "correctness",
        "stages": [
          {"key": "review", "prompt_file": "/abs/p.md", "role": "reviewer"},
          {"key": "fix", "prompt_file": "/abs/f.md", "role": "worker", "context": "previous"}
        ]}]}

Stage options (in `defaults` or a stage): `prompt_file` (required per stage after
defaults), `workspace`, `label`, `role`, `engine`, `model`, `provider`,
`reasoning_effort`, `max_model_steps`, `read_only`, `report`, `context`
(`"previous"` only). They mean exactly what the `start` flags mean, with the
same role defaults, model policy and validation.

Validation, all before anything is created or launched (ValueError with a clear
message): valid JSON object; 1..16 lanes; 1..8 stages per lane; lane keys unique;
stage keys unique within a lane; every key matches `[a-z0-9][a-z0-9_-]{0,31}`;
unknown option keys rejected; prompt files absolute, existing, non-empty;
workspace absolute directory; roles known; disallowed models refused;
`context: "previous"` not allowed on a lane's first stage.

`context: "previous"`: the stage's prompt becomes
`Context from previous stage <lane>.<stage>:\n<that stage's result.txt>\n\n<the stage prompt>`,
written to the batch dir as `prompts/<lane>.<stage>.md` and used as the prompt file.

Stage outcome (from the stage worker's `outcome_code`): `0` → `done`;
`3` → `needs_parent`; anything else → `failed` (`130` → `cancelled`).
Any non-`done` outcome blocks its lane: later stages become `skipped`. Other
lanes continue. Stage statuses: `pending`, `running`, `done`, `failed`,
`needs_parent`, `cancelled`, `skipped`.

Batch status while the runner works: `running`. Final: `cancelled` if it was
cancelled, else `failed` if any stage failed, else `needs_parent` if any stage
needs the parent, else `completed`. Batch code: completed 0, needs_parent 3,
failed 1, cancelled 130.

Storage: `root()/batches/<12-hex id>/` with `plan.json` (the validated plan as
given), `state.json`, `prompts/`. A batch state records label, workspace,
mission, status, code, started_at, ended_at, and for each lane its stages:
`{key, status, worker (job id or null), code}`. Each stage worker's state gets
`batch: <id>` and `batch_stage: "<lane>.<stage>"`, and `mission` if set.
`claive list` keeps listing only workers (batch dirs are not workers).

The runner is a detached background process (`_batch <id>`, started through
`launcher_path()` so test-registered engines work). It saves `state.json`
after every change.

Commands:
- `claive batch validate PLAN` → `Plan ok: <L> lanes, <S> stages`, exit 0.
- `claive batch start PLAN [--json]` → validate, create, start the runner,
  print `Batch <id> | <label> | <path>` (or JSON `{"id", "path"}`), exit 0.
- `claive batch status ID [--json]` → batch status and one line per stage:
  `<lane>.<stage> <STATUS> <worker id or -> [code=<n>]`. JSON: the state.
- `claive batch wait ID [--timeout S]` → block until the batch is not
  `running`, print status, exit with the batch code; on timeout print
  `Timed out after <N>s; batch <id> still running` and exit 124.
- `claive batch cancel ID` → cancel running stage workers, mark pending stages
  `skipped`, batch `cancelled`. Prints `Batch <id> cancelled`.
- `claive batch retry ID` → only when the batch is not running (else
  ValueError `batch is still running`). Resets every stage that is not `done`
  to `pending` (done stages and their results are kept and not re-run),
  sets the batch to `running`, restarts the runner, prints
  `Batch <id> retrying <N> stages`. A retried `context: "previous"` stage uses
  the kept result of its done predecessor; prompt files are re-read at retry.
- `claive batch list [--json]` → newest first, one line per batch.

## F. Missions

A mission is a durable record of a goal and the work linked to it, so a parent
that lost its context (compaction, restart, a different harness) can resume.
Stored in `root()/missions/<12-hex id>/mission.json`:
`{id, title, goal, status: "open"|"closed", created_at, notes: [{at, text}],
  links: [{kind: "worker"|"batch"|"run", id, at}]}`.

- `claive mission new --title T (--goal TEXT | --goal-file ABS)` →
  `Mission <id> | <title>`.
- `claive mission note ID TEXT` → append a note (decisions, next steps).
- `claive mission link ID (--worker W | --batch B | --run R)` → append a link
  (duplicates ignored). `--run` is a claive-orch run id (not validated beyond
  being non-empty; its status shows as `see claive-orch report <id>`).
- `--mission ID` on `run`/`start`/`open`, and `"mission"` in a batch plan, link
  the new worker/batch automatically and record `mission` in its state. If the
  flag is absent, `CLAIVE_MISSION` from the environment is used. An unknown or
  closed mission is a ValueError before anything is created. Batch stage
  workers are not linked individually; the batch is.
- `claive mission show ID [--json]` → title, status, goal, last 10 notes
  (JSON: all), every link with live status (worker: status/ASK and code;
  batch: batch status), and a `next` line computed in this order:
  1. a linked worker or batch stage worker that needs the parent →
     `answer <worker>: <question>`
  2. a linked worker or batch that failed → `inspect failed <id>`
  3. anything still running → `wait for <ids>` (space separated)
  4. otherwise → `review results, then close the mission`.
  Text output prints `Next: <next>`; JSON has key `next`.
- `claive mission list [--all] [--json]` → open missions (all with `--all`).
- `claive mission close ID` → status closed; prints `Mission <id> closed`.

## Non-goals

No mid-turn steering (not proven possible with Muse/Pi print modes). No
scheduler. No change to claive-orch behaviour. No automatic engine fallback.
No concurrency cap.
