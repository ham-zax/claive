# Codex Muse Workers

Reusable Muse Code subagents for Codex, with worker routing, private job tracking, and a live terminal dashboard. Maintained source for Hamza's installation in `~/.local/bin` and `~/.codex/skills/subagent-routing`.

## Native Codex progress

The default integration uses Codex's existing background-terminal indicator and `/ps`. The agent runs `muse-worker` through its managed shell tool with a short yield, retains the returned terminal session ID, and collects the final output. The launcher stays alive between related turns until explicitly closed, so Codex can track it while the parent continues working. The worker also appears in the optional registry dashboard.

`codex-workers start` deliberately detaches and immediately returns. Its worker appears in the registry, but the launch command finishes too quickly for Codex to keep tracking it as a background terminal. Use `start` only when detachment is intended. Likewise, do not append `&` or use `nohup` when native terminal visibility is wanted. Reusable workers remain idle between turns and make no model requests while waiting. Single-turn jobs may finish before the first yield.

Keep the managed terminal session ID separate from the printed worker ID. Cancel with `codex-workers cancel WORKER_ID`, then collect the terminal result; do not forcibly close the terminal first.

## Optional worker pane

```bash
codex-with-workers
```

This opens a separate tmux session with Codex above a five-line worker pane and a status bar that refreshes every second. The pane keeps its height when attaching or resizing the terminal. Codex options are forwarded, for example `codex-with-workers -C /home/hamza/repo/my-project`. Existing Codex and tmux configuration is preserved.

To monitor workers from another terminal without opening another Codex session:

```bash
codex-workers watch
codex-workers watch --compact
```

The compact pane shows one row per job with its ID, label, workspace, elapsed time, warning count, and current activity. It displays up to three jobs and indicates when more are available with `codex-workers list`. The full watch view also shows model steps observed in events. Both use the live terminal width. Running, idle, completed, failed, interrupted, and cancelled jobs have distinct states. The one-line view looks like:

```text
Muse: 2 running | 1 idle | 3 done | 1 failed | 0 stopped
```

This is an outer terminal status bar; it does not inject a widget into the Codex app or its native status line. It tracks jobs launched through these tools, not arbitrary agent processes. `done` means the CLI completed successfully; the parent must still verify the assigned work. A quiet worker remains running with an increasing last-event age.

## Launch and manage a worker

Put the task in a prompt file with a narrow mission, inputs, scope, file ownership, expected output, and stopping condition. Include repository rules and relevant evidence; shell workers do not inherit the parent conversation.

```bash
muse-worker \
  --workspace /home/hamza/repo/my-project \
  --prompt-file /tmp/worker-task.md \
  --label "Review authentication changes" \
  --read-only \
  --reasoning-effort high \
  --max-model-steps 100
```

`muse-worker` opens a reusable worker. After each turn it prints the outcome and stays idle for a related follow-up, preserving the same Muse session UUID. Keep it idle for a specific related follow-up expected in the active workflow. If no further use is planned, close it when that task chain ends; its retained history can be reopened later. Use `codex-workers run` for a deliberate single-turn job. Inside Codex, keep it in a managed shell session to use native background-terminal visibility. Replace it with `codex-workers start` only to detach intentionally and manage the job through the registry.

```bash
codex-workers list
codex-workers show JOB_ID --json
codex-workers logs JOB_ID
codex-workers logs JOB_ID --stderr
codex-workers wait JOB_ID
codex-workers followup JOB_ID --prompt-file /tmp/next-task.md --reasoning-effort xhigh
codex-workers effort JOB_ID --reasoning-effort max --max-model-steps 200
codex-workers usage JOB_ID
codex-workers close JOB_ID
codex-workers cancel JOB_ID
codex-workers status-line
```

`cancel` stops the worker process group, escalating to a kill after three seconds if necessary. `wait` returns when assigned turns finish (the reusable worker can remain idle), with zero for a valid completed turn, nonzero on failure, or 130 on cancellation. `close` drains already assigned turns before ending the supervisor; `cancel` stops it immediately. Closing a dashboard stops only the view; workers keep running or waiting until explicitly closed or cancelled.

The launcher always supplies `--workspace`, `--trust-workspace`, `--disable-approval`, and `--json`. It keeps the Muse sandbox enabled. `--read-only` disables both non-shell writes and shell execution. Add `--worktree` for each concurrent writer; it maps to `-w create`. Worktree creation defaults to `HEAD`, so transfer any required uncommitted changes explicitly. Use `--worktree-base` to choose another base. Use `--worktree-existing /absolute/worktree` when reopening an existing isolated lane; its path is recorded as `actual_workspace`. File ownership must remain disjoint. Run heavy builds and test suites one at a time.

Web tools are disabled by default; `--web` enables them. `--model` accepts only `muse-spark-1.3-contributor`. Other options include `--output-schema` and `--session-id`. `--no-session-log` is available only for deliberate single-turn runs, and is rejected for reusable workers. Run `muse-worker --help` for the full list. `--provider echo` is for transport checks and does not contact a live model; Muse does not accept reasoning effort with that provider, so the launcher omits model and effort options in that case.

## Model, reasoning, and caching

Every live Muse worker uses **`muse-spark-1.3-contributor`**. Default reasoning is **`high`** for normal work; use **`medium`** only for trivial tasks such as commit-message generation, and **`xhigh`/`max`** for complex work. The default step cap is 100; raise it when necessary. No alternate Muse model override or automatic helper fallback is permitted. Echo is an offline transport fixture.

