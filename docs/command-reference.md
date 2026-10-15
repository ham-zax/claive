# Command reference

Synopses and option names come from `bin/claive --help` and each subcommand's
help output, checked on 2026-10-15. Exit codes come from the current code.
Use `claive COMMAND --help` for the full help text.

File arguments use absolute paths. Worker addresses accept a 12-hex ID,
`@alias`, or a unique 4..11 character ID prefix.
Examples use placeholders such as `WORKER_ID` and `/abs/task.md`.

Common errors: 1 for refusal or handled errors, 2 for argument parsing,
130 for a keyboard interrupt. These codes apply unless a section states otherwise.
Every command accepts `-h, --help` and exits 0 after printing help.

Worker outcomes: 0 completed, 3 needs a parent decision, 130 cancelled.
Failures return a positive recorded engine code, capped at 255, or 1.
A launch, queued request or completed process does not establish parent acceptance.

## Launch options

`run`, `start` and `open` share these options. Role values override engine
config defaults. Explicit flags override role values. A read-only role stays
read-only. Only Muse supports a model-step cap and managed worktrees.

| Option | Meaning |
|---|---|
| `--workspace` | Existing absolute workspace directory |
| `--prompt-file` | Existing nonempty absolute task file |
| `--context-file` | add a bounded UTF-8 context layer to the first prompt (repeatable) |
| `--label` | Display label |
| `--engine` | muse, pi, claude or codex |
| `--role` | Built-in or custom role |
| `--team` | Configured team name. Requires --member and forbids --role |
| `--member` | Team member name. Requires --team |
| `--project-file` | override the team's project context for this launch |
| `--enable-steering` | opt into the duplex provider transport (Pi, Codex, Claude) |
| `--checks-file` | absolute JSON required-check manifest for parent verification |
| `--owned-path` | workspace-relative allowed path or glob (repeatable) |
| `--report` | request a structured claive-report block |
| `--reasoning-effort`, `--effort` | Engine effort. Muse/Claude default xhigh, Pi max, Codex Luna max or Sol medium. Role/config defaults can override |
| `--max-model-steps` | Optional positive Muse step cap. No default cap |
| `--read-only` | Request the engine's read-only mechanism. Pi uses an instruction |
| `--worktree` | Muse only. Create a linked worktree from the selected base |
| `--worktree-existing` | resume a lane in its existing absolute worktree path |
| `--worktree-base` | Muse base commit. Default HEAD |
| `--web` | compatibility flag. web is always on for Claude, Codex and Muse. Pi refuses it |
| `--model` | Engine model. Pins and config policy apply |
| `--session-id` | reuse this engine's session ID with the same workspace and policy |
| `--output-schema` | Absolute JSON schema file. Muse and Claude only |
| `--no-session-log` | Disable retained history. Single-turn run/start only |
| `--provider` | Engine provider. Pi accepts only opencode2api |
| `--mission` | link the new worker to a mission |
| `--turn-timeout` | fail a turn that runs longer than SECONDS (positive number) |
| `--fallback-models` | comma-separated fallback models for single-turn workers (run/start only) |

First-prompt order: role, team header, collaboration, project, context files,
task, report contract. Context layers repeat up to ten times.
Layer files must be absolute, nonempty UTF-8 text, at most 256 KiB.
Duplicate context files are refused. `--project-file` needs `--team`.

