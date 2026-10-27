# claive

Harness-neutral coding-worker orchestration for Claude Code, Codex, and shell parents. The worker core handles supervision, reusable turns, state, cancellation, logs, and the terminal dashboard. Engine adapters support Pi, Codex, Claude, and Muse, subject to host policy. Run `claive engines` to see available engines, models, and efforts. This host currently disables Muse and uses Pi for the verifier-driven implementer. Commands: `claive` (worker manager), `claive-worker` (reusable-worker launcher), `claive-orch` (arbiter), and `claive-codex` (Codex with a worker dashboard).

Pi is available explicitly with `--engine pi`, using only `opencode2api`. Its initial model is
`muse-spark-1.3-contributor-free`; new Pi workers inherit the last explicitly selected
Pi model when `--model` is omitted. See [Pi setup, sessions, supported options and verification](docs/pi-engine.md).
The built-in default is Muse when no host configuration selects another engine. This host selects Codex and disables Muse.

Claude Code is available with `--engine claude` (headless `claude -p`, binary `~/.local/bin/claude` or
`CLAUDE_WORKER_BINARY`). It defaults to `claude-haiku-5-5`. The claive config decides which models it may use:
set `engines.claude.models` (for example only `claude-haiku-5-5`) and any other `--model`, role model or
timeout fallback is refused. Efforts are `low` to `max` (default `xhigh`). Context budget mode is on by default:
at a warn and a stop limit claive asks the worker for a handoff and continues in a new session
(see [docs/design-budget-mode.md](docs/design-budget-mode.md)). Claude's own compaction is the
fallback at 105k (Haiku) or 155k (other models) tokens with budget mode off; `--max-model-steps`
is unsupported. `--output-schema /abs/schema.json` is passed as Claude's `--json-schema`, so the
result is the validated JSON object. `--enable-steering` switches to stream-json input for live steering
(see [docs/steering.md](docs/steering.md)). Workers use `--tools default`.
Write turns use `--permission-mode bypassPermissions`. Read-only turns use
`--permission-mode plan`. `--setting-sources ""` keeps the parent's hooks and
permission rules out. MCP servers and skills load. WebSearch and WebFetch stay on.
Claude workers load CLAUDE.md files through `--add-dir` and
`CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD=1`. Claive adds the worker file
`claude/CLAUDE.md` next to `config.json` (default `~/.config/claive/claude/CLAUDE.md`) when it
exists. Then claive adds each directory from the git root down to the workspace. The walk stops
below the home directory. The parent's `~/.claude/CLAUDE.md`, `CLAUDE.local.md` and `@import`
lines do not load.
Live check, 2026-10-15: `ExitPlanMode` is unavailable in `--print` mode.
The model refused writes itself, so that check did not prove a harness-level write block.

Codex is available with `--engine codex` (`codex exec --json`, binary from `CODEX_WORKER_BINARY` or the nvm install).
The default Codex model is `gpt-6.1-terra` at `xhigh`. Terra supports `low`, `medium`, `high`, `xhigh`, `max` and `ultra`. `gpt-6-luna` supports `medium` and `max` and defaults to `max`. `gpt-6.1-sol` supports `low` through `xhigh` and defaults to `medium`. Any other `--model` or `--reasoning-effort` is refused.
Compaction is left at Codex's own behaviour. Codex chooses its own thread ID,
so the job's session ID is replaced by it once the thread starts. Skills load by default.
Workers omit the user's `config.toml` unless `engines.codex.user_config` is `true`
or `CLAIVE_CODEX_USE_USER_CONFIG=1`. This opt-in includes configured MCP servers.
Exec turns retain `--ignore-rules`. Read-only workers
use the read-only sandbox, writers run with `--dangerously-bypass-approvals-and-sandbox` by default (`CLAIVE_CODEX_YOLO=0` restores the `workspace-write` sandbox with approvals off; read-only workers are always sandboxed). `--max-model-steps` and `--output-schema` are unsupported.

Codex workers with `--enable-steering` use a separate App Server transport for
turn-bound in-flight instructions; see [steering.md](docs/steering.md). Ordinary
Codex exec launches use the ordinary transport.
The App Server path links the user's `auth.json`, sessions and skills into a
private `CODEX_HOME`. It reads the repo's `AGENTS.md`.
Every Codex turn sets `web_search="live"`.

Workers use their engine's default tools. Claive supplies no restricted tool list.
`--web` is accepted and has no effect on Claude, Codex and Muse. Pi refuses it.
See the [command reference](docs/command-reference.md) for every subcommand.

## Send a message to a worker

