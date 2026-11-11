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
config defaults. `--set` overrides the config file. Explicit flags override role
values and `--set`. A read-only role stays read-only. Only Muse supports a
model-step cap.

| Option | Meaning |
|---|---|
| `--workspace` | Existing absolute workspace directory |
| `--prompt-file` | Existing nonempty absolute task file |
| `--context-file` | add a bounded UTF-8 context layer to the first prompt (repeatable) |
| `--label` | Display label |
| `--alias` | Bind `@NAME` to the new worker. The name must be unused |
| `--engine` | muse, pi, claude, codex or agy |
| `--role` | Built-in or custom role |
| `--team` | Configured team name. Requires --member and forbids --role |
| `--member` | Team member name. Requires --team |
| `--project-file` | override the team's project context for this launch |
| `--enable-steering` / `--disable-steering` | enable/disable duplex transport; enabled by default for Pi writers and Claude with a turn budget. Codex uses its existing exec transport unless steering is explicitly enabled. Disabling steering also disables Claude's context budget. |
| `--checks-file` | absolute JSON required-check manifest for parent verification |
| `--check` | required check as `NAME=COMMAND` (repeatable); combines with `--checks-file` |
| `--fit-checked` | omit delegation fit and scope reminders |
| `--owned-path` | workspace-relative allowed path or glob (repeatable) |
| `--report` | request a structured claive-report block |
| `--reasoning-effort`, `--effort` | Engine effort. Muse/Claude default xhigh, Pi max, Codex Terra xhigh, Luna max, or Sol medium. Role/config defaults can override |
| `--max-model-steps` | Optional positive Muse step cap. No default cap |
| `--read-only` | Native enforced reading where supported. AGY, Pi and Claude use a reported soft instruction |
| `--require-read-only` | Require native write prevention. Refuse engines with only soft read mode |
| `--worktree` | Create a linked worktree from the selected base |
| `--worktree-existing` | resume a lane in its existing absolute worktree path |
| `--worktree-base` | base commit. Default HEAD |
| `--worktree-setup` | run a shell command once in the new worktree under the heavy lock |
| `--web` | compatibility flag. web is always on for Claude, Codex and Muse. Pi refuses it |
| `--model` | Engine model. Pins and config policy apply. Pi also accepts `provider/id` |
| `--allow-model` | run a model outside the allowed tier for this worker (repeatable). The permission is recorded on the launch and kept for follow-ups |
| `--set` | temporarily override any config key for this worker (repeatable). Value is a YAML or JSON object |
| `--session-id` | reuse this engine's session ID with the same workspace and policy |
| `--output-schema` | Absolute JSON schema file. Muse and Claude only |
| `--no-session-log` | Disable retained history. Single-turn run/start only |
| `--provider` | Engine provider. Pi accepts any provider it knows; see `claive pi provider list` |
| `--mission` | link the new worker to a mission |
| `--turn-timeout` | fail a turn that runs longer than SECONDS (positive number) |
| `--turn-budget` | alias for `--turn-timeout`; a total budget, independent of stream activity |
| `--stall-timeout` | inactivity watchdog in seconds; default 180, 0 disables. Active local tools are exempt. |
| `--turn-grace` | extra wrap-up seconds after the total budget; default 0. SIGTERM still escalates to SIGKILL after the existing process grace. |
| `--idle-ttl` | suspend an unused reusable supervisor after this many seconds; default 7200, 0 disables |
| `--fallback-models` | comma-separated fallback models for run/start. Pi accepts `provider/model` to switch provider. Defaults to `engines.<name>.fallback_models`. AGY also supports reusable open workers |

`--set` is the general override for a key that has no dedicated flag:

```sh
claive run --set '{engines: {pi: {prompt_limit: 200000}}}' ...
claive run --set '{"engines": {"codex": {"disabled_model_efforts": {"gpt-6.1-sol": ["max"]}}}}' ...
```

The object deep-merges over the config file, and a dedicated flag still wins. The
override lives in the worker record, so follow-up turns keep it. It never writes the
file. The launch prints the merged override to stderr.

First-prompt order: role, team header, collaboration, project, context files,
task, report contract. Context layers repeat up to ten times.
Layer files must be absolute, nonempty UTF-8 text, at most 256 KiB.
Duplicate context files are refused. `--project-file` needs `--team`.

