# Verified Ladder (`claive-orch`) and Parallel Fan-Out

Read `~/repo/claive/docs/orchestration-ladder.md` before running a ladder. `claive-orch` drives an implementer/critic loop with a deterministic verifier arbiter (plain `claive` does not).

## `claive-orch` workflow

```bash
claive-orch init --repo /abs/repo --task-file /abs/task.md \
  --verify 'uv run pytest -q tests/test_x.py' --arm D --rounds 2 --category bug-fix
claive-orch next RUN
```

- **Verifier requirements:** The verify command must fail at committed `HEAD` (or `--base REV`); a passing base invalidates the run. Do not invent dummy tests or extra scope just to force a task through the arbiter.
- **Base state:** Lanes start from committed `HEAD` (uncommitted parent edits stay out; do not commit just so lanes see them). Check every path, symbol, and shell variable in the task file against the repo first.
- **Follow `next` strictly:** Loop on `claive-orch next RUN` and run the exact action it names. Never bypass a refusal with `--force`; read the refusal and re-run `next`.
- **Integration:** The verifier picks the winner (`stalled` / `unresolved` is never success). Review the diff, run `claive-orch integrate RUN` (applies unstaged and re-verifies without committing), then `claive-orch cleanup RUN --branches`.

## Parallel fan-out and host RAM guard

- **Disjoint write ownership:** Run one `claive-orch` run or `--worktree` per task when files overlap. Read-only workers may share a tree.
- **One heavy job at a time:** Remote models run concurrently, but local tests, builds, and verifiers share ~7 GB RAM. Wrap heavy commands with `claive-memcap <N>G --lock heavy -- CMD` (never lock the `claive` launch itself). Check `uptime` under load and leave no stray processes.

## Quotas, failures, and installed drift

- **Exhausted subscription quota:** Stop dispatching to the exhausted engine/quota, report the reset time when shown, and switch only with configured/approved fallbacks (`claive engines --json`) or user approval.
- **Failed turns:** Read `claive logs ID --stderr` and `claive show ID --json`. Retry once only with a changed hypothesis; otherwise abort (`claive-orch finish RUN --abort --reason "..."`) and report.
- **Installed drift:** If `claive` behavior disagrees with `~/repo/claive`, inspect `claive runtime --source-dir /abs/claive --json` and sync with `install.sh --runtime-only` rather than editing installed files.
