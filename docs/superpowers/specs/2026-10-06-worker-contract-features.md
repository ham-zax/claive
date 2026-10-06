# Worker contract features: doctor, reports, roles, parent questions, failure kinds

Adds five features to `claive` (bin/claivelib/cli.py plus one new module
bin/claivelib/roles.py). Inspired by pi-subagents; adapted to claive's
harness-neutral, file-based design. No new dependencies (stdlib only).

## 1. `claive doctor [--json]`

Read-only health check. Never launches a model, never prints secret values
(API keys, auth headers). Each check is `{name, ok, required, detail}`.
Exit 0 when every `required` check is ok, otherwise 1. Text output: one line per
check, `ok`, `warn` (not ok, not required) or `FAIL` (not ok, required), then the
name and detail. `--json` prints `{"ok": bool, "checks": [...]}`.

Checks, in this order and with exactly these names:

| name | required | ok when |
|---|---|---|
| `python` | yes | Python >= 3.10 |
| `state_dir` | yes | `root()` exists or can be created, and a temp file can be written and removed in it |
| `muse` | no | Muse binary (`MUSE_WORKER_BINARY` or `~/.local/bin/muse`) is an executable file |
| `pi` | no | Pi binary (`PI_WORKER_BINARY` or `~/.local/bin/pi`) is an executable file |
| `engines` | yes | at least one of `muse`, `pi` is ok |
| `opencode2api` | no | `$PI_CODING_AGENT_DIR/models.json` (default `~/.pi/agent/models.json`) has `providers.opencode2api.baseUrl`, and GET `<baseUrl>/models` (3 s timeout, `Authorization: Bearer <apiKey>` if an apiKey is configured) returns JSON. Detail: model count plus any of the preferred models (`mimo-v2.6-flash-free`, `big-pickle`, `space-bunny-free`, `muse-spark-1.3-contributor-free`) missing from the list. Missing file/provider: not ok, detail says so |
| `git` | no | `git` on PATH |
| `quota` | no | no worker record (any status) has `quota_exhausted` with a `quota_reset_at` ISO time still in the future. Detail when not ok: engine, reset time, and "pre-approved fallback: Pi muse-spark-1.3-contributor-free" |
| `interrupted` | no | no worker record has status `interrupted`; detail lists their IDs |

Resolve binaries through the engine's own defaults (do not duplicate paths if the
engine exposes them; a small helper per engine is fine).

## 3. Structured worker report

Opt-in per worker: `--report` on `run`/`start`/`open` (implied by `--role`).
Stored as `state["report_contract"] = True` and inherited by every later turn
(follow-ups and answers).

When enabled, claive composes the prompt the engine receives. For each turn it
writes `<job dir>/prompt-<turn:04>.md` containing, separated by blank lines:
the role preamble (first turn only, if a role is set), the caller's prompt text,
then the contract below. The engine is given that composed file;
`state["prompt_file"]` is the composed path and `state["source_prompt_file"]`
the caller's file. Without the contract, behaviour and prompt paths are unchanged.

Contract text (exact wording may vary, but it must contain the literal fence tag
`claive-report` and the key names):

    End your final answer with exactly one fenced block tagged claive-report
    containing one JSON object:
    {"status": "done" | "blocked" | "needs_decision",
     "summary": "<one or two sentences>",
     "changed_files": ["path", ...],
     "commands_run": [{"command": "...", "exit_code": 0}],
     "residual_risks": ["..."],
     "question": "<required when status is blocked or needs_decision>"}
    Use needs_decision instead of guessing when a choice would change design,
    public behaviour, data formats, scope, or acceptance criteria.