Use `claive send ID --message "..."` to leave a note for a reusable worker's next turn. Add
`--mode wake` to start a turn when the worker is idle, or `--mode now` to steer a supported,
opted-in running turn (it falls back to wake when steering is unavailable). `--message-file`
accepts an absolute file path, `--id` makes a send retry-safe, and `--json` prints the receipt.
Messages arrive as a `<parent-message>` envelope with the message ID and sender (set
`CLAIVE_PARENT` to name your harness). A worker cannot message itself. Peer messages require a parent-opened channel, and
`wake` is refused while the worker waits for `claive answer`.

`claive ask ID --message "..." [--timeout 120] [--schema /abs/reply.schema.json]` asks a question and waits for the answer:
the worker replies with `claive reply MSG_ID --message "..."`, or the final text of that
turn counts. A schema requires a valid JSON reply. A question that has not arrived by the deadline is withdrawn. Exit codes: 0
answered, 124 withdrawn or expired, 3 the worker needs the parent, 1 refused.

`claive peek ID [--steps 5] [--start N] [--json]` shows numbered recent steps of a worker
(text, tool calls, results) without touching it. Parent only. Worker commands also accept a
unique ID prefix of 4 to 11 characters.

## Native Codex progress

The default integration uses Codex's existing background-terminal indicator and `/ps`. The agent runs `claive-worker` through its managed shell tool with a short yield, retains the returned terminal session ID, and collects the final output. The launcher stays alive between related turns until explicitly closed, so Codex can track it while the parent continues working. The worker also appears in the optional registry dashboard.

`claive start` deliberately detaches and immediately returns. Its worker appears in the registry, but the launch command finishes too quickly for Codex to keep tracking it as a background terminal. Use `start` only when detachment is intended. Likewise, do not append `&` or use `nohup` when native terminal visibility is wanted. Reusable workers remain idle between turns and make no model requests while waiting. Single-turn jobs may finish before the first yield.

Keep the managed terminal session ID separate from the printed worker ID. Cancel with `claive cancel WORKER_ID`, then collect the terminal result; do not forcibly close the terminal first.

## Optional worker pane

```bash
claive-codex
```

This opens a separate tmux session with Codex above a five-line worker pane and a status bar that refreshes every second. The pane keeps its height when attaching or resizing the terminal. Codex options are forwarded, for example `claive-codex -C /home/hamza/repo/my-project`. Existing Codex and tmux configuration is preserved.

To monitor workers from another terminal without opening another Codex session:

```bash
claive watch
claive watch --compact
```

The compact pane shows one row per job with its ID, label, workspace, elapsed time, warning count, and current activity. It displays up to three jobs and indicates when more are available with `claive list`. The full watch view also shows model steps observed in events. Both use the live terminal width. Running, idle, completed, failed, interrupted, and cancelled jobs have distinct states. The one-line view looks like:

```text
Workers: 2 running | 1 idle | 3 done | 1 failed | 0 stopped
```

This is an outer terminal status bar; it does not inject a widget into the Codex app or its native status line. It tracks jobs launched through these tools, not arbitrary agent processes. `done` means the CLI completed successfully; the parent must still verify the assigned work. A quiet worker remains running with an increasing last-event age.

## Launch and manage a worker

Put the task in a prompt file with a narrow mission, inputs, scope, file ownership, expected output, and stopping condition. Include repository rules and relevant evidence; shell workers do not inherit the parent conversation.

```bash
claive-worker \
  --workspace /home/hamza/repo/my-project \
  --prompt-file /tmp/worker-task.md \
  --label "Review authentication changes" \
  --read-only \
  --reasoning-effort xhigh \
  --max-model-steps 100
```

`claive-worker` opens a reusable worker. After each turn it prints the outcome and stays idle for a related follow-up, preserving the same Muse session UUID. Keep it idle for a specific related follow-up expected in the active workflow. If no further use is planned, close it when that task chain ends; its retained history can be reopened later. Use `claive run` for a deliberate single-turn job. Inside Codex, keep it in a managed shell session to use native background-terminal visibility. Replace it with `claive start` only to detach intentionally and manage the job through the registry.

```bash
claive list
claive show JOB_ID --json
claive logs JOB_ID
claive logs JOB_ID --stderr
claive wait JOB_ID
claive followup JOB_ID --prompt-file /tmp/next-task.md --reasoning-effort xhigh
claive effort JOB_ID --reasoning-effort max --max-model-steps 200
claive usage JOB_ID
claive close JOB_ID
claive cancel JOB_ID
claive status-line
```

`cancel` stops the worker process group, escalating to a kill after three seconds if necessary. `wait` returns when assigned turns finish (the reusable worker can remain idle), with zero for a valid completed turn, nonzero on failure, or 130 on cancellation. `close` drains already assigned turns before ending the supervisor; `cancel` stops it immediately. Closing a dashboard stops only the view; workers keep running or waiting until explicitly closed or cancelled.

