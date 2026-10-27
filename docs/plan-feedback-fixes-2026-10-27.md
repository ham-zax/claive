# Plan: parent feedback fixes (2026-10-27)

Source: parent-session feedback on Claive (four sessions, 2026-10-26/27). Ledger: `TASKS.md`.
Work tree for T1-T3: `/home/hamza/repo/claive-wt-b` (branch `cfx/b`, from HEAD `5db1c53`
plus the uncommitted Terra-default diff already applied). One reusable Codex Terra worker
runs T1, T2 and T3 in order in that tree, one followup per task. All three edit
`bin/claivelib/cli.py`, so they run in sequence, not in parallel.

## Rules for every task

- Edit only the files the task lists. Do not commit, stage, push or delegate.
- Add tests only in the new test file the task names. Existing tests must still pass.
- Run tests only through the shared lock (load is high; one heavy job at a time):
  `flock /home/hamza/.local/state/claive/locks/heavy.lock claive-memcap 1500M -- python3 -m unittest <files> -v`
- Run only the test command the task names. The parent runs the full suite once at the end.
- Keep the code style: stdlib only, short docstrings, `ValueError` for user errors (exit 2).
- When the brief cannot be followed literally, stop and report. Make no design choice.
- End with a `claive-report` block: outcome, files changed, commands run with exit codes, risks.

## Items out of scope

- Ledger keyed by workstream (feedback item 1): TASKS.md belongs to the parent protocol, not
  to Claive. No Claive change.
- Model lookup for dictated names (item 3): `claive engines` already lists every model and
  effort. T4 documents it.
- Effort presets (item 5): `--role` plus custom `roles` in config already exist. T4 adds
  three host roles in the user config and documents them.
- Alias text mismatch: the repo skill is current. The installed Skillshare copy is stale.
  T4 syncs it.

## T1: start robustness (cli.py, acceptance.py, config.py)

Problem: under load 14.5, `claive start` reported "worker supervisor did not start (no PID
after 5 s)", `claive show` reported `interrupted`, and the worker then ran. The parent
relaunched; the relaunch failed on a 30 s `git ls-files` timeout. `supervisor.log` was empty.

Facts: `load()` (cli.py ~336) marks a worker `interrupted` when it has no `supervisor_pid`
5 s after `started_at`. `spawn_supervisor()` (cli.py ~1550) waits a fixed 5 s, then raises.
The spawned process is the supervisor itself: `supervise()` writes `supervisor_pid=os.getpid()`
(cli.py ~639). `acceptance.snapshot()` runs `git ls-files` with a fixed `timeout=30`
(acceptance.py:74, and the other `timeout=30` calls in that function).

Changes:
1. Config: add top-level key `launch` to `TOP_LEVEL_KEYS` in config.py, validated as an
   object with optional keys `start_timeout` (number, 1..600) and `git_timeout` (number,
   1..600). Unknown keys inside `launch` are an error, like other sections. Add accessor
   `config.launch_setting(name, default)`.
2. Spawn record: in `spawn_supervisor()`, right after `Popen` succeeds, write
   `spawn.json` in the job dir: `{"pid": process.pid, "identity": identity(process.pid),
   "spawned_at": time.time()}` (atomic write as `save()` does). Do not write state.json there
   (the supervisor writes state.json concurrently).
3. `load()`: in the branch with no `supervisor_pid`, mark `interrupted` only when the spawn
   process is proven gone: `spawn.json` exists and `identity(pid) != recorded identity`.
   When `spawn.json` is missing (old jobs, or the window before it is written), keep the old
   rule but with a 300 s grace instead of 5 s. A live spawn process keeps status `starting`.
4. Start wait: limit = `launch.start_timeout` when set, else
   `min(60, max(5, 5 * load1 / cpu_count))` (`os.getloadavg()[0]`, `os.cpu_count() or 1`).
   When the process exits without a PID: fail as today. When the limit passes and the process
   is still alive: do not raise. Print to stderr
   `warning: start not confirmed after N s (load L); supervisor process PID is alive. Run claive show ID before any relaunch.`
   and return normally, so the launch exits 0 and prints its usual ID lines.
5. Diagnostics: before `Popen`, write the first line of `supervisor.log`:
   `claive: spawn at <ISO time> load <l1> <l5> <l15>`. After `Popen`, append
   `claive: supervisor pid <pid>`. In `supervise()`, print to stdout (it goes to
   supervisor.log), flushed, `claive: +<s>s <step>` for: `supervisor entered`, `state loaded`,
   `acceptance ready`, `worker process started` (s = seconds since `spawned_at` from
   spawn.json, or since supervise() entry when missing).