Parsing, after each turn that requested the contract: take the **last**
```` ```claive-report ```` fenced block in the turn's final answer text.
- None found: `state["report_state"] = "missing"`.
- Not a JSON object, `status` not one of the three values, `summary` not a
  non-empty string, list fields present but not lists of the right type, or
  `question` missing/empty when status is not `done`:
  `report_state = "invalid"`, `state["report_error"]` = short reason.
- Otherwise `report_state = "ok"`, `state["report"]` = the parsed object
  (keys normalised: missing list fields become `[]`, missing question `null`).
The full answer is still written to `result.txt` unchanged.
A missing or invalid report does not change the turn status, but `report()`
(used by show/wait/run) prints a warning line naming the report state.
When the report is ok, `report()` prints the summary, changed files, commands
with exit codes, risks, and the question if any.

`report`, `report_state`, `report_error`, `needs_parent` are cleared at the start
of every turn.

## 5. Roles

`--role scout|worker|reviewer|oracle` on `run`/`start`/`open`. Defined in
`bin/claivelib/roles.py` as data:

| role | engine | model | effort | read-only | step cap |
|---|---|---|---|---|---|
| scout | pi | mimo-v2.6-flash-free | max | yes | none |
| worker | muse | muse-spark-1.3-contributor | xhigh | no | 100 |
| reviewer | pi | big-pickle | max | yes | none |
| oracle | pi | space-bunny-free | max | yes | none |

Each role has a short preamble (scout: map relevant code, entry points, data flow,
file:line, risks, no edits; worker: implement only the assigned files, no commit,
no staging, no delegation, run the named checks, escalate with needs_decision
rather than guess; reviewer: no edits, concrete defects with file:line and
evidence, say plainly if none; oracle: no edits, challenge assumptions, risks,
alternatives, what evidence would change the view).

Explicit flags win over role defaults: `--engine`, `--model`, `--reasoning-effort`,
`--max-model-steps`. `--read-only` can only add read-only; a read-only role stays
read-only. A role always enables the report contract. The step cap applies only
when the resolved engine supports it (Pi rejects step caps: do not pass the role's
cap to an engine whose `default_max_model_steps` is None unless the user gave
`--max-model-steps` explicitly). `state["role"]` records the role.
`--engine`'s argparse default must become None so "not given" is detectable;
resolve to the role engine, else `DEFAULT_ENGINE`.

Model policy, for every launch with or without a role: refuse any model whose
lowercase name contains `nemotron` or starts with `ling-3.1-flash`, with
`ValueError("model <m> is disallowed by claive policy")`, before creating the job.

## 7. Worker asks the parent

After a turn whose report is ok with status `needs_decision` or `blocked`, set
`state["needs_parent"] = {"kind": <status>, "question": <question>}`.
- `outcome_code` returns **3** when the (effective) status is `completed` and
  `needs_parent` is set. So `claive run` and `claive wait` exit 3.
- `report()` prints `Needs parent (<kind>): <question>` and
  `Answer: claive answer <id> --message ...` (reusable) or, for single-turn
  workers, a note that answering needs a reusable worker (`open --session-id`).
- `render`/`compact_view` show badge `ASK` for such workers; `summary()`
  (status-line) adds `N need you` when N > 0.

New command `claive answer ID (--message TEXT | --message-file ABS_PATH)`:
- Error unless the worker is reusable, active, not closing, and `needs_parent` is set
  (`worker is not waiting for a parent decision`).
- Writes `<job dir>/answer-<ns>.md`: the question, the parent's answer, and
  "Continue the assignment with this decision.", then queues it exactly like a
  follow-up (same request file format), so the contract is appended by the same
  composition path as follow-ups.
- Prints `Answer queued for <id>`.

## 8. Failure kinds

Every failed turn sets `state["failure_kind"]`; cleared at the start of each turn.

| kind | when |
|---|---|
| `quota` | `quota_exhausted` was set during the turn |
| `protocol` | otherwise, malformed events > 0 or no terminal event |
| `worker` | otherwise, terminal `failed`, or non-zero exit with terminal `completed` |
| `launch` | the engine process could not be started (exception before or at `Popen`) |
| `supervisor` | any other exception inside `supervise` after the child started |
| `rejected` | a queued follow-up/answer was rejected (missing prompt, validation error, unknown worktree); set together with `last_turn_status = "failed"` |
| `interrupted` | `load()` marks a record `interrupted` |

`report()` prints `Failure kind: <kind>` when set. `list --json` and `show --json`
include it (they already dump state).

## Non-goals

No changes to `claive-orch` behaviour (its prompts do not use `--report`/`--role`).
No model is ever launched by doctor. No automatic engine fallback.