### Named specialists

Give a worker a persistent address so another parent session can return to the
same specialist and retained context. This adapts the addressed-agent idea from
[OpenRig](https://github.com/mvschwarz/openrig) to claive's reusable workers:

```bash
claive alias set backend WORKER_ID
claive alias list --json
claive show @backend --json
claive followup @backend --prompt-file /abs/next-task.md
claive wait @backend
claive mission link MISSION_ID --worker @backend
```

`@name` works wherever a worker ID is accepted: `show`, `logs`, `usage`, `wait`,
`followup`, `effort`, `answer`, `close`, `cancel`, and `mission link --worker`.
Names start with a lowercase letter and contain up to 64 lowercase letters,
digits, `_`, `.`, or `-`. Bindings live in `$CLAIVE_DIR/aliases.json`, shared by
parents using that state directory; labels remain display text. Command output,
inbox events, and mission links keep the canonical worker ID.

Binding an existing name to a different worker requires
`claive alias set backend NEW_WORKER_ID --replace`. Removing a name with
`claive alias remove backend` keeps the worker and its history. A closed worker
remains inspectable through its name, but follow-ups still require an available
reusable worker. To resume a closed specialist, reopen its retained `--session-id`
with the same workspace and policy, then explicitly rebind the name. Listing
shows `missing` if a bound worker's state has been removed. An invalid entry in
`aliases.json` (for example after a manual edit) is ignored with a warning rather than
breaking other names; the next `alias set` or `alias remove` rewrites the registry
without it.

### Conversations across engines and models

Existing reusable workers can exchange replies in a bounded conversation:

```bash
claive conversation start @proposer @critic @reviewer \
  --message-file /abs/question.md --rounds 2 --timeout 300
claive conversation show CONVERSATION_ID --json
```

Each round visits participants in the supplied order and passes the previous
participant's reply to the next. For example, open one Pi worker on
`mimo-v2.6-flash-free`, another on `big-pickle`, and a Claude worker on
`claude-haiku-5-5`, then give them the names above. Muse and Codex workers also
use the same protocol. Open participants with their own role prompts and
`--report` (or a role); use `--read-only` for discussion and review. Their
engine, model, session, effort defaults, permissions, and file ownership remain
the policies chosen at launch. Peer replies do not grant new permissions.

A worker can belong to only one running conversation at a time (the claim is
released automatically if the controller dies). With `CLAIVE_MISSION` set, the
conversation is linked to that mission and appears in `claive mission show`.
Workers must be idle, reusable, distinct, and have no queued work or unresolved
failure/question when the conversation starts. A conversation never opens
workers or selects models. Workers opened before conversation support was
installed must be closed and reopened with their retained `--session-id` first;
their old supervisors cannot produce request receipts. Aliases resolve once at startup. Each reply has a
request ID, sender, engine/model, session, turn number, and saved receipt, so an
unrelated follow-up cannot substitute its result. `--rounds` is 1..8 (default
2), with 2..8 participants; `--timeout` is a finite total scheduling/wait budget
in seconds (default 300). Messages and composed prompts are capped at 128 KiB.

The controller runs in the foreground; use the parent's managed shell with a
short yield for visibility. It stops scheduling on failure, a required report
that is missing/invalid, a parent question (exit 3), timeout (124), or a stop
signal (130). A timeout or stop signal can leave a request already claimed;
the transcript's `pending_request.response_file` identifies its eventual result.
Requests still queued expire or observe the controller's stop marker before
delivery. Inspect that receipt
before sending again. To stop a worker's current turn, use `claive cancel ID`.
Answer a parked question with `claive answer ID`, then start a new bounded
conversation when participants are ready. The command does not restart or retry
conversations automatically.

Transcripts live in `$CLAIVE_DIR/conversations/<id>/state.json`, and worker
receipts in `$CLAIVE_DIR/<worker>/responses/<request-id>.json`. Completion and
questions also appear in `claive inbox`. `conversation start --json` returns
one JSON object at completion; `conversation show --json` can inspect progress
or a retained transcript, including an interrupted controller.

### Health, roles, reports, and parent questions

```bash
claive doctor
claive doctor --json
claive doctor --live   # also one tiny read-only Pi turn on Pi's shared default model, timed
claive run --role reviewer --workspace /abs/repo --prompt-file /abs/task.md
claive run --report --workspace /abs/repo --prompt-file /abs/task.md
claive answer JOB_ID --message "Use design B."
```

`doctor` is a read-only health check (exit 0 when all required checks pass); it launches a model only with `--live`. It runs `pi --version`, checks that every model claive may request from Pi is listed under `opencode2api` in Pi's `models.json` (`pi_provider`), and times `GET /models` on the provider. The default engine's binary and, when Pi is the default, `pi_provider` are required. `--role scout|worker|reviewer|oracle` selects engine, model, effort, read-only, and step-cap defaults (explicit flags win) and always enables the report contract; `--report` enables the contract without a role. The worker ends its answer with a `claive-report` JSON block (`done|blocked|needs_decision`); `run`/`wait` exit 3 when it asks for a decision, the dashboard shows `ASK`, and `status-line` counts `N need you`. `answer` queues a parent decision to a reusable worker waiting for one. Failed turns record `failure_kind` (`quota|protocol|worker|launch|supervisor|rejected|interrupted`), shown by `show`/`wait`/`run` and in `--json` output.

### Async workers, inbox, batches, and missions (any harness)

```bash
claive open --detach --role worker --workspace /abs/repo --prompt-file /abs/task.md
claive wait ID1 ID2 --any --timeout 300      # first to settle; 124 = still running
claive inbox --consumer codex                 # finished turns since this consumer's last read
claive batch start /abs/plan.json && claive batch wait BATCH_ID
claive mission new --title "Parser" --goal-file /abs/goal.md
claive mission show MISSION_ID                # links, live status, and a Next: line
```

These work the same from Claude Code, Codex, a Pi agent, or a shell: plain commands, `--json` output, exit codes `0` ok, `1` failed, `3` needs parent, `124` timed out, `130` cancelled, and all state under `$CLAIVE_DIR`. `CLAIVE_NOTIFY_CMD` optionally runs a command per event (event JSON on stdin). Workers get `CLAIVE_WORKER_ID` and cannot launch workers. Protocol and host notes: [docs/harness-integration.md](docs/harness-integration.md); spec: [docs/superpowers/specs/2026-10-06-async-workers.md](docs/superpowers/specs/2026-10-06-async-workers.md).

### Unattended goal queue (`claive serve`)

```bash
claive goal add --title "Audit deps" --prompt-file /abs/goal.md --workspace /abs/repo [--write] [--budget 2h] [--role reviewer]
claive goal list [--all]                      # status, attempts, budget used, worker, open questions
claive goal answer GOAL_ID --message "Use B"  # resume a parked goal in the same session
claive goal cancel|retry|show GOAL_ID
claive serve                                  # foreground loop for systemd; --once runs one pass (launch or harvest) and exits
claive serve --stop | --resume | --status     # stop switch, clear it, show state
```

On a server there is no parent session to launch, wait for, and answer workers, so `claive serve` plays that role for a durable queue in `$CLAIVE_DIR/goals/`. Each goal runs as an ordinary reusable worker (`claive open --detach --report`) on any engine, so `claive show/logs/wait` work on it; goals are read-only unless added with `--write` and must pass the `workspaces` allowlist both when added and at launch. One goal runs at a time by default (`--max-parallel`). A worker question parks the goal, records the question, posts a `goal` event to `claive inbox`, and the queue moves on; `claive goal answer` resumes the same worker, or the same session in a new worker if that worker was stopped. A goal past its wall-clock budget (default 2h; time parked does not count) is cancelled and marked `timed_out`. Quota, protocol and launch failures pause all launches with exponential backoff (30 s doubling to 30 min); a failed goal retries after a growing delay up to `--max-attempts` (default 3); launches refused by config or the allowlist fail at once. The stop switch `$CLAIVE_DIR/supervisor/stop` (set by `claive serve --stop`) is checked between turns: serve cancels its workers, requeues running goals, and exits 0, and nothing launches until `--resume`. SIGTERM leaves workers running and the next serve adopts or harvests them. Logs: `$CLAIVE_DIR/supervisor/serve.log` and stdout. A lock allows one serve per state directory. [systemd/claive-serve.service](systemd/claive-serve.service) is a user unit (`KillMode=process`, `Restart=on-failure`, explicit `PATH` and `PI_WORKER_BINARY`) with install steps in its header.

Every engine accepts the opt-in `--worktree` flag. Claive creates the worktree at `$CLAIVE_DIR/<id>/worktree` from `--worktree-base` (default `HEAD`) and warns about uncommitted source paths. Read-only workers use a detached worktree; writers use branch `claive/<id>`. The optional `--worktree-setup CMD` runs once under the shared heavy lock. Claive keeps changed worktrees, including ignored dependency directories, and worktrees used by active workers or awaiting parent acceptance. It appends lifecycle events to `worktrees.jsonl`. Use `claive worktree list` and `claive worktree clean --dry-run` to inspect cleanup candidates. `clean` also skips missing job state; `--discard-changed` permits removal of changed candidates. Use `--worktree-existing /absolute/worktree` for a worktree you manage yourself. Serialize heavy jobs with `claive-memcap 1500M --lock heavy -- COMMAND`.

When enabled, the Muse adapter supplies `--workspace`, `--trust-workspace` and `--json`, and keeps its sandbox enabled. Write turns also pass `--disable-approval`. Read-only turns use `--permission-profile :read-only --disable-write`, keep the shell on, and omit `--disable-approval`. Claive passes `--max-model-steps` only when a flag or role sets a cap.

Web tools stay on. `--web` is accepted and has no effect on Muse. `--model` accepts only `muse-spark-1.3-contributor`. Other options include `--output-schema` and `--session-id`. `--no-session-log` is available only for deliberate single-turn runs, and is rejected for reusable workers. Run `claive-worker --help` for the full list. `--provider echo` is for transport checks and does not contact a live model; Muse does not accept reasoning effort with that provider, so the launcher omits model and effort options in that case.

## Model, reasoning, and caching

Every live Muse worker uses **`muse-spark-1.3-contributor`**. Default reasoning is **`xhigh`**. Use **`max`** for very complicated tasks and **`high`/`medium`** only when suitable. Muse has no default step cap. Set `--max-model-steps` only when a task needs a cap. No alternate Muse model override or automatic helper fallback is permitted. Echo is an offline transport fixture.

Default effort is `xhigh` for Muse (`max` for very complicated tasks; `high`/`medium` only rarely) and `max` for Pi, which clamps it to each model's highest level. The orchestrator can override effort and step cap on each `followup`, or change future defaults with `effort`. Per-turn overrides do not replace the future default. Changes do not alter an in-flight request. Follow-ups run sequentially with the same workspace/tool policy and Muse session UUID. A `--worktree` worktree is created by claive and lasts for all turns of the worker, so isolated follow-ups keep their files.

The outer supervisor stays alive, while each underlying `muse exec` exits after a turn. Session logging lets the next exec restore history. Related follow-ups should reuse this worker, without arbitrary retirement after a fixed number of tasks. After closure, `claive-worker --session-id UUID` can restore retained history in the same workspace and tool policy; inspect `show JOB_ID --json` for that UUID. A resumed session in a new worker gets a new worktree; to keep the same files, pass the retained worktree with `--worktree-existing`. Keep unrelated lanes separate.

Meta [prompt caching](https://dev.meta.ai/docs/prompt-caching) automatically reuses stable leading tokens on its servers. Closing a terminal does not itself flush the cache, and leaving one idle does not guarantee retention. Put changing task details after stable instructions/history; see the [cookbook](https://dev.meta.ai/docs/cookbook/prompt-caching). Muse exec currently exposes no cache-key or retention flag, so no extra CLI option is needed or invented.

`claive usage JOB_ID [--json]` reads token counters from retained Muse exports. It counts model completions once and excludes duplicated attribution records. It reports usage for the entire retained session, including internal calls; positive cached tokens demonstrate a hit. Missing counters mean unknown. These cumulative accounting numbers are not current context size or an invoice; see [token usage](https://dev.meta.ai/docs/token-counting#usage). Session reuse preserves history, while actual cache benefit must be measured.

## Verified orchestration (`claive-orch`)

`claive-orch` is a deterministic arbiter for verifier-gated coding work. On this host, Pi implements because Muse is disabled. A Pi critic from another model family reviews the diff and verifier output. The implementer then corrects the work. The verifier decides each round: a better or equal score is checkpointed on `orch/<run>/<lane>`, and a worse score is reverted. Only after a stall does arm D add a different second lane. The parent asks `claive-orch next RUN` and performs the action it names.

```bash
claive-orch init --repo /abs/repo --task-file /abs/task.md --verify 'python3 -m pytest -q' --arm D
claive-orch next RUN          # NEXT / Why / How
claive-orch report RUN        # outcome, score trajectory, critics, tokens (auto-collected)
claive-orch integrate RUN     # apply the winner unstaged, re-verify in the checkout
claive-orch integrate RUN --paths src --exclude src/gen   # apply part of the diff
claive-orch rescore RUN --reason 'fixed held-out check'  # re-verify lanes without using a round
claive-orch reject RUN a --defect 2 --reason 'relaxes a gate'  # drop a wrong critic suggestion
claive-orch cleanup RUN --branches   # remove worktrees and lane branches
claive-orch prune --repo /abs/repo   # list stale orch/* branches (--apply deletes)
claive-memcap 2G -- npm test        # memory cap without systemd-run (init --verify-memory 2G uses it)
claive-orch arms
```

The full step-by-step protocol (step table, arbiter rules, integration, quota fallback) is in [docs/orchestration-ladder.md](docs/orchestration-ladder.md); the skill carries only the summary.

The Skillshare source (`~/.config/skillshare/skills/`) is the canonical copy of the active skills, and every agent reads it through Skillshare links. This repository keeps a tracked mirror:

- `skills/claive-orchestration`: verified Muse/Pi runs and general delegation for Claude Code, Codex, and shell parents.
- `skills/claive-routing-codex`: Codex-specific managed-terminal worker launch and recovery.
- TTC experiment: deferred and removed from the active repository for now; see [`docs/experiment/README.md`](docs/experiment/README.md).

Claude Code uses `claive-orchestration` and its global CLAUDE.md delegation policy; the separate Claude routing skill was removed.

### Quick start from Claude Code or Codex

After `./install.sh`, start a new session and ask, for example:

```text
Use the claive-orchestration skill. You have my permission to delegate to Muse/Pi workers.
Repo: /abs/repo. Task: <goal>. Verifier: <command that fails now>. Arm D. Don't commit.
```

Where everything lives:

| What | Source (this repo) | Installed |
|---|---|---|
| Arbiter CLI | `bin/claive-orch`, `bin/claivelib/orchestration.py` | `~/.local/bin/claive-orch`, `~/.local/bin/claivelib/` |
| Worker manager CLI | `bin/claive`, `bin/claivelib/` | `~/.local/bin/claive` |
| Shared orchestration skill | `skills/claive-orchestration/` | Skillshare source `~/.config/skillshare/skills/claive-orchestration/` |
| Codex routing skill | `skills/claive-routing-codex/` | Skillshare source `~/.config/skillshare/skills/claive-routing-codex/` |
| Deferred TTC experiment | `docs/experiment/README.md` | Repository note only; no active skill is installed |
| Worker and run state | n/a | `~/.local/state/claive/<worker-id>/`, `…/runs/<run-id>/` |

The TTC experiment is deferred; its repository note is [`docs/experiment/README.md`](docs/experiment/README.md). Run state lives under `${CLAIVE_DIR:-~/.local/state/claive}/runs/<id>/`.

## Five-hour quota

On an explicit Muse subscription-quota exhaustion, the manager records the failure and reported reset time and prints a notice naming the pre-approved fallback, Pi `muse-spark-1.3-contributor-free` at `max`; any other fallback needs Hamza's approval. The orchestrator stops sending work to that exhausted quota and switches the affected lane to that fallback, telling Hamza. A generic 429 alone is not proof of the five-hour limit. Nothing in the manager switches providers or retries automatically. The orchestrator (not the manager) makes the switch; the fallback applies to the affected assignment and does not change Muse's defaults. OpenCode's historical relay remains unavailable until repaired and verified.

## Files and installation

| Path | Purpose |
| --- | --- |
| `bin/claive` | Thin Python launcher for the worker manager |
| `bin/claivelib/` | Generic worker core, compatibility layer, and engine adapters |
| `bin/claive-worker` | Reusable worker launcher in a managed terminal |
| `bin/claive-codex` | Codex with the tmux worker view |
| `bin/claive-orch` | Launcher for the orchestration arbiter (`claivelib/orchestration.py`) |
| `skills/claive-routing-codex/SKILL.md` | Codex delegation and verification guidance |
| `skills/claive-orchestration/SKILL.md` | Shared worker delegation and Muse/Pi verifier-driven orchestration |
| `docs/experiment/README.md` | Deferred TTC experiment note; no experiment materials are active |
| `docs/harness-integration.md` | Host-neutral protocol for Claude Code, Codex, Pi agents, and shells |
| `reference/global-AGENTS.md` | Snapshot of the configured global instructions |
| `reference/agy-relay.sh` | Historical relay; not an active model route |
| `reference/opencode-relay.sh` | Existing incompatible OpenCode relay, for reference |
| `reference/prompt-caching.md` | Meta documentation links and observed CLI behavior |
| `tests/test_workers.py` | Isolated behavioral checks |
| `tests/test_orch.py` | Arbiter ladder, guards, and reporting checks |
| `tests/test_features.py`, `tests/test_async.py` | Worker contract and async (inbox, batch, mission) checks |

Requirements: Linux with Python 3 and the binary for each enabled engine. `MUSE_WORKER_BINARY` can select a Muse executable when Muse is enabled. The combined terminal view also requires tmux and Codex on PATH. No third-party Python packages are required by the tools or their checks.

Edit the two active `SKILL.md` files in the Skillshare source, run `skillshare sync`, then copy them into `skills/` here so the mirror matches (`install.sh` without `--runtime-only` fails while they differ). For CLI binaries, use:

```bash
./install.sh --dry-run
./install.sh
./install.sh --pi   # also install host-neutral skills for a Pi parent agent
./install.sh --runtime-only   # only package, launchers, and provenance; skips every skill check/write
```

Changed CLI destination files are backed up under `~/.local/state/claive-install/backups`. The installer refreshes CLI tools and `claivelib`, records source revision/path/content fingerprints in `claivelib/provenance.json` (`bin/claivelib/runtime.py` exposes read-only source-to-installed drift and smoke checks for `claive doctor` integration), installs `claive-orchestration` for Claude Code/Codex (and optionally Pi), and installs `claive-routing-codex` for Codex only. It does not install the removed Claude routing or deferred TTC experiment. Skillshare-managed symlinks are left intact if their content matches the repo source; divergent or broken symlinks are refused with an instruction to sync Skillshare. `--runtime-only` (combinable with `--dry-run`) syncs only the package, the five launchers, and the provenance record while skipping every skill check and write, so a divergent Skillshare-managed skill link never blocks a runtime repair and skill destinations are left untouched. Existing user-installed skill directories are preserved; remove obsolete Claive-owned entries through the Skillshare migration when you are ready. In this machine's setup Skillshare, not this installer, owns skill distribution. The installer does not overwrite global instructions or install reference relays. Custom destinations can be set with `CLAIVE_BIN_DIR`, `CODEX_HOME`, `CLAUDE_HOME`, and `XDG_STATE_HOME`.

Hamza's Muse settings (`~/.config/muse/settings.json`) also select `muse-spark-1.3-contributor` with `high` reasoning. The installer preserves those settings; the launcher pins its own model and effort independently.

Hamza's global `~/.codex/AGENTS.md` already points to the installed routing skill and tracked launchers. The skill uses Hamza's absolute paths; adapt those instructions when installing for another user. The global snapshot is reference material, not a replacement for another installation's policies.

Both relay copies are historical and are excluded from the active Muse-only model policy. The OpenCode relay is also incompatible: installed OpenCode 2.0.21 rejects its `--dir` and `--variant` flags. agy uses automatic approval and has no built-in worktree isolation. Neither relay is automatically tracked by the Muse dashboard.

## State and verification

Job metadata, JSONL events, diagnostic output, and final answers are retained under `${XDG_STATE_HOME:-$HOME/.local/state}/claive`, with private directories and files. Logs can contain task prompts and source excerpts; keep this state outside the repository. Set `CLAIVE_DIR` to an absolute path to use a different registry. Finished jobs remain available for inspection; the dashboard shows all active jobs and up to 20 recent results.

### Per-host engine config

Hosts without Muse (for example a Pi-only server) set the default engine and role overrides in `${CLAIVE_CONFIG:-${XDG_CONFIG_HOME:-~/.config}/claive/config.json}` instead of editing code. Without the file, behaviour is the built-in default (Muse, with Pi for scout/reviewer/oracle). `CLAIVE_ENGINE` overrides `default_engine` for one shell; `--engine` and role engines still win over the default. `claive doctor` reports the config in use, and an invalid config refuses every launch.

```json
{"default_engine": "pi",
 "roles": {"worker": {"engine": "pi", "model": "muse-spark-1.3-contributor-free"}}}
```

Role keys: `engine`, `model`, `reasoning_effort`, `read_only`,
`max_model_steps`, and either `preamble` or `prompt_file`. A prompt file must be absolute.
A new role requires `engine`, `read_only`, and `preamble` or `prompt_file`.
Built-in overrides inherit omitted values. Explicit launch flags win.
All built-in roles default to `max_model_steps: null`.
A built-in role cap applies only when its selected engine supports caps.
Pi refuses explicit caps and custom-role caps.
Stored workers keep their launch engine.

Teams live under `teams.<name>`. Each team has `members`, mapping member names to
roles, and optional absolute `collaboration` and `project` files.
`--team NAME --member NAME` selects the member's role and cannot use `--role`.
`--project-file /abs/project.md` replaces that team's project file for one launch.
A launch binds `@TEAM-MEMBER`. An active binding prevents another launch.
`claive team list` and `claive team show NAME` display configuration and aliases.
They do not start workers.

First-prompt order: role, team header, collaboration, project, context files,
task, then report contract. `--context-file` repeats up to ten times, in order.
Each layer file must be nonempty UTF-8 text, absolute, and at most 256 KiB.
Duplicate context files are refused. Follow-ups do not reload those layers.
See [the command reference](docs/command-reference.md) for launch options.

Claive reads config again when a command needs policy. It does not cache config across commands.
The same file holds the user's per-engine policy under `engines`. claive enforces it in code on every launch, follow-up, effort change, model fallback and batch stage, including workers that were already open when the file changed:

```json
{"engines": {
  "claude": {"enabled": false, "reason": "subscription quota is for interactive use"},
  "pi": {"default_model": "big-pickle", "default_reasoning_effort": "high",
         "disabled_reasoning_efforts": ["max"],
         "disabled_models": {"space-bunny-free": "too slow"}}}}
```

`"enabled": false` refuses the engine and tells the caller it is `explicitly disabled by the user`, with the optional `reason`, and not to retry or work around it. `default_model` and `default_reasoning_effort` apply when neither a flag nor a role chooses; the engine still validates them, so they cannot unlock a model an engine is locked against. `disabled_models` and `disabled_reasoning_efforts` are a list of names or an object mapping each name to a reason. A misspelt engine name or key fails every launch instead of silently disabling nothing. `claive engines [--json]` prints the effective policy, defaults and steering support per engine, and `doctor` reports disabled engines.

`models` is an allowlist of models, each with the reasoning efforts it may use. With the key present, a model that is not listed is refused, and so is an effort outside its `efforts` (`{}` allows every effort the engine supports). A turn must then name its model, through `--model`, a role or `default_model` (which must be listed). The allowlist can only narrow the limits in the engine code: a model or effort the engine does not support makes the config invalid, which refuses every launch. Orchestration picks skip models that are not listed. `claive engines` shows the effective models and efforts. For example, Luna only at `max`, Sol below `max`, and Claude at `high` or `xhigh`:

```json
{"engines": {
  "codex": {"models": {"gpt-6-luna": {"efforts": ["max"]},
                       "gpt-6.1-sol": {"efforts": ["low", "medium", "high", "xhigh"]}}},
  "claude": {"models": {"claude-haiku-5-5": {"efforts": ["high", "xhigh"]}}}}}
```

`engines.codex.user_config: true` loads the user's `config.toml`.
`CLAIVE_CODEX_USE_USER_CONFIG=1` also enables it. Skills load without this opt-in.
The exec transport still passes `--ignore-rules`.

`engines.<name>.extra_args` is an opt-in list of nonempty strings.
Claive adds it before the engine command's `--`, or at the end when no `--` exists.
It refuses `--` and flags in `OWNED_FLAGS` in `bin/claivelib/config.py`,
including `--flag=value`. Those sets reserve output, model and session controls.
Codex steering and fork turns refuse nonempty `engines.codex.extra_args`.
For example, a user can select Claude tools explicitly:

```json
{"engines": {"claude": {"extra_args": ["--tools", "Read,Grep,Glob,Bash"]}}}
```

A host whose `opencode2api` runs locally can route Pi turns over loopback instead of the public URL in `models.json` with `{"providers": {"opencode2api": {"base_url": "http://127.0.0.1:PORT/v1"}}}`. Only `localhost` and loopback addresses are accepted. Pi reads the URL only from `models.json`, so claive gives Pi turns an overlay agent directory (`$CLAIVE_DIR/pi-agent-overlay`: links to every entry of the real one plus a mode-600 `models.json` differing only in `baseUrl`), rebuilt each turn. The real `models.json` is never edited; `doctor` times both URLs, and workers record and print `Provider override:`.

On a host where workers run unattended, `{"workspaces": [{"path": "~/repo/site", "write": false}, {"path": "~/repo/site/drafts", "write": true}]}` limits where workers may run. With the key present, `run`/`start`/`open`/`batch` refuse a `--workspace` or `--worktree-existing` whose real path (symlinks resolved) is outside every entry, and the deepest matching entry decides the mode: `write: false` requires `--read-only` or a read-only role. Without the key any workspace is allowed, as before. This limits the worker's cwd and requested write mode. Pi has no OS sandbox. Its read-only instruction does not enforce write protection, and tools can access absolute paths elsewhere. `doctor` reports the entry counts.

Run checks from the repository root. The suite is hermetic: `tests/hermetic.py` hides the host config, `CLAIVE_*` variables and installed Muse/Pi binaries. `CLAIVE_LIVE_TESTS=1` also runs the checks that drive the real Muse binary.

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
shellcheck install.sh bin/claive-worker bin/claive-codex
```

`just check` runs both; `just test-one tests/test_x.py` runs single files (no `PYTHONPATH` needed).
CI (`.github/workflows/test.yml`) runs the same checks on every push to main and every pull request.

Checks cover pinned Muse defaults, structured/legacy state compatibility, the engine-neutral fixture lifecycle, reusable follow-ups, dynamic effort, closure/cancellation, cache-usage accounting, subscription-quota notices, terminal-event validation, malformed and failed results, process-group cancellation, concurrent jobs, stale process identities, installer isolation, private state files, and tmux with nondefault pane indices. A real Muse echo check characterizes linked-worktree creation and cleanup when Muse is installed; the tmux check requires tmux. These checks do not test live model authentication or model availability. Test workers and the temporary tmux server are stopped during cleanup.