6. Git timeout: every `timeout=30` in `acceptance.snapshot()` uses
   `config.launch_setting("git_timeout", 30)`. On `subprocess.TimeoutExpired`, raise
   `ValueError("git ls-files timed out after N s in WS (load L); raise launch.git_timeout in the claive config")`
   (use the real git subcommand name).
7. Duplicate guard: in the launch code next to the existing "owned concurrent writers" check
   (cli.py ~1460), refuse a new non-read-only launch when another job with status
   `starting` (supervisor not confirmed yet), not read-only, in the same resolved workspace
   (`actual_workspace` or `workspace`) has the same `label`. Message:
   `worker ID (label L) has not confirmed its start in this workspace; run claive show ID before any relaunch, cancel it, or pass another --label`.
   Labels default to the prompt stem, so compare the effective label. Same-label workers that
   are `running` or idle stay allowed: the alias `-2` suffix (tests/test_async.py:134) depends
   on it. Writers with owned paths are already refused by the existing ownership check.

Tests: new `tests/test_start_robustness.py`, hermetic (see `tests/hermetic.py` and how
`tests/test_incidents.py` covers `spawn_supervisor`). Cover: a slow supervisor that writes its
PID after the limit (launch exits 0 with the warning, status later `running`/`completed`, never
`interrupted`); a spawn process that dies without a PID (status `interrupted`, launch fails);
`load()` with live spawn pid and no supervisor pid stays `starting`; first log line present;
`launch` config validation (good, bad type, unknown key); git timeout message; duplicate label
refused, read-only duplicate allowed, different workspace allowed.

Test command: `tests/test_start_robustness.py tests/test_incidents.py tests/test_acceptance.py tests/test_async.py`

## T2: parent ergonomics (cli.py, advice.py, acceptance.py)

1. Doctor: the `interrupted` check (cli.py ~1890) lists entries as `ID label age`
   (age like `45m`, `3h`, `2d`, from `finished_at`/`updated_at`/`started_at`, whichever the
   state has, newest first). Entries older than 24 h do not fail the check: the detail ends
   with `N older than 24h; run claive prune --interrupted`. The check fails only when an entry
   is younger than 24 h. Keep the detail within the current 200-char limit by listing at most 5.
2. New command `claive prune --interrupted [--older-than DUR] [--dry-run] [--json]`.
   `--interrupted` is required (the only selector now). `DUR` defaults to `24h`; parse it with
   `wakes.parse_duration` when it accepts `24h`/`30m`/`2d` (check it), else write a small parser in cli.py.
   Selects job dirs whose `load(child)` status is `interrupted` and older than DUR. Skip (and
   print `kept ID: worktree`) any job with `retained_worktree` or a `worktree` dir inside the job
   dir. Delete the rest with `shutil.rmtree`, and remove alias bindings that point at deleted IDs
   (use `aliases.py` functions). Print one line per job and a total. `--dry-run` deletes nothing.
   Exit 0. Add it to the argparse table and to `docs/command-reference.md`.
3. `--fit-checked` flag on every launch parser that has `--checks-file` (store_true). When set,
   `advice.launch_notes()` omits the `Fit:` and `Scope:` notes. The `Bound:` note stays.
   Pass it through state as `state["fit_checked"] = True` only when set.
4. Repeatable `--check 'NAME=COMMAND'` on the same parsers. Each becomes
   `{"name": NAME, "command": shlex.split(COMMAND), "required": true}`. Combine with
   `--checks-file` entries; validate the combined list with the existing rules in
   `acceptance.manifest` (refactor the validation into a function that takes a list, keep
   `manifest(filename)` working). Missing `=`, empty name or empty command: ValueError.
5. Failure summary in `report()` (cli.py ~1644). Replace the header with
   `Command failures during the turn (attempts, not the final result): N cumulative; X this turn; Y previous turn`.
   Then record per-command results: in the event handler, when a tool-completion event carries
   a command (check the normalized events in `bin/claivelib/engines/*.py`: `task_warning` has
   `command`; find whether successful completions also carry `command` and an exit status),
   store `turn_command_results[command] = "ok" | "fail"` (bounded to the last 20 commands, reset
   per turn with the other per-turn keys). Print `Last result per command: OK <cmd>` /
   `FAIL <cmd>` lines (commands cut to 120 chars, `clean()`). When no engine event gives
   success with a command, print only the header and the reasons and report that in risks.