Claude, Codex and Muse always have web access. `--web` has no effect on them.
Pi refuses `--web` and requests read-only behavior through prompt instructions.
Codex steering/fork turns refuse `engines.codex.extra_args`.
See [engine configuration](../README.md#per-host-engine-config).

## `run`

Run one tracked turn in the foreground.

```text
claive run --workspace WORKSPACE --prompt-file PROMPT_FILE [launch options]
```

Options: [launch options](#launch-options).

Exit codes: Worker outcome. See the worker codes below.

Example: `claive run --workspace /abs/repo --prompt-file /abs/task.md --role reviewer`.

## `start`

Start one tracked turn in the background. Exit success reports launch, not completion.

```text
claive start --workspace WORKSPACE --prompt-file PROMPT_FILE [launch options]
```

Options: [launch options](#launch-options).

Exit codes: 0 after launch. Inspect the worker with `wait` for its outcome.

Example: `claive start --workspace /abs/repo --prompt-file /abs/task.md --turn-timeout 300`.

## `open`

Open a reusable worker. Without `--detach`, the supervisor stays in the foreground.

```text
claive open --workspace WORKSPACE --prompt-file PROMPT_FILE [launch options] [--detach]
```

Options: [launch options](#launch-options), plus `--detach` for a background supervisor.

Exit codes: Worker outcome on foreground closure. `--detach` returns 0 after launch.

Example: `claive open --detach --workspace /abs/repo --prompt-file /abs/task.md --role worker`.

## `fork`

Fork an idle reusable Pi, Codex or Claude session into a detached reusable worker. Muse refuses forks.

```text
claive fork [--label LABEL] (--prompt PROMPT | --prompt-file PROMPT_FILE) [--context-file
  CONTEXT_FILE] id
```

| Option | Meaning |
|---|---|
| `--label` | Display label |
| `--prompt` | initial prompt text for the fork |
| `--prompt-file` | absolute file containing the initial prompt |
| `--context-file` | add a bounded UTF-8 context layer to the fork's first prompt (repeatable) |

Exit codes: 0 after launch. Inspect the fork with `wait`.

Example: `claive fork @backend --prompt 'Review another approach.'`.

## `monitor`

Manage parent commands that observe worker workspaces.

```text
claive monitor {add,list,cancel,run-due} ...
```

Options: choose a subcommand below.

Exit codes: selected subcommand's codes.

Example: `claive monitor add @backend --command 'git status --short' --every 5m`.

### `monitor add`

Register a command for an open reusable worker. Commands run with `bash -c` on the parent host.

```text
claive monitor add --command COMMAND --every EVERY [--label LABEL] id
```

| Option | Meaning |
|---|---|
| `--command` | Read-only command run with bash -c. No command sandbox. Required |
| `--every` | interval from 30s through 24h (for example 5m). Required |
| `--label` | short name shown in change and failure messages |

Exit codes: 0 on success. Common errors apply.

Example: `claive monitor add @backend --command 'git status --short' --every 5m`.

### `monitor list`

List one worker's monitors, or all monitors when no ID is supplied.

```text
claive monitor list [--json] [id]
```

| Option | Meaning |
|---|---|
| `--json` | Print JSON |

Exit codes: 0 on success. Common errors apply.

Example: `claive monitor list @backend --json`.

### `monitor cancel`

Cancel one monitor.

```text
claive monitor cancel monitor_id
```

Options: help only.

Exit codes: 0 on success. Common errors apply.

Example: `claive monitor cancel MONITOR_ID`.

### `monitor run-due`

Run one pass of due monitors. Inspect the printed failure count.

```text
claive monitor run-due
```

Options: help only.

Exit codes: 0 when the pass completes, including recorded monitor failures.

Example: `claive monitor run-due`.

## `send`

Send context to a reusable worker, or schedule a wake. Channel members can send peer `note|wake` messages.

```text
claive send [--mode {note,wake,now}] (--message MESSAGE | --message-file MESSAGE_FILE) [--id
  MESSAGE_ID] [--at AT] [--every EVERY] [--json] id
```

| Option | Meaning |
|---|---|
| `--mode` | note waits for the next turn, wake starts an idle turn, now steers or wakes. Values: note, wake, now. Default: note |
| `--message` | Inline message text |
| `--message-file` | Existing absolute message file |
| `--id` | 32 lowercase hexadecimal message ID |
| `--at` | Wake at an ISO-8601 time or +duration. Refuses --mode now |
| `--every` | Repeat duration, at least 30s. Requires --at |
| `--json` | Print JSON |

Exit codes: 0 on success. Common errors apply.

Example: `claive send @backend --message 'Review the latest diff.' --at +30m --every 1h`.

## `channel`

Manage bounded channels for peer messages.

```text
claive channel {open,show,close} ...
```

Options: choose a subcommand below.

Exit codes: selected subcommand's codes.

Example: `claive channel open design @builder @reviewer --max-messages 12`.

### `channel open`

Open a channel for two to four distinct active reusable workers. Only the parent can manage channels.

```text
claive channel open [--max-messages MAX_MESSAGES] [--max-per-pair MAX_PER_PAIR] [--timeout TIMEOUT]
  [--json] name members [members ...]
```

| Option | Meaning |
|---|---|
| `--max-messages` | Total channel message limit. Default: 20 |
| `--max-per-pair` | Message limit for each worker pair. Default: 6 |
| `--timeout` | Channel lifetime in seconds, positive. Default: 900 |
| `--json` | Print JSON |

Exit codes: 0 on success. Common errors apply.

Example: `claive channel open design @builder @reviewer --max-messages 12`.

### `channel show`

Show channel state, counts and any closing summary.

```text
claive channel show [--json] name
```

| Option | Meaning |
|---|---|
| `--json` | Print JSON |

Exit codes: 0 on success. Common errors apply.

Example: `claive channel show design --json`.

### `channel close`

Close a channel and print its summary.

```text
claive channel close name
```

Options: help only.

Exit codes: 0 on success. Common errors apply.

Example: `claive channel close design`.

## `schedule`

List or cancel scheduled wakes. Create schedules with `send --at`.

```text
claive schedule {list,cancel} ...
```

Options: choose a subcommand below.

Exit codes: selected subcommand's codes.

Example: `claive schedule list @backend --json`.

### `schedule list`

List the worker's wake schedules.

```text
claive schedule list [--json] id
```

| Option | Meaning |
|---|---|
| `--json` | Print JSON |

Exit codes: 0 on success. Common errors apply.

Example: `claive schedule list @backend --json`.

### `schedule cancel`

Cancel one wake schedule.

```text
claive schedule cancel [--json] id schedule_id
```

| Option | Meaning |
|---|---|
| `--json` | Print JSON |

Exit codes: 0 on success. Common errors apply.

Example: `claive schedule cancel @backend SCHEDULE_ID`.

## `ask`

Ask a worker and wait for `reply` or the delivered turn's final answer.

```text
claive ask (--message MESSAGE | --message-file MESSAGE_FILE) [--timeout TIMEOUT] [--id MESSAGE_ID]
  [--schema SCHEMA] [--json] id
```

| Option | Meaning |
|---|---|
| `--message` | Inline message text |
| `--message-file` | Existing absolute message file |
| `--timeout` | Reply deadline in seconds, greater than 0 through 3600. Default: 120 |
| `--id` | 32 lowercase hexadecimal message ID |
| `--schema` | existing absolute JSON schema file for the reply |
| `--json` | Print JSON |

Exit codes: 0 answered, 1 refused/failed/invalid reply, 3 needs parent, 124 withdrawn or expired.

Example: `claive ask @backend --message 'Which file owns the parser?' --timeout 120`.

## `reply`

Answer a parent question from inside a worker turn. The parent cannot use this command.

```text
claive reply (--message MESSAGE | --message-file MESSAGE_FILE) id
```

| Option | Meaning |
|---|---|
| `--message` | Inline message text |
| `--message-file` | Existing absolute message file |

Exit codes: 0 on success. Common errors apply.

Example: `claive reply MESSAGE_ID --message 'src/parser.py'`.

## `steer`

Send input to an active Pi, Codex or Claude worker launched with `--enable-steering`.

```text
claive steer (--message MESSAGE | --message-file MESSAGE_FILE) [--wait WAIT] [--json] id
```

| Option | Meaning |
|---|---|
| `--message` | Inline message text |
| `--message-file` | Existing absolute message file |
| `--wait` | wait up to SECONDS for a provider acknowledgement. Default: 0 |
| `--json` | Print JSON |

Exit codes: 0 for queued/sent/acknowledged input, 1 for rejected/not-delivered/unconfirmed input, 124 for an acknowledgement wait deadline.

Example: `claive steer @backend --message 'Check the parser first.' --wait 10 --json`.

## `runtime`

Report provenance, source drift and runtime smoke checks.

```text
claive runtime [--source-dir SOURCE_DIR] [--json]
```

| Option | Meaning |
|---|---|
| `--source-dir` | source checkout to compare against this runtime |
| `--json` | Print JSON |

Exit codes: 0 no detected drift and smoke checks pass, 2 drift or smoke failure, 3 unknown source drift.

Example: `claive runtime --source-dir /abs/claive --json`.

## `verify`

Run declared checks for a settled turn, or record an external check outcome. Output is JSON.

```text
claive verify [--check CHECK] [--status {blocked,failed,not_run,passed}] [--reason REASON]
  [--environment ENVIRONMENT] id
```

| Option | Meaning |
|---|---|
| `--check` | Select one declared check |
| `--status` | Record an external check outcome. Values: blocked, failed, not_run, passed |
| `--reason` | Reason for the recorded decision |
| `--environment` | Describe the external check environment |

Exit codes: 0 all required checks pass, 2 verification is not passed.

Example: `claive verify @backend`.

## `accept`

Record parent acceptance or rejection of a settled turn. Output is JSON.

```text
claive accept [--reject] [--reason REASON] [--exception EXCEPTION] id
```

| Option | Meaning |
|---|---|
| `--reject` | Record rejection instead of acceptance |
| `--reason` | Reason for the recorded decision |
| `--exception` | explicit parent exception for blocked or unrun mandatory checks |

Exit codes: 0 on success. Common errors apply.

Example: `claive accept @backend`.

## `scope`

Record a parent change to write ownership. Acceptance becomes pending.

```text
claive scope --owned-path OWNED_PATH --reason REASON id
```

| Option | Meaning |
|---|---|
| `--owned-path` | Workspace-relative path or glob, repeatable. Required |
| `--reason` | Reason for the recorded decision. Required |

Exit codes: 0 on success. Common errors apply.

Example: `claive scope @backend --owned-path 'src/*' --reason 'Include parser files.'`.

## `list`

List active workers and recent results.

```text
claive list [--json]
```

| Option | Meaning |
|---|---|
| `--json` | Print JSON |

Exit codes: 0 on success. Common errors apply.

Example: `claive list --json`.

## `alias`

Manage persistent worker names. An alias does not start or reopen a worker.

```text
claive alias {set,list,remove} ...
```

Options: choose a subcommand below.

Exit codes: selected subcommand's codes.

Example: `claive alias set backend WORKER_ID`.

### `alias set`

Bind a name to an existing worker.

```text
claive alias set [--replace] name id
```

| Option | Meaning |
|---|---|
| `--replace` | explicitly replace an existing binding |

Exit codes: 0 on success. Common errors apply.

Example: `claive alias set backend WORKER_ID`.

### `alias list`

List bindings and current worker status.

```text
claive alias list [--json]
```

| Option | Meaning |
|---|---|
| `--json` | Print JSON |

Exit codes: 0 on success. Common errors apply.

Example: `claive alias list --json`.

### `alias remove`

Remove a binding. Keep the worker and history.

```text
claive alias remove name
```

Options: help only.

Exit codes: 0 on success. Common errors apply.

Example: `claive alias remove backend`.

## `team`

Read configured teams and member aliases. These commands do not launch workers.

```text
claive team {list,show} ...
```

Options: choose a subcommand below.

Exit codes: selected subcommand's codes.

Example: `claive team list --json`.

### `team list`

List configured teams.

```text
claive team list [--json]
```

| Option | Meaning |
|---|---|
| `--json` | Print JSON |

Exit codes: 0 on success. Common errors apply.

Example: `claive team list --json`.

### `team show`

Show one team's files, members, roles and aliases.

```text
claive team show [--json] name
```

| Option | Meaning |
|---|---|
| `--json` | Print JSON |

Exit codes: 0 on success. Common errors apply.

Example: `claive team show backend --json`.

## `conversation`

Coordinate bounded exchanges between existing reusable workers.

```text
claive conversation {start,show} ...
```

Options: choose a subcommand below.

Exit codes: selected subcommand's codes.

Example: `claive conversation start @builder @reviewer --message-file /abs/topic.md --rounds 2`.

### `conversation start`

Run ordered reply rounds across two to eight idle reusable workers. No workers are created.

```text
claive conversation start (--message MESSAGE | --message-file MESSAGE_FILE) [--rounds ROUNDS]
  [--timeout TIMEOUT] [--json] workers [workers ...]
```

| Option | Meaning |
|---|---|
| `--message` | Inline message text |
| `--message-file` | Existing absolute message file |
| `--rounds` | rounds of replies per participant (1..8. default 2). Default: 2 |
| `--timeout` | Total scheduling/wait budget in seconds, positive. Default: 300 |
| `--json` | Print JSON |

Exit codes: 0 completed, 3 needs parent, 124 deadline, 130 cancelled, or the failed participant's outcome.

Example: `claive conversation start @builder @reviewer --message-file /abs/topic.md --rounds 2`.

### `conversation show`

Read a retained conversation transcript.

```text
claive conversation show [--json] id
```

| Option | Meaning |
|---|---|
| `--json` | Print JSON |

Exit codes: 0 on success. Common errors apply.

Example: `claive conversation show CONVERSATION_ID --json`.

## `show`

Show worker state, result and evidence. JSON also includes current policy and workspace freshness.

```text
claive show [--json] id
```

| Option | Meaning |
|---|---|
| `--json` | Print JSON |

Exit codes: 0 on success. Common errors apply.

Example: `claive show @backend --json`.

## `logs`

Print the last event or stderr lines.

```text
claive logs [--stderr] [--lines LINES] id
```

| Option | Meaning |
|---|---|
| `--stderr` | Read stderr instead of events |
| `--lines` | Number of trailing lines. Default: 20 |

Exit codes: 0 on success. Common errors apply.

Example: `claive logs @backend --stderr --lines 40`.

## `cancel`

Request cancellation. The command returns before the worker settles. Inspect the outcome with `wait`.

```text
claive cancel id
```

Options: help only.

Exit codes: 0 on success. Common errors apply.

Example: `claive cancel @backend`.

## `close`

Request closure after assigned turns. Retain session history.

```text
claive close id
```

Options: help only.

Exit codes: 0 on success. Common errors apply.

Example: `claive close @backend`.

## `usage`

Read recorded provider counters. Missing counters mean unknown.

```text
claive usage [--json] id
```

| Option | Meaning |
|---|---|
| `--json` | Print JSON |

Exit codes: 0 on success. Common errors apply.

Example: `claive usage @backend --json`.

## `peek`

Read numbered recent steps without interrupting the worker. Parent only.

```text
claive peek [--steps STEPS] [--start START] [--json] id
```

| Option | Meaning |
|---|---|
| `--steps` | Number of recent steps. Default: 5 |
| `--start` | first step number to show (pages forward) |
| `--json` | Print JSON |

Exit codes: 0 on success. Common errors apply.

Example: `claive peek @backend --steps 5 --start 10`.

## `wait`

Wait for a settled worker or new inbox events. Several IDs require `--any`.

```text
claive wait [--any] [--timeout TIMEOUT] [--inbox CONSUMER] [--json] [id ...]
```

| Option | Meaning |
|---|---|
| `--any` | Return when the first worker settles |
| `--timeout` | Wait deadline in seconds, positive |
| `--inbox` | also return on new inbox events for CONSUMER |
| `--json` | print one JSON object with the event that ended the wait |

Exit codes: Worker outcome, 124 wait deadline, or inbox result: 3 needs parent, otherwise 0.

Example: `claive wait @builder @reviewer --any --timeout 30 --json`.

## `inbox`

Read events since a named consumer's cursor.

```text
claive inbox [--consumer CONSUMER] [--peek] [--json]
```

| Option | Meaning |
|---|---|
| `--consumer` | Independent inbox cursor name. Default: default |
| `--peek` | Do not advance the inbox cursor |
| `--json` | Print JSON |

Exit codes: 0 on success. Common errors apply.

Example: `claive inbox --consumer codex --peek --json`.

## `batch`

Manage plans with parallel lanes and sequential stages.

```text
claive batch {validate,start,status,wait,cancel,retry,list} ...
```

Options: choose a subcommand below.

Exit codes: selected subcommand's codes.

Example: `claive batch validate /abs/plan.json`.

### `batch validate`

Validate a plan without launching workers.

```text
claive batch validate plan
```

Options: help only.

Exit codes: 0 on success. Common errors apply.

Example: `claive batch validate /abs/plan.json`.

### `batch start`

Start a validated plan in the background.

```text
claive batch start [--json] plan
```

| Option | Meaning |
|---|---|
| `--json` | Print JSON |

Exit codes: 0 on success. Common errors apply.

Example: `claive batch start /abs/plan.json --json`.

### `batch status`

Show batch and stage state.

```text
claive batch status [--json] id
```

| Option | Meaning |
|---|---|
| `--json` | Print JSON |

Exit codes: 0 on success. Common errors apply.

Example: `claive batch status BATCH_ID --json`.

### `batch wait`

Wait for the batch outcome.

```text
claive batch wait [--timeout TIMEOUT] id
```

| Option | Meaning |
|---|---|
| `--timeout` | Wait deadline in seconds, positive |

Exit codes: Stored batch outcome, or 124 while still running.

Example: `claive batch wait BATCH_ID --timeout 30`.

### `batch cancel`

Request cancellation of a running batch.

```text
claive batch cancel id
```

Options: help only.

Exit codes: 0 on success. Common errors apply.

Example: `claive batch cancel BATCH_ID`.

### `batch retry`

Retry unfinished stages. Keep completed stages and still-running workers.

```text
claive batch retry id
```

Options: help only.

Exit codes: 0 on success. Common errors apply.

Example: `claive batch retry BATCH_ID`.

### `batch list`

List retained batches.

```text
claive batch list [--json]
```

| Option | Meaning |
|---|---|
| `--json` | Print JSON |

Exit codes: 0 on success. Common errors apply.

Example: `claive batch list --json`.

## `mission`

Track goals, notes and links across parent sessions.

```text
claive mission {new,note,link,show,list,close} ...
```

Options: choose a subcommand below.

Exit codes: selected subcommand's codes.

Example: `claive mission new --title Parser --goal-file /abs/goal.md`.

### `mission new`

Create a mission from inline text or a goal file.

```text
claive mission new --title TITLE (--goal GOAL | --goal-file GOAL_FILE)
```

| Option | Meaning |
|---|---|
| `--title` | Display title. Required |
| `--goal` | Inline goal text |
| `--goal-file` | Existing nonempty absolute goal file |

Exit codes: 0 on success. Common errors apply.

Example: `claive mission new --title Parser --goal-file /abs/goal.md`.

### `mission note`

Append a parent note.

```text
claive mission note id text
```

Options: help only.

Exit codes: 0 on success. Common errors apply.

Example: `claive mission note MISSION_ID 'Use the existing parser.'`.

### `mission link`

Link one worker, batch or arbiter run.

```text
claive mission link (--worker WORKER | --batch BATCH | --run RUN) id
```

| Option | Meaning |
|---|---|
| `--worker` | Worker ID or alias |
| `--batch` | Batch ID |
| `--run` | Arbiter run ID |

Exit codes: 0 on success. Common errors apply.

Example: `claive mission link MISSION_ID --worker @backend`.

### `mission show`

Show links, live status and the next action.

```text
claive mission show [--json] id
```

| Option | Meaning |
|---|---|
| `--json` | Print JSON |

Exit codes: 0 on success. Common errors apply.

Example: `claive mission show MISSION_ID --json`.

### `mission list`

List open missions, or all missions with `--all`.

```text
claive mission list [--all] [--json]
```

| Option | Meaning |
|---|---|
| `--all` | Include closed missions |
| `--json` | Print JSON |

Exit codes: 0 on success. Common errors apply.

Example: `claive mission list --all --json`.

### `mission close`

Close a mission record.

```text
claive mission close id
```

Options: help only.

Exit codes: 0 on success. Common errors apply.

Example: `claive mission close MISSION_ID`.

## `goal`

Manage the durable queue that `serve` runs.

```text
claive goal {add,list,show,answer,cancel,retry} ...
```

Options: choose a subcommand below.

Exit codes: selected subcommand's codes.

Example: `claive goal add --title Audit --workspace /abs/repo --prompt-file /abs/task.md`.

### `goal add`

Queue a goal. Read-only is the default.

```text
claive goal add --title TITLE --prompt-file PROMPT_FILE --workspace WORKSPACE [--role ROLE]
  [--engine ENGINE] [--model MODEL] [--write] [--budget BUDGET] [--max-attempts MAX_ATTEMPTS]
```

| Option | Meaning |
|---|---|
| `--title` | Display title. Required |
| `--prompt-file` | Existing nonempty absolute task file. Required |
| `--workspace` | Existing absolute workspace directory. Required |
| `--role` | Built-in or custom role |
| `--engine` | muse, pi, claude or codex |
| `--model` | Engine model. Pins and config policy apply |
| `--write` | let the worker edit the workspace |
| `--budget` | wall-clock budget: seconds, or 30m / 2h (default 2h) |
| `--max-attempts` | attempts before the goal fails (default 3) |

Exit codes: 0 on success. Common errors apply.

Example: `claive goal add --title Audit --workspace /abs/repo --prompt-file /abs/task.md`.

### `goal list`

List queued and active goals.

```text
claive goal list [--all] [--json]
```

| Option | Meaning |
|---|---|
| `--all` | include finished goals |
| `--json` | Print JSON |

Exit codes: 0 on success. Common errors apply.

Example: `claive goal list --all --json`.

### `goal show`

Read one goal record.

```text
claive goal show [--json] id
```

| Option | Meaning |
|---|---|
| `--json` | Print JSON |

Exit codes: 0 on success. Common errors apply.

Example: `claive goal show GOAL_ID --json`.

### `goal answer`

Answer a parked goal and resume its work.

```text
claive goal answer --message MESSAGE id
```

| Option | Meaning |
|---|---|
| `--message` | Inline message text. Required |

Exit codes: 0 on success. Common errors apply.

Example: `claive goal answer GOAL_ID --message 'Use the existing parser.'`.

### `goal cancel`

Cancel a goal and any active worker.

```text
claive goal cancel id
```

Options: help only.

Exit codes: 0 on success. Common errors apply.

Example: `claive goal cancel GOAL_ID`.

### `goal retry`

Requeue a failed, cancelled or timed-out goal.

```text
claive goal retry id
```

Options: help only.

Exit codes: 0 on success. Common errors apply.

Example: `claive goal retry GOAL_ID`.

## `serve`

Run the goal queue in the foreground, or manage its stop switch.

```text
claive serve [--stop | --resume | --status | --once] [--interval INTERVAL] [--max-parallel
  MAX_PARALLEL]
```

| Option | Meaning |
|---|---|
| `--stop` | set the stop switch and return |
| `--resume` | clear the stop switch and return |
| `--status` | print serve, stop switch and queue state |
| `--once` | run one pass and exit |
| `--interval` | seconds between passes (default 5). Default: 5 |
| `--max-parallel` | goals run at once (default 1). Default: 1 |

Exit codes: 0 on success. Common errors apply.

Example: `claive serve --once`.

## `followup`

Queue another turn on a reusable worker. Overrides affect only that turn.

```text
claive followup --prompt-file PROMPT_FILE [--label LABEL] [--reasoning-effort REASONING_EFFORT]
  [--max-model-steps MAX_MODEL_STEPS] id
```

| Option | Meaning |
|---|---|
| `--prompt-file` | Existing nonempty absolute task file. Required |
| `--label` | Display label |
| `--reasoning-effort`, `--effort` | override this turn. otherwise use worker policy |
| `--max-model-steps` | Muse step cap for future turns, positive |

Exit codes: 0 on success. Common errors apply.

Example: `claive followup @backend --prompt-file /abs/next.md --effort high`.

## `effort`

Change a reusable worker's defaults for future turns. The active request keeps its settings.

```text
claive effort --reasoning-effort REASONING_EFFORT [--max-model-steps MAX_MODEL_STEPS] id
```

| Option | Meaning |
|---|---|
| `--reasoning-effort`, `--effort` | Engine-specific future effort. Required |
| `--max-model-steps` | Muse step cap for future turns, positive |

Exit codes: 0 on success. Common errors apply.

Example: `claive effort @backend --effort xhigh`.

## `watch`

Display live worker state. Stopping the view does not stop workers.

```text
claive watch [--interval INTERVAL] [--once] [--no-color] [--compact]
```

| Option | Meaning |
|---|---|
| `--interval` | Refresh/pass interval in seconds. Default: 1 |
| `--once` | Run one pass |
| `--no-color` | Disable terminal color |
| `--compact` | at most five lines. one row per worker |

Exit codes: 0 on success. Common errors apply.

Example: `claive watch --compact --once`.

## `status-line`

Print one line of worker counts.

```text
claive status-line
```

Options: help only.

Exit codes: 0 on success. Common errors apply.

Example: `claive status-line`.

## `engines`

Read engine policy, defaults, allowed models and steering support.

```text
claive engines [--json]
```

| Option | Meaning |
|---|---|
| `--json` | Print JSON |

Exit codes: 0 on success. Common errors apply.

Example: `claive engines --json`.

## `doctor`

Check binaries, config, state, Pi provider and runtime. A model runs only with `--live`.

```text
claive doctor [--json] [--live]
```

| Option | Meaning |
|---|---|
| `--json` | Print JSON |
| `--live` | also send one tiny read-only Pi turn (no session) and time it |

Exit codes: 0 all required checks pass, 1 a required check fails.

Example: `claive doctor --json`.

## `answer`

Queue a decision for a reusable worker waiting for the parent.

```text
claive answer (--message MESSAGE | --message-file MESSAGE_FILE) id
```

| Option | Meaning |
|---|---|
| `--message` | Inline message text |
| `--message-file` | Existing absolute message file |

Exit codes: 0 on success. Common errors apply.

Example: `claive answer @backend --message 'Use design B.'`.

## `tmux`

Open Codex above a worker pane in a separate tmux session.

```text
claive tmux ...
```

Options: arguments after `--` go to Codex.

Exit codes: tmux attach/switch exit code.

Example: `claive tmux -- -C /abs/repo`.