Claude, Codex and Muse always have web access. `--web` has no effect on them.
Pi refuses `--web` and requests read-only behavior through prompt instructions.
Codex steering/fork turns refuse `engines.codex.extra_args`.
See [engine configuration](../README.md#per-host-engine-config).

The config object `launch` accepts `start_timeout`, `git_timeout`, and
`worktree_setup_timeout`, in seconds. The first two accept numbers from 1 to 600.
The setup timeout accepts numbers from 1 to 3600 and defaults to 900 seconds.
The default start timeout adapts to load, from 5 to 60 seconds. The Git timeout defaults to 30 seconds.

`claive-memcap LIMIT --lock NAME [--lock-timeout S] -- COMMAND` holds a named lock
while the command runs. A lock timeout exits 75.

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

If the supervisor is alive but has not confirmed its start, `start` prints a warning
and returns 0. Run `claive show ID` before any relaunch.

Example: `claive start --workspace /abs/repo --prompt-file /abs/task.md --turn-timeout 300`.

## `prune`

`claive cleanup --idle-older-than 2h [--dry-run] [--json]` safely requests closure
of old idle reusable supervisors without deleting sessions or history. The same
operation is available as `claive prune --idle-older-than 2h`. Workers with parent
questions, queued work, monitors or scheduled wakes are skipped.

Reusable supervisors also suspend automatically after `--idle-ttl`. `send` restarts
an automatically suspended supervisor using its retained session; it does not replay
the previous assignment. An explicit `close` prevents automatic restart. Changed
worktrees remain available; clean managed worktrees can be recreated at the same path.
`launch.stall_timeout` and `launch.idle_ttl` configure the defaults globally.

At 85% of a total turn budget, a steerable worker receives one wrap-up instruction.
Configured model fallbacks disable automatic steering; an explicit combination is
rejected. For engines that emit no events during a long model step, increase
`--stall-timeout` or use `--stall-timeout 0` to disable the inactivity watchdog.
Three Pi output-token truncations without a successful tool stop the attempt with
`failure_kind: output_truncated`; the first receives chunking guidance when steering
is enabled. Successful tool execution resets the consecutive counter. Failed writer
turns and missing reports retain bounded workspace status/diff evidence, including
untracked paths. These changes describe the workspace, including pre-existing edits.

Remove interrupted workers older than the selected duration. Workers with a retained
worktree stay in the registry. Aliases to removed workers are removed too.

```text
claive prune --interrupted [--older-than DUR] [--dry-run] [--json]
```

`--older-than` defaults to `24h`. Use a unit-suffixed duration such as `30m` or `2d`.
`--dry-run` reports the selected workers without deleting them. `--json` prints JSON.
Exit code: 0 on success.

## `worktree`

Claive creates worktrees only when a launch uses `--worktree`. Events are recorded
in `worktrees.jsonl` in the Claive state directory.

```text
claive worktree list [--json]
claive worktree clean [--older-than DUR] [--dry-run] [--discard-changed] [--json]
```

`list` shows logged worktrees, their job status, age, size, branch, changes and source.
`clean` defaults to worktrees older than `24h`. It skips worktrees used by any
active worker and those with missing job state. Changed worktrees, including
ignored dependency directories, stay unless `--discard-changed` is set.
Worktrees awaiting parent acceptance stay even with that flag. Generated tool
caches such as `__pycache__` do not keep a worktree. `--dry-run` removes nothing.
Exit code: 0 on success.

## `pi provider`

Inspect and edit Pi's provider configuration. Pi owns provider definitions, its
built-in catalog and credentials; Claive owns policy and reporting.

```text
claive pi provider list [--json]
claive pi provider add NAME --base-url URL --api API --model ID [--model ID ...]
                              [--api-key-env VAR] [--display-name NAME] [--replace]
claive pi provider remove NAME
claive pi provider check NAME [--json]
```

`list` merges `models.json` with Pi's built-in catalog and reports each provider's
source, model count and auth readiness. `add` writes `models.json` atomically at
mode `0600` after one timestamped backup; it refuses an existing provider without
`--replace`, an unsupported `--api`, a non-http(s) URL, and an empty model list.
`--api-key-env` stores `"$VAR"`, so Claive never stores a literal key and never
reads `auth.json`. `remove` deletes the entry and says so when Pi still serves
that provider from its built-in catalog. `check` reports readiness from
`pi auth check --provider NAME --json` and times `GET /models` when the provider
has a `baseUrl`; it never prints the key.

Exit code: 0 on success, 1 when `check` finds the provider not ready, 2 on a
refused edit.

## `config`

Inspect, validate or create the config file. The file is YAML or JSON; YAML is
preferred. Discovery is `$CLAIVE_CONFIG`, then `config.yaml`, `config.yml`,
`config.json` in `${XDG_CONFIG_HOME:-~/.config}/claive/`.

```text
claive config path
claive config check [--json]
claive config show [--json]
claive config init [--force]
claive config migrate [--force]
```

- `path` prints the active file, one line.
- `check` parses and validates it, then runs the model-allowlist cross-checks a
  launch runs. It also names a shadowed file. Exit 1 with the exact error.
- `show` prints the file as text, or as JSON with `--json`.
- `init` writes a commented YAML template with every key. It refuses to overwrite
  an existing file, and refuses to shadow a different config file, unless
  `--force` is given.
- `migrate` writes `config.yaml` from the active JSON file, validates the result,
  and renames the old file to `config.json.bak-<timestamp>`.

See `docs/engine-configuration.md`.

## `models`

Print the three model tiers per engine: the default, the allowed set, and the
extra set that needs an explicit opt-in. Read-only and non-interactive.

```text
claive models [NAME] [--json]
```

`allowed` is `engines.<name>.models` when the config has an allowlist, else the
engine's declared catalog. `extra` is what the catalog knows but the policy does
not allow; running one needs `--allow-model MODEL` or an entry in
`engines.<name>.models`. A model the catalog has never heard of is refused the
same way, naming `engines.<name>.extra_models`. See
`docs/engine-configuration.md`.

Exit code: 0 on success, 1 for an unknown engine name.

## `engine config`

Print one engine's effective configuration, or every engine's, with the source of
each value. Read-only and non-interactive: it never launches a worker, never
writes, and never prompts, so a headless host can audit its engine setup.

```text
claive engine config [NAME] [--json]
```

Every engine accepts the same keys in the config file: `binary`, `provider`, `env`,
`prompt_limit`, `default_model`, `default_model_efforts`, `models`,
`disabled_models`, `disabled_model_efforts`, `extra_args`, `providers`,
`enabled` and `reason`. Each value reports `config` when the file sets it and `default` when the
engine's declared default applies. `binary` also reports whether the path is
executable. See `docs/engine-configuration.md`.

Exit code: 0 on success, 1 for an unknown engine name.

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

For Claude budget pauses, exit 3 provides recovery guidance and keeps the question open until
its original deadline. Resume the worker, then wait again with the same text and `--id`.
A handoff is not an answer. The question's ID, reply schema and deadline survive recovery.

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

Send input to an active Pi, Codex or Claude worker with steering enabled, either
by default or through `--enable-steering`.

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

## `handoff`

Ask a running Claude worker with budget mode for a handoff now. claive sends the handoff
template as a budget message. The worker then continues in a new session from its handoff,
or gives its final answer if the work is complete.

```text
claive handoff [--note NOTE] [--wait WAIT] [--json] id
```

| Option | Meaning |
|---|---|
| `--note` | Text the worker must keep in the handoff |
| `--wait` | wait up to SECONDS for a provider acknowledgement. Default: 0 |
| `--json` | Print JSON |

Exit codes: as `steer`. A worker that is not Claude or has no budget mode exits 1.

Example: `claive handoff @backend --note 'Keep the parser findings.' --wait 10`.

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
claive list [--json] [--active] [--limit N]
```

| Option | Meaning |
|---|---|
| `--json` | Print JSON |

Exit codes: 0 on success. Common errors apply.

Example: `claive list --json`.

`--active` includes starting, running, cancelling and idle reusable workers.
`--limit N` selects the newest matching workers after filtering. Without a limit,
the existing active-plus-20-finished view is preserved. `claive status ID` is an
alias for `claive show ID`, including `--json`.

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

Claude workers add a `Context budget:` line: context tokens, warn, stop and compaction limits,
handoffs and missed handoffs, or `off (reason)`. JSON adds `budget`, `context_tokens`, `handoffs`,
`turn_handoffs`, `budget_missed` and `pending_handoff`. Budget recovery also exposes
`pending_budget_context` and a `Pending context:` line naming the saved manifest.
`needs_parent.kind == "budget_recovery"` means a handoff or required input needs attention.
Restore missing artifacts before `answer` or `followup`, or start a new worker with a fresh task.
Steering receipts omit private payloads. Optional `continuation.phase` is `staged` for prompt
inclusion or `entered` after correlated provider replay. Native receipt status does not change.

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
claive usage --all [--since HOURS] [--json]
```

| Option | Meaning |
|---|---|
| `--json` | Print JSON |
| `--all` | Sum every worker with retained usage |
| `--since HOURS` | Only workers active in the last HOURS, such as `12h` or `24h` |

One worker prints its session totals, cache ratio and latest call. `--all` prints the
host totals, the uncached input, the cache ratio, and one row per worker. Direct Pi
sessions outside Claive are not included. Exit codes: 0 on success. `--all` with an
ID, or neither, is refused.

Example: `claive usage @backend --json` or `claive usage --all --since 12h`.

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

A `budget_handoff` event records a Claude context handoff: turn, handoff number, total,
context tokens, new session ID and the first 300 characters of the handoff's Next step.

## `batch`

Manage plans with parallel lanes and sequential stages.

Writer lanes at a Git repository root use separate managed worktrees by default.
Later stages in a lane reuse that lane's worktree, including read-only stages that
inspect earlier edits. Worktrees are retained for review after the batch. Set the
top-level plan field `"shared_workspace": true` to explicitly use shared workspaces.
Non-Git and repository-subdirectory workspaces retain shared behavior with a warning.

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
| `--engine` | muse, pi, claude, codex or agy |
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