Tests: new `tests/test_parent_ergonomics.py`. Cover doctor age/label/24h rule, prune
(dry-run, delete, kept worktree, alias removal), `--fit-checked` notes, `--check` parsing and
merge with a file, duplicate name across both sources, report header and last-result lines.

Test command: `tests/test_parent_ergonomics.py tests/test_advice.py tests/test_acceptance.py tests/test_features.py`

## T3: isolation for every engine and the heavy lock (engine.py, engines/*.py, cli.py, memcap.py)

1. Worktree for every engine. Move the body of `MuseEngine.prepare_isolation` (muse.py ~138)
   into the base `Engine.prepare_isolation` in engine.py, unchanged in behavior (worktree at
   `job_dir/worktree`, detached for read-only, branch `claive/<job>` for writers, returns
   `isolation` with `mode="existing", owner="claive", source, branch, base_commit`). Muse keeps
   its override only if it needs Muse-specific steps; otherwise delete it.
   In codex.py, claude.py and pi.py, replace the "does not manage worktrees" refusal with the
   same mode validation Muse uses (`none|create|existing`, `--worktree-existing` must be an
   existing absolute dir, `--worktree-base` needs `--worktree`). For these engines the turn's
   working directory must be the isolation `existing_path` on every turn (first turn,
   followup, fork). Find how each engine builds its command and cwd from `TurnRequest.workspace`
   and where follow-up turns pick the workspace (`actual_workspace`); make all three use the
   worktree. `release_worktree` in cli.py already handles cleanup for `owner == "claive"`.
2. `--worktree-setup CMD` on launch parsers with `--worktree` (requires `--worktree`, else
   ValueError). Store it in `launch["worktree_setup"]`. The supervisor runs it once, before the
   first turn only, as `["/bin/sh", "-c", CMD]` with cwd = the worktree, stdout/stderr to
   `job_dir/worktree-setup.log`, under the heavy lock (item 4, lock name `heavy`), timeout
   `launch.worktree_setup_timeout` (add to the T1 `launch` config keys, number 1..3600, default
   900). Non-zero exit or timeout: the turn fails with `failure_kind="worktree_setup"` and error
   `worktree setup failed (exit N); see <log>`; no model turn runs.
3. Dirty-tree warning: at launch, when isolation mode is `create`, run
   `git -C WS status --porcelain=v1 --untracked-files=normal` (timeout from
   `launch.git_timeout`). When any lines exist, print to stderr:
   `warning: the worktree starts from <base short sha> and lacks N uncommitted paths in WS (staged S, unstaged U, untracked T): p1, p2, ... (at most 10)`
   and `To include them, commit by path: git -C WS commit -m MSG -- <paths>`. Never commit.
4. Heavy lock in `claive-memcap`: add `--lock NAME` (NAME matches `[a-z0-9][a-z0-9-]{0,31}`)
   and `--lock-timeout S`. Lock file: `<state dir>/locks/NAME.lock`, state dir resolved like
   `cli.root()` (`CLAIVE_DIR`, else `$XDG_STATE_HOME/claive`, else `~/.local/state/claive`);
   create `locks/` with mode 0700. Take `fcntl.flock(LOCK_EX)` before starting the command and
   hold it until the command ends. If the lock is busy, print once to stderr
   `claive-memcap: waiting for lock NAME (held by pid P)` (after acquiring, write own pid into
   the file). `--lock-timeout` expiry: exit 75 with a message. Expose a helper
   `memcap.hold_lock(name, timeout=None)` (context manager) for item 2.

Tests: new `tests/test_isolation_all_engines.py`. Cover worktree create for codex, claude and
pi with the fixture engines in `tests/` (worktree exists, turn cwd is the worktree, writer
branch kept when changed, removed when clean), validation errors, `--worktree-setup` success and
failure, the dirty-tree warning text, memcap lock serialization (second holder waits), lock
timeout exit 75, bad lock name.

Test command: `tests/test_isolation_all_engines.py tests/test_memcap.py tests/test_codex_engine.py tests/test_claude_engine.py tests/test_pi_engine.py tests/test_workers.py`

## T6: worktree log and housekeeping (cli.py, engine.py, new worktrees.py)

