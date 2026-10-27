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
- Common exit codes (see [command reference](command-reference.md) for exceptions):

  | code | meaning |
  |---|---|
  | 0 | completed |
  | 1 | failed or refused (`failure_kind` says why: quota, protocol, worker, launch, supervisor, rejected, interrupted) |
  | 2 | argument parsing error, failed verification, or runtime drift/smoke failure |
  | 3 | a worker needs a parent decision (`claive answer ID --message ...`) |
  | 124 | wait deadline expired, or ask/conversation deadline expired |
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
   command time limit. Add `--inbox CONSUMER` to also return on new inbox events for that
   consumer (advancing its cursor, exit 3 if an event needs the parent), and `--json` for
   one object with the event that ended the wait.
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

Pass `--alias backend` or `--label backend` at launch to bind a reusable worker.
If the launch left it unnamed, use `claive alias set backend ID`.
Any parent using the same `$CLAIVE_DIR` can discover it with
`claive alias list --json`, inspect it with `claive show @backend --json`, and
queue related work with `claive followup @backend --prompt-file /abs/task.md`.
To add context without a new task, use `claive send @backend --message '...'`
(`--mode note|wake|now`; receipts in the worker's `messages/receipts/`).
To question it, `claive ask @backend --message '...' --timeout 120` (exit 0 answered,
124 withdrawn/expired, 3 needs the parent, 1 refused or failed turn); the worker answers with `claive reply
MSG_ID --message '...'` or in its final text. `claive peek @backend [--steps N]
[--start N]` reads its numbered recent steps without touching it (parent only).
Set `CLAIVE_PARENT=<harness>` to name the sender in delivered messages.
Worker commands accept `@name` or a unique 4..11 character ID prefix in place of
an ID, including `answer` and `wait --any`. Names do not start workers or restore closed sessions.

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

## Engine capabilities and configuration

Workers use the engine's default tools. Claive passes no restricted tool list.

| Engine | Read-only mechanism | Web | Steering | Fork | Step cap |
|---|---|---|---|---|---|
| Claude | `--tools default --permission-mode plan` | Always on | Stream JSON | Yes | Refused |
| Codex | Read-only sandbox | Live search every turn | App Server | Yes | Refused |
| Muse | `--permission-profile :read-only --disable-write`, shell on | Always on | Refused | Refused | Opt-in |
| Pi | Full tools and a read-only instruction | `--web` refused | RPC | Yes | Refused |

Steering needs `--enable-steering` at launch. Fork needs an idle reusable session.
`--web` is accepted and has no effect on Claude, Codex and Muse.

Claude write turns use `--permission-mode bypassPermissions`.
`--setting-sources ""` keeps the parent's hooks and permission rules out.
MCP servers and skills load. Claive does not pass `--strict-mcp-config` or
`--disable-slash-commands`. WebSearch and WebFetch remain available.
Claude workers load CLAUDE.md files through `--add-dir` and
`CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD=1`. The order is the worker file
`claude/CLAUDE.md` next to `config.json` (default `~/.config/claive/claude/CLAUDE.md`, only when
it exists), then each directory from the git root down to the workspace. A `.git` file (a
worktree) also marks the git root. Outside a repository, only the workspace loads. The walk stops
below the home directory, so the parent's `~/.claude/CLAUDE.md` does not load. Each added
directory loads `CLAUDE.md`, `.claude/CLAUDE.md` and `.claude/rules/*.md`.
Live check, 2026-10-15: `CLAUDE.local.md` and `@import` lines do not load.
Codex workers read `~/.codex/AGENTS.md` and the repository `AGENTS.md` natively.
Live check, 2026-10-15: `ExitPlanMode` is unavailable in `--print` mode.
The model refused writes itself. A harness-level write block remains unproven.

Codex write turns have full access by default. `CLAIVE_CODEX_YOLO=0` selects
workspace-write. Read-only exec turns set `sandbox_mode="read-only"` through `-c`.
The App Server path links `auth.json`, sessions and skills into a private
`CODEX_HOME` and reads the repo's `AGENTS.md`. Skills load by default.
The user's `config.toml`, including MCP configuration, is opt-in:
`engines.codex.user_config: true` or `CLAIVE_CODEX_USE_USER_CONFIG=1`.
Exec turns pass `--ignore-rules`. The App Server wrapper does not pass that flag.
Every turn sets `web_search="live"`. Config opt-in installs no tools.

Muse read-only turns omit `--disable-approval`, which conflicts with a permission
profile. Write turns pass it. Neither mode has a default model-step cap.
Only a flag or role/config value adds `--max-model-steps`.

Pi uses `--tools '*'` in both modes. It has no CLI read-only mode.
The instruction enters through `--append-system-prompt` and after the task text.
This requests model compliance and does not enforce write protection.
Pi passes `--offline --no-extensions --no-prompt-templates --no-themes --no-approve`.
Skills, MCP and context files load. See [Pi engine](pi-engine.md).

Claive reloads config when commands need policy. `engines.<name>` accepts
`enabled`, `reason`, `default_model`, `default_reasoning_effort`,
`disabled_models`, `disabled_reasoning_efforts`, `models`, and `extra_args`.
`user_config` applies only to Codex. `budget` applies only to Claude.
`enabled: false` refuses the whole engine.
Defaults and allowlists cannot expand the engine's supported models or efforts.

`extra_args` is an opt-in list of nonempty strings. Claive inserts them before
`--`, or at the end. It refuses `--` and the flags in `OWNED_FLAGS` in
`bin/claivelib/config.py`, including `--flag=value`.
Codex steering and fork turns refuse nonempty `engines.codex.extra_args`.

Custom `roles.<name>` need `engine`, `read_only`, and `preamble` or an absolute
`prompt_file`. Built-in overrides inherit omitted values. Explicit flags win.
All built-in roles default to `max_model_steps: null`.
A built-in role cap applies only when its selected engine supports caps.
Unsupported explicit or custom-role caps are refused.
Teams map members to roles and add collaboration/project files.
Use `--team NAME --member NAME`, without `--role`.
`--project-file` replaces the team's project file for that launch.
`claive team list|show` reads team configuration and member aliases.

First-prompt order is role, team header, collaboration, project, context files,
task, report contract. `--context-file` repeats up to ten times, in order.
Layer files must be absolute, nonempty UTF-8 text, and at most 256 KiB.
Duplicate context files are refused. Follow-ups keep the retained session.

### Claude context budget

`engines.claude.budget` is on by default. Built-in limits: Haiku warns at 90k and stops at 100k
tokens. Other models warn at 140k and stop at 150k. Both allow 7 automatic
continuations per turn. Config entries merge over these per model and per key. `budget: false` turns it off.
At the warn limit claive asks the worker to continue toward a checkpoint before the stop limit.
The worker must not start a sub-task that cannot finish before that checkpoint. At the stop limit claive asks it
to stop: a PreToolUse hook (`"$CLAIVE_BIN" _budget_gate`, added through `--settings`) denies each
tool call, so no large tool result enters first, and claive sends the stop message on stdin. If
the gate fails, that message arrives one tool result late, and compaction is still possible. A
tool error that starts with `PreToolUse:` and contains `[claive budget]` in the worker log is this
gate, not a failure.
The worker then ends its session with a reply that starts with `[claive-handoff]` and includes
a nonblank `## Owed to the parent` section. Claive saves the reply in `<job>/handoffs/` and
continues the same logical turn in a fresh session. Versioned manifests in `<job>/budget-context/`
reference the frozen assignment, latest handoff and exact external steering records. The prompt
includes these inputs without nesting older handoffs. Raw receipt payloads stay out of public JSON.
Matching provider replay confirms context entry. It does not prove compliance or an answered question.
The inbox gets a `budget_handoff` event for each handoff.
At the handoff after the limit, the turn exits 3 with `needs_parent.kind == "budget_limit"`.
For a reusable worker, `claive answer` or `followup` is the permission to continue from the
saved context. A one-shot worker cannot take an answer: start a new worker with the saved prompt
and available context. Invalid handoffs and unavailable inputs exit 3 with
`needs_parent.kind == "budget_recovery"`. `show` names the artifacts and recovery action.
Restore missing or corrupt inputs before resuming, or start a new worker with a fresh assignment.
An answer alone cannot recreate missing data. An `ask` remains open during automatic continuation,
and budget pauses provide guidance rather than an answer. Its original deadline still applies.
`--turn-timeout` covers all sessions of the turn. Claude's own compaction is the fallback, at
stop + 20k for Haiku and stop + 25k for other models. `show` counts each compaction as `missed`. Spec: [design-budget-mode.md](design-budget-mode.md).

## Forks, schedules, monitors and channels

`claive fork ID --prompt '...'` starts an independent reusable worker with a
native copy of an idle Pi, Codex or Claude session. It reuses the workspace and
launch policy. It does not create an isolated worktree.

`claive send ID --message '...' --at +30m [--every 1h]` schedules a wake.
`--every` needs `--at` and must be at least 30 seconds. `--mode now` cannot schedule.
Use `claive schedule list ID` and `claive schedule cancel ID SCHEDULE_ID`.

`claive monitor add ID --command 'git status --short' --every 5m` registers a
parent command in the worker workspace. Cadence is 30s through 24h.
Commands must be read-only. Claive does not sandbox them.
Use `monitor list [ID]`, `monitor cancel MONITOR_ID`, or `monitor run-due`.

`claive channel open NAME ID1 ID2` enables bounded peer messages for two to four
active reusable workers. Defaults: 20 messages, six per pair, 900 seconds.
Members can use `claive send PEER --message '...' --mode note|wake`.
Workers cannot open channels or use peer `ask`, `now`, or scheduled sends.
Use `channel show NAME` or `channel close NAME`.

Exact options and examples: [command reference](command-reference.md).

## Safety

- Workers get `CLAIVE_WORKER_ID` and cannot launch claive workers or batches (this
  prevents recursive claive orchestration when a worker reads these skills).
  Claude's default tools can start native subagents. Claive does not track them.
  `CLAIVE_ALLOW_NESTED=1` overrides this; use it only on purpose.
- `claive engines [--json]` reports the user's engine policy (enabled state and
  reason, default and disabled models and efforts, steering transport). Launches,
  follow-ups and batch plans that violate it are refused before any model call.
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

