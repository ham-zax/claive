# claive

Harness-neutral coding-worker orchestration: any parent agent (Claude Code, Codex, or another harness that can run shell commands) can drive it. Muse Code is the default production engine and Pi the second engine. The generic worker core handles supervision, reusable turns, state, cancellation, logs, and the live terminal dashboard; Muse-specific command/event/usage behavior lives behind an engine adapter. Maintained source for Hamza's installation in `~/.local/bin`, `~/.codex/skills` and `~/.claude/skills`. Commands: `claive` (worker manager), `claive-worker` (reusable-worker launcher), `claive-orch` (arbiter), and `claive-codex` (opens Codex beside the worker dashboard).

Pi is available explicitly with `--engine pi`, using only `opencode2api`. Its initial model is
`muse-spark-1.3-contributor-free`; new Pi workers inherit the last explicitly selected
Pi model when `--model` is omitted. See [Pi setup, sessions, supported options and verification](docs/pi-engine.md).
Muse remains the default engine. Use `claive-worker --engine pi` to select Pi.

Claude Code is available with `--engine claude` (headless `claude -p`, binary `~/.local/bin/claude` or
`CLAUDE_WORKER_BINARY`). It is strictly locked to `claude-haiku-5-5`: any other `--model`, role model or
timeout fallback is refused. Efforts are `low` to `max` (default `xhigh`), auto-compacting at 100k tokens; `--max-model-steps`,
`--output-schema` and worktree isolation are unsupported. Settings, hooks, MCP servers and skills are not loaded;
read-only workers get `Read,Grep,Glob`.

