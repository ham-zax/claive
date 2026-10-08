# Driving claive from any harness

claive is a plain CLI with file-based state, so any parent can drive it: Claude
Code, Codex, a Pi agent, another agent harness, or a shell script. Nothing below
depends on one host's features. If your host can run a command and read its
output and exit code, it can orchestrate workers.

## Contract

- The first output line of every launch is `Worker <12-hex id> | <label> | <path>`
  (`Batch <id> | ...` for batches, `Mission <id> | ...` for missions). Add
  `--json` to `list`, `show`, `inbox`, `batch ...`, `mission ...` for
  machine-readable output.
- Exit codes, everywhere:

  | code | meaning |
  |---|---|
  | 0 | completed |
  | 1 | failed or usage error (`failure_kind` says why: quota, protocol, worker, launch, supervisor, rejected, interrupted) |
  | 3 | a worker needs a parent decision (`claive answer ID --message ...`) |
  | 124 | `--timeout` expired; the work is still running |
  | 130 | cancelled |

- All state is under `$CLAIVE_DIR` (default `~/.local/state/claive`). A new
  process, or a different harness, sees the same workers, batches, missions and
  inbox.
- Paths passed to claive must be absolute.

## Launch without blocking

| want | command |
|---|---|
| one turn, background | `claive start --role scout --workspace W --prompt-file P` |
| reusable session, background | `claive open --detach --role worker --workspace W --prompt-file P` |
| several workers with stages | `claive batch start PLAN.json` |
| block until done | `claive run ...` (exit code is the outcome) |

Every launch returns at once except `run` and a foreground `open`. No `&`,
`nohup`, host background jobs or shell sessions are needed.

## Learn when work finishes

Pick whichever your host supports; all three read the same events.

1. **Bounded wait** (works everywhere):
   `claive wait ID1 ID2 ... --any --timeout 300`. This returns the first worker that
   settles, printing its report and exiting with its code. Exit 124 means nothing
   finished yet. Do other work, then wait again. Keep the timeout below your host's
   command time limit.
2. **Inbox** (poll at natural pauses, or after a context reset):
   `claive inbox --consumer <your-harness-name> --json`. This returns every turn,
   batch and goal event since that consumer's last read. Use one consumer name per
   parent (for example `claude`, `codex`, `pi-main`) so parents do not steal each
   other's events. `--peek` reads without advancing.
3. **Push hook** (optional): set `CLAIVE_NOTIFY_CMD` before launching. It runs
   via `/bin/sh -c` for each event, with the event JSON on stdin and
   `CLAIVE_EVENT_ID`, `CLAIVE_EVENT_STATUS` and `CLAIVE_EVENT_CODE` in its
   environment. Use it for a desktop notification, a file your host watches, a
   chat webhook, and so on. Its failures are ignored.

## Answer questions

A worker started with `--report` or `--role` may stop with `needs_decision` or
`blocked`. When that happens, `wait` and `run` exit with 3, and the inbox badge is
`ASK` with the question. For a reusable worker, reply with
`claive answer ID --message "..."`, then wait again. A single-turn worker cannot
be answered; start a new one with the decision included.

## Return to a named specialist

After opening a reusable worker, bind it with `claive alias set backend ID`.
Any parent using the same `$CLAIVE_DIR` can discover it with
`claive alias list --json`, inspect it with `claive show @backend --json`, and
queue related work with `claive followup @backend --prompt-file /abs/task.md`.
Worker commands accept `@name` in place of an ID, including `answer` and
`wait --any`. Names do not start workers or restore closed sessions.

An operation resolves the address once; its output and persisted mission links
use the worker ID, so rebinding a name cannot move an existing assignment.
To move an address to a different worker, use
`claive alias set backend NEW_ID --replace`. `claive alias remove backend`
removes only the address. Always reuse the specialist's workspace and policy
when reopening its retained session.

## Coordinate a bounded conversation

Workers can talk to each other through the parent: a conversation forwards each
participant's reply to the next, so tell the user and other agents this is available
whenever two or more perspectives should react to each other.

Open workers with individual role prompts, engines/models, and `--report`, wait
for their initial turns to settle, and bind names. Then run:

```bash
claive conversation start @pi-builder @pi-critic @claude-reviewer \
  --message-file /abs/topic.md --rounds 2 --timeout 300 --json
```

The controller forwards replies in order, preserving each participant's session
and launch policy. With three workers and two rounds it queues at most six
follow-ups. Each reply has an exact request receipt; it never reads another
turn's mutable `result.txt` as the reply. Required-report errors and parent
questions stop the flow. Peer content is framed as evidence rather than
authority to alter the assignment. Workers cannot start conversations under
the normal recursion guard. A worker can be in one running conversation at a time, and
`CLAIVE_MISSION` links the conversation to the mission.