## Execution, verification, and parent acceptance

A `completed` worker executed normally. Each new turn starts with verification
`not_run` and acceptance `pending`. A worker's `claive-report` remains its own
claim; it cannot create parent verification or acceptance evidence.

Declare checks and ownership before launching:

```sh
claive open --workspace /abs/repo --prompt-file /abs/task.md \
  --worktree --owned-path 'src/*' --owned-path 'tests/*' \
  --checks-file /abs/checks.json
```

The checks file is an array of objects such as
`{"name":"analyzer","command":["systemd-analyze","verify","service.timer"],"required":true,"timeout":60}`.
For a simple check, use `--check 'unit=python3 -m unittest'` instead. Repeat
`--check` to add checks or combine it with a checks file. Use `--fit-checked`
when the parent already assessed delegation fit and scope.
Derive expected results from the requirement or independent data. Do not use the
program's own output as the expected result.
Commands use argv arrays, run from fresh scratch copies, and record output,
exit code, environment, and `passed`, `failed`, or `blocked` outcomes.
For pytest checks, add `"pytest":{"min_passed":1,"max_skipped":0}` to require
a real passing summary and reject silent skips even when pytest exits zero.
Verifier scratch copies omit dependency/cache/build directories, reject symlinks
that escape the copy, and capture bounded output tails. Checks requiring Git
metadata must use an explicit external verification environment, since scratch
copies do not include `.git`. The initial
state and checks not executed are `not_run`. The Git workspace snapshot includes
tracked files, deletions, and all untracked files, including ignored build output.
It compares content against the dirty baseline rather than trusting a file list
in the worker's report. Ownership validation is retrospective; use isolated
worktrees for writers. Muse and Codex provide native read-only restrictions.
Pi requests read-only behavior from the model and supplies no enforcement.
Concurrent writers with declared ownership must use separate worktrees.
Managed worktrees with a verification/ownership contract are retained for the
parent to verify and integrate; cleanup skips them while acceptance is pending.
All engines accept `--worktree` and `--worktree-setup`. The latter runs once
under the heavy lock. Use `claive worktree list` and `claive worktree clean
--dry-run` to inspect logged trees. Use `claive prune --interrupted` to remove
old interrupted jobs after their worktrees are cleared.