Codex is available with `--engine codex` (`codex exec --json`, binary from `CODEX_WORKER_BINARY` or the nvm install).
It is strictly locked to `gpt-6-luna` at reasoning effort `max`; any other `--model` or `--reasoning-effort` is refused.
Compaction is left at Codex's own behaviour. Codex chooses its own thread ID,
so the job's session ID is replaced by it once the thread starts. User config and rules are ignored; read-only workers
use the read-only sandbox, writers `workspace-write` with approvals off (`CLAIVE_CODEX_YOLO=1` replaces writers' sandbox with `--dangerously-bypass-approvals-and-sandbox`; read-only workers are unaffected). `--max-model-steps`, `--output-schema` and
worktree isolation are unsupported.

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

The default Muse engine supplies `--workspace`, `--trust-workspace`, `--disable-approval`, and `--json`, and keeps Muse's sandbox enabled. `--read-only` disables both non-shell writes and shell execution. `--worktree` isolates the worker in a linked worktree at `--worktree-base` (default `HEAD`; uncommitted changes are not included). Muse 1.4.3 cannot create one itself (its sandbox makes `.git` read-only before `-w create` runs `git worktree add`), so claive creates it in the worker's state directory (`$CLAIVE_DIR/<id>/worktree`) and passes it as `-w existing`. It lasts for all turns of a reusable worker. Read-only workers get a detached worktree; writers get branch `claive/<id>`. When the worker closes, is cancelled or finishes, claive removes the worktree (and branch) unless a writer left changes (including untracked and ignored files other than tool caches such as `__pycache__` and dependency trees such as `node_modules` or `.venv`) or commits, or git refuses the removal (a locked worktree); then it keeps them, prints the `git worktree remove` command and records `retained_worktree` in `show --json`. A supervisor killed with SIGKILL skips this cleanup. Use `--worktree-existing /absolute/worktree` for a worktree you manage yourself. File ownership must remain disjoint. Run heavy builds and test suites one at a time.

Web tools are disabled by default; `--web` enables them. `--model` accepts only `muse-spark-1.3-contributor`. Other options include `--output-schema` and `--session-id`. `--no-session-log` is available only for deliberate single-turn runs, and is rejected for reusable workers. Run `claive-worker --help` for the full list. `--provider echo` is for transport checks and does not contact a live model; Muse does not accept reasoning effort with that provider, so the launcher omits model and effort options in that case.

## Model, reasoning, and caching

Every live Muse worker uses **`muse-spark-1.3-contributor`**. Default reasoning is **`high`** for normal work; use **`medium`** only for trivial tasks such as commit-message generation, and **`xhigh`/`max`** for complex work. The default step cap is 100; raise it when necessary. No alternate Muse model override or automatic helper fallback is permitted. Echo is an offline transport fixture.

Default effort is `xhigh` for Muse (`max` for very complicated tasks; `high`/`medium` only rarely) and `max` for Pi, which clamps it to each model's highest level. The orchestrator can override effort and step cap on each `followup`, or change future defaults with `effort`. Per-turn overrides do not replace the future default. Changes do not alter an in-flight request. Follow-ups run sequentially with the same workspace/tool policy and Muse session UUID. A `--worktree` worktree is created by claive and lasts for all turns of the worker, so isolated follow-ups keep their files.

The outer supervisor stays alive, while each underlying `muse exec` exits after a turn. Session logging lets the next exec restore history. Related follow-ups should reuse this worker, without arbitrary retirement after a fixed number of tasks. After closure, `claive-worker --session-id UUID` can restore retained history in the same workspace and tool policy; inspect `show JOB_ID --json` for that UUID. A resumed session in a new worker gets a new worktree; to keep the same files, pass the retained worktree with `--worktree-existing`. Keep unrelated lanes separate.

Meta [prompt caching](https://dev.meta.ai/docs/prompt-caching) automatically reuses stable leading tokens on its servers. Closing a terminal does not itself flush the cache, and leaving one idle does not guarantee retention. Put changing task details after stable instructions/history; see the [cookbook](https://dev.meta.ai/docs/cookbook/prompt-caching). Muse exec currently exposes no cache-key or retention flag, so no extra CLI option is needed or invented.

`claive usage JOB_ID [--json]` reads token counters from retained Muse exports. It counts model completions once and excludes duplicated attribution records. It reports usage for the entire retained session, including internal calls; positive cached tokens demonstrate a hit. Missing counters mean unknown. These cumulative accounting numbers are not current context size or an invoice; see [token usage](https://dev.meta.ai/docs/token-counting#usage). Session reuse preserves history, while actual cache benefit must be measured.

## Verified orchestration (`claive-orch`)

`claive-orch` is a deterministic arbiter that sits on top of the worker manager. It implements the depth-first escalation ladder from [Refining Over Resampling](docs/experiment/paper-notes.md) (arXiv 2608.05643), adapted to coding agents. Muse implements. A Pi critic from a different model family reviews the diff and the verifier output. The same Muse session then corrects the work. The verifier decides each round: a better or equal score is checkpointed as a commit on `orch/<run>/<lane>`, and a worse score is reverted. Only after a stall does arm D add one deliberately diverse second lane. The parent (Claude Code or Codex) asks `claive-orch next RUN` and performs exactly the one action it names. Models propose; deterministic code disposes.

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
claive-orch compare --experiment NAME
claive-orch arms
```

Two skills drive it, and both work with either host:

- `skills/worker-orchestration`: everyday verified delegation and parallel fan-out.
- `skills/ttc-experiment`: the controlled refine-vs-resample experiment (arms A, R, R', B, B0, D; repeats; gates).

### Quick start from Claude Code or Codex

After `./install.sh`, start a new session and ask, for example:

```text
Use the worker-orchestration skill. You have my permission to delegate to Muse/Pi workers.
Repo: /abs/repo. Task: <goal>. Verifier: <command that fails now>. Arm D. Don't commit.
```

Where everything lives:

| What | Source (this repo) | Installed |
|---|---|---|
| Arbiter CLI | `bin/claive-orch`, `bin/claivelib/orchestration.py` | `~/.local/bin/claive-orch`, `~/.local/bin/claivelib/` |
| Worker manager CLI | `bin/claive`, `bin/claivelib/` | `~/.local/bin/claive` |
| Skills (both hosts) | `skills/worker-orchestration/`, `skills/ttc-experiment/` | `~/.claude/skills/…` and `~/.codex/skills/…` |
| Claude delegation policy | `skills/claude-subagent-routing/` | `~/.claude/skills/subagent-routing/` |
| Codex delegation policy | `skills/subagent-routing/` | `~/.codex/skills/subagent-routing/` |
| Worker and run state | n/a | `~/.local/state/claive/<worker-id>/`, `…/runs/<run-id>/` |

The paper, design, plan, protocol, environment facts, and implementation mapping are in [`docs/experiment/`](docs/experiment/README.md). Run state lives under `${CLAIVE_DIR:-~/.local/state/claive}/runs/<id>/`.

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
| `skills/subagent-routing/SKILL.md` | Codex delegation and verification guidance |
| `skills/worker-orchestration/SKILL.md` | Host-neutral (Claude Code or Codex) Muse + Pi verified orchestration |
| `skills/ttc-experiment/SKILL.md` | Running the refine-vs-resample experiment |
| `skills/claude-subagent-routing/SKILL.md` | Claude Code's delegation policy, installed as `~/.claude/skills/subagent-routing` |
| `docs/harness-integration.md` | Host-neutral protocol for Claude Code, Codex, Pi agents, and shells |
| `docs/experiment/` | Paper notes, design, plan, protocol, environment, implementation mapping |
| `reference/global-AGENTS.md` | Snapshot of the configured global instructions |
| `reference/agy-relay.sh` | Historical relay; not an active model route |
| `reference/opencode-relay.sh` | Existing incompatible OpenCode relay, for reference |
| `reference/prompt-caching.md` | Meta documentation links and observed CLI behavior |
| `tests/test_workers.py` | Isolated behavioral checks |
| `tests/test_orch.py` | Arbiter ladder, guards, and reporting checks |
| `tests/test_features.py`, `tests/test_async.py` | Worker contract and async (inbox, batch, mission) checks |

Requirements: Linux with Python 3. The default production engine requires Muse at `~/.local/bin/muse`; `MUSE_WORKER_BINARY` can select another absolute Muse executable. The combined terminal view also requires tmux and Codex on PATH. No third-party Python packages are required by the tools or their checks.

Edit this repository, then refresh the installed copies:

```bash
./install.sh --dry-run
./install.sh
./install.sh --pi   # also install host-neutral skills for a Pi parent agent
```

Changed destination files are backed up under `~/.local/state/claive-install/backups`. Identical files are skipped. Symlink and incompatible destination types are rejected. The installer copies the four tools, the `claivelib` package, the routing skill (Codex only), and the `worker-orchestration` and `ttc-experiment` skills into both `${CODEX_HOME:-~/.codex}/skills/` and `${CLAUDE_HOME:-~/.claude}/skills/`. Claude Code's `subagent-routing` is replaced by `skills/claude-subagent-routing` (Muse and Pi through this tooling; Sonnet 5.5 only on request), with the previous copy backed up. The installer it does not overwrite global instructions or install the reference relays. Custom absolute destinations can be supplied through `CLAIVE_BIN_DIR`, `CODEX_HOME`, `CLAUDE_HOME`, and `XDG_STATE_HOME`.

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

Role keys: `engine`, `model`, `reasoning_effort`, `read_only`, `max_model_steps` (ignored by Pi, which has no step cap), `preamble`. Stored workers keep the engine they were launched with.

A host whose `opencode2api` runs locally can route Pi turns over loopback instead of the public URL in `models.json` with `{"providers": {"opencode2api": {"base_url": "http://127.0.0.1:PORT/v1"}}}`. Only `localhost` and loopback addresses are accepted. Pi reads the URL only from `models.json`, so claive gives Pi turns an overlay agent directory (`$CLAIVE_DIR/pi-agent-overlay`: links to every entry of the real one plus a mode-600 `models.json` differing only in `baseUrl`), rebuilt each turn. The real `models.json` is never edited; `doctor` times both URLs, and workers record and print `Provider override:`.

On a host where workers run unattended, `{"workspaces": [{"path": "~/repo/site", "write": false}, {"path": "~/repo/site/drafts", "write": true}]}` limits where workers may run. With the key present, `run`/`start`/`open`/`batch` refuse a `--workspace` or `--worktree-existing` whose real path (symlinks resolved) is outside every entry, and the deepest matching entry decides the mode: `write: false` requires `--read-only` or a read-only role. Without the key any workspace is allowed, as before. This limits the worker's cwd and write mode only; Pi has no OS sandbox, so its read tool can still open absolute paths elsewhere. `doctor` reports the entry counts.

Run checks from the repository root. The suite is hermetic: `tests/hermetic.py` hides the host config, `CLAIVE_*` variables and installed Muse/Pi binaries. `CLAIVE_LIVE_TESTS=1` also runs the checks that drive the real Muse binary.

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
shellcheck install.sh bin/claive-worker bin/claive-codex
```

Checks cover pinned Muse defaults, structured/legacy state compatibility, the engine-neutral fixture lifecycle, reusable follow-ups, dynamic effort, closure/cancellation, cache-usage accounting, subscription-quota notices, terminal-event validation, malformed and failed results, process-group cancellation, concurrent jobs, stale process identities, installer isolation, private state files, and tmux with nondefault pane indices. A real Muse echo check characterizes linked-worktree creation and cleanup when Muse is installed; the tmux check requires tmux. These checks do not test live model authentication or model availability. Test workers and the temporary tmux server are stopped during cleanup.