Use a managed shell with a short yield, retain its session ID, and collect its
final exit code. On code 3, answer the worker named in `needs_parent`. On code
124/130, inspect `pending_request.response_file` before retrying: an in-flight
turn may still finish, while a queued request expires. The controller stops
scheduling without cancelling unrelated worker activity. Inspect with
`claive conversation show ID --json`; participants' ordinary `show`, `wait`,
`answer`, `close`, and `cancel` commands continue to work.

## Run goals without a parent (`claive goal`, `claive serve`)

On a server with no parent session, queue goals with
`claive goal add --title T --prompt-file F --workspace W [--write] [--budget 2h]`
and let `claive serve` (a foreground loop, run under the user unit
`systemd/claive-serve.service`) launch each one as a reusable `--report`
worker on any engine. A question parks the goal and posts a `goal` event with
badge `ASK` to the inbox; reply with `claive goal answer ID --message "..."`
(not `claive answer`, so the goal record stays in step). Goals have a wall-clock
budget, retries with backoff, and a global backoff on quota, protocol and launch
failures. `claive serve --stop` cancels its workers and pauses the queue until
`--resume`; `--status` shows the state. Logs are in
`$CLAIVE_DIR/supervisor/serve.log`. See the README for details.

## Survive context loss with missions

```bash
M=$(claive mission new --title "Parser rewrite" --goal-file /abs/goal.md | sed -n 's/^Mission \([0-9a-f]*\).*/\1/p')
export CLAIVE_MISSION=$M          # every launch below links itself
claive mission note $M "Chose streaming design; tests in tests/test_parser.py"
```

After compaction, a restart or a switch to another harness, run
`claive mission list` and then `claive mission show ID`. The `Next:` line says
what to do: answer a worker, inspect a failure, wait for running work, or review
and close. Write decisions down with `mission note`, because the parent's
memory is the only thing missions cannot recover.

## Batches

A plan is a JSON file of lanes. Lanes run in parallel and the stages inside a
lane run in order. Every lane and every stage needs a `key` (1-32 lowercase
letters, digits, `_` or `-`); stages are reported as `lane.stage`. `defaults`
applies stage options to every stage, and each stage needs an absolute
`prompt_file`. `context: "previous"` passes the prior stage's result
forward.

```json
{"label": "review", "workspace": "/abs/repo",
 "defaults": {"engine": "pi", "model": "mimo-v2.6-flash-free", "read_only": true},
 "lanes": [
  {"key": "a", "stages": [{"key": "scan", "prompt_file": "/abs/scan.md", "report": true},
                         {"key": "summary", "prompt_file": "/abs/sum.md", "context": "previous"}]},
  {"key": "b", "stages": [{"key": "scan", "prompt_file": "/abs/scan2.md"}]}]}
```

A stage `model` applies to that stage only and does not change Pi's shared
default model; an explicit `--model` on `run`/`start`/`open` does. A stage that does not finish `done` skips the rest of its lane. Use
`claive batch validate` before `start`. After `batch wait`, read
`claive batch status ID` and `claive show <stage worker>`. To rerun only the
stages that did not finish, use `claive batch retry ID`. A batch whose
runner process died is reported as `failed`; retry keeps any stage whose
worker is still running and waits for it.

## Safety

- Workers get `CLAIVE_WORKER_ID` and cannot launch workers or batches (this
  prevents recursive orchestration when a worker reads claive's own skills).
  `CLAIVE_ALLOW_NESTED=1` overrides this; use it only on purpose.
- `claive doctor` checks binaries (it runs `pi --version`), the state directory,
  the config, that Pi's `models.json` lists every model claive may request
  (`pi_provider`), the provider's `GET /models` latency, and quota, without
  launching a model. `--live` adds one tiny read-only Pi turn on Pi's shared
  default model. Exit 0 means every required check passed; it reports a loopback provider override when one is set.
- With a `workspaces` allowlist in the claive config, workers run only in listed
  directories, read-only unless the deepest matching entry has `write: true`.
- A worker's report is evidence, not proof. Verify with your own tests, or use
  `claive-orch` for the verified ladder.

## Host notes

- **Claude Code**: launch with `start`, `open --detach` or `batch start` (plain
  Bash). Wait with `claive wait ... --any --timeout 540` in the foreground, or
  without a timeout using `run_in_background: true` to get a completion
  notification. Read `inbox --consumer claude` after compaction.
- **Codex**: plain `exec_command` calls. Use `wait --any --timeout` below the
  tool's time limit instead of keeping shell sessions open, and run
  `inbox --consumer codex` at the start of each turn.
- **Pi agent**: the same commands through its bash tool, with
  `--consumer pi-<name>`. Install the skills with `install.sh --pi`. Pi workers
  launched by claive cannot recurse; a Pi *parent* must not have
  `CLAIVE_WORKER_ID` set.
- **Shell or CI**: `claive batch start plan.json && claive batch wait ID`, and
  branch on the exit code.