The orchestrator can override effort and step cap on each `followup`, or change future defaults with `effort`. Per-turn overrides do not replace the future default. Changes do not alter an in-flight request. Follow-ups run sequentially with the same workspace/tool policy and Muse session UUID; an isolated worktree is reused after the first turn.

The outer supervisor stays alive, while each underlying `muse exec` exits after a turn. Session logging lets the next exec restore history. Related follow-ups should reuse this worker, without arbitrary retirement after a fixed number of tasks. After closure, `muse-worker --session-id UUID` can reopen the same lane with the same workspace, worktree, and policy; inspect `show JOB_ID --json` for that UUID. Keep unrelated lanes separate.

Meta [prompt caching](https://dev.meta.ai/docs/prompt-caching) automatically reuses stable leading tokens on its servers. Closing a terminal does not itself flush the cache, and leaving one idle does not guarantee retention. Put changing task details after stable instructions/history; see the [cookbook](https://dev.meta.ai/docs/cookbook/prompt-caching). Muse exec currently exposes no cache-key or retention flag, so no extra CLI option is needed or invented.

`codex-workers usage JOB_ID [--json]` reads token counters from retained Muse exports. It counts model completions once and excludes duplicated attribution records. It reports usage for the entire retained session, including internal calls; positive cached tokens demonstrate a hit. Missing counters mean unknown. These cumulative accounting numbers are not current context size or an invoice; see [token usage](https://dev.meta.ai/docs/token-counting#usage). Session reuse preserves history, while actual cache benefit must be measured.

## Five-hour quota

On an explicit Muse subscription-quota exhaustion, the manager records the failure and reported reset time and prints a notice to ask Hamza whether to wait or approve a specific available fallback subagent/model. The orchestrator stops sending work to that exhausted quota and continues independent authorized work while awaiting the choice. A generic 429 alone is not proof of the five-hour limit. Nothing in the manager switches providers or retries automatically. A confirmed fallback applies to the affected assignment and does not change Muse's defaults. OpenCode's historical relay remains unavailable until repaired and verified.

## Files and installation

| Path | Purpose |
| --- | --- |
| `bin/codex-workers` | Python standard-library job manager and dashboard |
| `bin/muse-worker` | Reusable worker launcher in a managed terminal |
| `bin/codex-with-workers` | Codex with the tmux worker view |
| `skills/subagent-routing/SKILL.md` | Codex delegation and verification guidance |
| `reference/global-AGENTS.md` | Snapshot of the configured global instructions |
| `reference/agy-relay.sh` | Historical relay; not an active model route |
| `reference/opencode-relay.sh` | Existing incompatible OpenCode relay, for reference |
| `reference/prompt-caching.md` | Meta documentation links and observed CLI behavior |
| `tests/test_workers.py` | Isolated behavioral checks |

Requirements: Linux with Python 3 and Muse installed at `~/.local/bin/muse`. The combined terminal view also requires tmux and Codex on PATH. No Python packages are required by the tools or their checks. `MUSE_WORKER_BINARY` can select another absolute Muse executable.

Edit this repository, then refresh the installed copies:

```bash
./install.sh --dry-run
./install.sh
```

Changed destination files are backed up under `~/.local/state/muse-subagents/backups`. Identical files are skipped. Symlink and non-file destinations are rejected. The installer copies the three tools and skill; it does not overwrite global instructions or install the reference relays. Custom absolute destinations can be supplied through `MUSE_SUBAGENTS_BIN_DIR`, `CODEX_HOME`, and `XDG_STATE_HOME`.

Hamza's Muse settings (`~/.config/muse/settings.json`) also select `muse-spark-1.3-contributor` with `high` reasoning. The installer preserves those settings; the launcher pins its own model and effort independently.

Hamza's global `~/.codex/AGENTS.md` already points to the installed routing skill and tracked launchers. The skill uses Hamza's absolute paths; adapt those instructions when installing for another user. The global snapshot is reference material, not a replacement for another installation's policies.

Both relay copies are historical and are excluded from the active Muse-only model policy. The OpenCode relay is also incompatible: installed OpenCode 2.0.21 rejects its `--dir` and `--variant` flags. agy uses automatic approval and has no built-in worktree isolation. Neither relay is automatically tracked by the Muse dashboard.

## State and verification

Job metadata, JSONL events, diagnostic output, and final answers are retained under `${XDG_STATE_HOME:-$HOME/.local/state}/codex-workers`, with private directories and files. Logs can contain task prompts and source excerpts; keep this state outside the repository. Set `CODEX_WORKERS_DIR` to an absolute path to use a different registry. Finished jobs remain available for inspection; the dashboard shows all active jobs and up to 20 recent results.

Run checks from the repository root:

```bash
python3 tests/test_workers.py
shellcheck install.sh bin/muse-worker bin/codex-with-workers
```

Checks cover pinned model/reasoning defaults, reusable follow-ups, dynamic effort, closure/cancellation, cache-usage accounting, subscription-quota notices, terminal-event validation, malformed and failed results, cancellation of process groups, concurrent jobs, stale process identities, safe arguments, private state files, and tmux with nondefault pane indices. Fake workers and temporary registries are used. The echo transport check requires an installed Muse binary; the tmux check requires tmux. These checks do not test live model authentication or model availability. Test workers and the temporary tmux server are stopped during cleanup.