```sh
claive verify ID
claive verify ID --check analyzer --status blocked \
  --reason 'systemd analyzer unavailable' --environment 'ARM test host'
claive accept ID
claive accept ID --exception 'Parent approved alternate analyzer evidence'
claive accept ID --reject --reason 'Required regression failed'
claive scope ID --owned-path 'src/*' --owned-path MANIFEST.in \
  --reason 'Packaging needs the manifest'
```

A blocked/unrun mandatory check prevents acceptance unless the parent records
an explicit exception. Failed mandatory checks and ownership violations cannot
be bypassed with that exception; ownership requires an explicit scope change.
Acceptance is bound to the settled turn. New turns start pending, verification
changes clear prior acceptance, and later workspace edits make the evidence
stale. Parent-only commands reject recursive worker invocation. Evidence is
retained under `evidence/turn-NNNN/`; execution status and normal worker exit
codes are unchanged.

`show --json` performs an on-demand workspace freshness check, exposing `fresh`,
`stale`, or `unknown` independently of the historical acceptance record.
`show --json` exposes queue depth and pending request identifiers separately from
current phase and last observed activity. Cumulative warnings remain available;
current-turn/previous-turn counts and structured details preserve turn meaning.
`usage --json` includes recent per-call input/cache counters and input growth.
Those measurements describe provider requests, not the current context-window
size; cache reuse alone does not establish efficiency.

## Runtime installation checks

`claive runtime --source-dir /abs/claive --json` reports installation provenance,
content drift, and smoke checks; detected drift exits nonzero. `doctor` also
shows these checks as advisory warnings. Synchronize a stale runtime with this
project's `install.sh`, which backs up changed destinations and copies the whole
package. Codex exec turns retain `--ignore-rules` when user config is enabled.
The App Server wrapper does not pass that flag. Missing provenance/source is reported explicitly.

For genuine steering at the next Pi/Codex/Claude model boundary, see [steering.md](steering.md).
Use `install.sh --runtime-only` when syncing just the runtime; Skillshare owns skills.

The installer backs up and migrates symlinks to this checkout's own command
launchers into installed launchers, so their imports use the deployed package.
Skillshare links and unrelated launcher links are preserved.