User request (2026-10-27): claive-created worktrees stay opt-in, carry a fixed name, and are
logged, so Claive can list and clean them later when disk space runs low.

1. Opt-in only: `--worktree` stays the only way to create one. Add no config key or role field
   that enables it by default. Add a test that a launch without `--worktree` creates no worktree.
2. Naming: keep the T3 layout as the contract: path `<job dir>/worktree`, writer branch
   `claive/<job id>`, read-only lanes detached. Housekeeping must touch only worktrees that match
   this layout and appear in the log.
3. Log: new module `bin/claivelib/worktrees.py` with `record(event, **fields)` that appends one
   JSON line to `<state dir>/worktrees.jsonl` (mode 0600, `fcntl.flock` on the file while
   appending). Fields: `event` (`created`, `removed`, `kept`, `cleaned`), `at` (epoch),
   `job`, `path`, `branch` (or null), `source` (repo path), `base_commit`, `reason` (for `kept`).
   Call it from the base `prepare_isolation` after a successful `git worktree add` (`created`),
   from `release_worktree` (`removed` or `kept` with the reason from `_worktree_changes` or the
   git error), from `discard_job` (`removed`), and from item 4 (`cleaned`).
4. Command `claive worktree list [--json]` and
   `claive worktree clean [--older-than DUR] [--dry-run] [--discard-changed] [--json]`.
   Current set = log entries folded by path (last event wins), kept only when the path still
   exists. `list` prints per worktree: job, status of its job (`load()`), age, size (`du -s`
   style sum of file sizes, skip symlinks), branch, changed yes/no (`_worktree_changes`), source.
   `clean` selects worktrees whose job is not in `ACTIVE`, older than DUR (default `24h`, parse
   with `wakes.parse_duration`). Unchanged: `git -C source worktree remove path`, then
   `git branch -D branch` (reuse `remove_worktree`). Changed ones (uncommitted files or commits
   ahead of `base_commit`) are skipped with `kept ID: changes (use --discard-changed)` unless
   `--discard-changed` is passed. When the source repo is gone, remove the directory with
   `shutil.rmtree` only when the path is inside the Claive state dir. Never touch a path outside
   the state dir, a worktree not in the log, a worker in `ACTIVE`, or a worktree awaiting
   parent acceptance. Recheck changes before removal and keep detached branches with new
   commits. Keep a worktree when Git refuses removal. Print one line per
   worktree and a total with freed bytes. `--dry-run` changes nothing. Exit 0.
5. `claive doctor`: add a `worktrees` check (never failing): count and total size of logged
   worktrees that still exist, and how many are older than 24 h.
6. `claive prune --interrupted` (T2) keeps skipping jobs with a worktree; its `kept` line now says
   `kept ID: worktree (run claive worktree clean)`.

Tests: new `tests/test_worktree_housekeeping.py`. Cover log lines for create/release/kept/discard,
`list` output fields, `clean` of an old unchanged worktree, skip of a changed one, removal with
`--discard-changed`, skip of an active worker, refusal of a path outside the state dir, dry-run,
missing source repo, the opt-in test from item 1.

Test command: `tests/test_worktree_housekeeping.py tests/test_isolation_all_engines.py tests/test_parent_ergonomics.py tests/test_workers.py`

## T4: docs, skill and config (parent, inline)

- Skill `claive-orchestration`: roster when Muse is disabled (the `claive-orch` implementer
  uses Pi); Terra effort table (`xhigh` complex or design, `high` specified mechanical
  work, `medium` low-stakes work); `claive engines` for dictated names; new flags (`--check`,
  `--fit-checked`, `--worktree` on every engine, `--worktree-setup`, `claive prune`,
  `claive-memcap --lock heavy`); start-not-confirmed rule.
- Same facts in `claive-routing-codex`, README, `docs/harness-integration.md` (remove the stale
  "bind it with `claive alias set`" line), `docs/command-reference.md`.
- User config: roles `terra-complex`, `terra-mechanical`, `terra-lowstakes`.
- Worktree housekeeping (T6): `claive worktree list|clean`, the log file, opt-in rule.
- `skillshare sync`; install the verified runtime from the final commit during T5.

## T5: integration (parent)

Integrate the reviewed work, run `just check` under the lock, run one independent read-only
review (Terra `xhigh`), apply at most one fix round, install the final runtime, then report.
The user also requested a push after verification.
