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
   `claive inbox --consumer <your-harness-name> --json`. This returns every turn
   and batch event since that consumer's last read. Use one consumer name per
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
lane run in order. `context: "previous"` passes the prior stage's result
forward. A stage that does not finish `done` skips the rest of its lane. Use
`claive batch validate` before `start`. After `batch wait`, read
`claive batch status ID` and `claive show <stage worker>`. To rerun only the
stages that did not finish, use `claive batch retry ID`.

## Safety

- Workers get `CLAIVE_WORKER_ID` and cannot launch workers or batches (this
  prevents recursive orchestration when a worker reads claive's own skills).
  `CLAIVE_ALLOW_NESTED=1` overrides this; use it only on purpose.
- `claive doctor` checks binaries, the state directory, the model gateway and
  quota without launching a model.
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
