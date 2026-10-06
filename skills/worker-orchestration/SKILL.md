---
name: worker-orchestration
description: Orchestrate Muse and Pi worker agents from Claude Code or Codex using the codex-orch arbiter. A Muse implementer, a cross-family Pi critic, and verifier-gated refinement with checkpoint revert, escalating to one diverse second candidate only after a stall. Use when the user asks to orchestrate, delegate to workers or subagents, "use Muse/Pi", refine with a critic, or run several workers on different models; also for parallel fan-out of independent subtasks.
---

# Worker orchestration (Muse + Pi, arbiter-driven)

You are the **parent**: you plan, write task text, launch workers, and integrate.
You do **not** decide acceptance, reverts, round limits, escalation, or selection.
The deterministic arbiter `codex-orch` decides those. Ask it `codex-orch next RUN`,
do exactly the one action it names, and report the result back. The tests decide
correctness; no model opinion overrules them.

Background and evidence: `/home/hamza/repo/codex-muse-workers/docs/experiment/`
(the paper *Refining Over Resampling*, arXiv 2608.05643, plus the design and
`06-skill-driven-implementation.md`). Source and tests:
`/home/hamza/repo/codex-muse-workers` (`bin/codex-orch`, `bin/codex-workers`).

## When to use it, and when not

- **Use** for a task with an executable check (tests, a type-check, a build, or a
  script you can write) that is big enough that a wrong first attempt is likely or
  costly.
- **Do it yourself** when the task is small or tightly coupled to what you are
  doing, or when delegation overhead exceeds the work.
- **No verifier?** Write one first: a small test or check script that fails now.
  If that is impossible, use a plain `codex-workers` fan-out (below) and review
  the result yourself. Do not pretend a model review is verification.
- Respect the host's rules on delegation. If the user has not asked for workers
  or subagents and the host instructions require permission, ask first.

## Roles and roster

| Role | Default | Why |
|---|---|---|
| Implementer (lane a) | Muse `muse-spark-1.3-contributor` (engine `muse`, model pinned, effort `high`) | Workhorse: follows instructions precisely |
| Critic (read-only) | Pi, a family **other than muse-spark**: `nemotron-3-ultra-free`, `mimo-v2.6-flash-free`, `big-pickle`, `longcat-2.5-preview-free`, `ling-3.1-flash-free`, `space-bunny-free` | A different family sees different blind spots; Muse is a weak critic of itself |
| Second candidate (lane b, arm D) | Pi, a family different from lane a, **plus a different strategy** | Diversity must be deliberate (paper: resampling saturates) |
| Reviewer (tie only) | Pi read-only, a different family from both lanes if possible | Called at most once |
| Verifier | The verify command | Final judge |

Families: big-pickle, ling, longcat, mimo, muse-spark, nemotron, space-bunny.
`muse-spark-1.3-free` and `muse-spark-1.3-contributor-free` on Pi count as **the
same family as Muse**: never use them to critique Muse. `codex-orch worker`
refuses same-family critics.

Pi rules: **always pass `--model`** (an explicit model also becomes Pi's global
default, and omitting it inherits whatever was used last), plus `--engine pi
--provider opencode2api`. Pi has no `--max-model-steps`, `--output-schema`,
`--web`, or worktree flags. Pi read-only has **no bash**, so the critic prompt
`codex-orch` generates already contains the diff and the verifier output. The
gateway models are free tiers: on a rate limit or an error, retry once, then pick
another family and mention it in your report.

## Host mechanics

All paths must be absolute. The worker ID is the 12-hex value on the first output
line: `Worker <id> | label | path`.

**Claude Code.**
- Long-lived implementer: run `codex-workers open ...` with Bash
  `run_in_background: true`, then read the first line of that background output
  for the worker ID. If it is not there yet, run
  `codex-workers list --json | jq -r '.[] | select(.label=="LABEL") | .id'`.
- Single-turn critics and reviewers: `codex-workers start ...` returns at once
  and prints the ID.
- Waiting: `codex-workers wait ID`, either with `run_in_background: true` (you
  are notified when it exits) or with `timeout: 600000`. Exit 0 = completed,
  130 = cancelled, anything else = failed.
- Never use `&`, `nohup`, or `sleep` polling loops.

**Codex.** Launch `codex-workers open ...` (or `codex-subagent-worker ...`,
which is the same thing) through `exec_command` with a short `yield_time_ms`,
and keep the shell session ID. Collect output with `write_stdin`. Critics may
use `codex-workers start`. Full Codex mechanics, caching, and the quota policy
are in the `subagent-routing` skill.

Common commands: `codex-workers show ID --json`, `logs ID [--stderr]`,
`usage ID --json`, `followup ID --prompt-file F`, `close ID`, `cancel ID`.

## The verified ladder (default: arm D, rounds 2)

Everything starts from the repository's **committed** `HEAD` (or `--base REV`).
Uncommitted parent edits are not in the lanes, so commit them or ask the user
first. Worker writes go only into lane worktrees, never into the user's checkout.

```bash
# 0. Task text: goal, constraints, files and symbols, acceptance criteria.
#    Do not include the solution. Write it to an absolute file.
codex-orch init --repo /abs/repo --task-file /abs/task.md \
  --verify 'python3 -m pytest -q tests/test_x.py' --arm D --rounds 2
#    -> "Run <RUN>". It runs the verifier at base. If the verifier already
#       passes, the run is invalid: fix the check.

codex-orch next RUN          # always ask; it prints NEXT / Why / How
```

Then loop on `codex-orch next RUN` and do what it names:

| `next` says | Do |
|---|---|
| `add_lane a` | `codex-orch lane RUN a --engine muse --model muse-spark-1.3-contributor` |
| `implement a` | `P=$(codex-orch prompt RUN implement a)`; launch `codex-workers open --workspace <lane path> --prompt-file "$P" --label RUN-a --reasoning-effort high --max-model-steps 100` (background, see host mechanics); `codex-orch worker RUN a --role implementer --worker-id W`; `codex-workers wait W` |
| `verify a` | after the implementer's turn ends: `codex-orch verify RUN a`. It runs the tests, then checkpoints or **reverts a regression automatically** |
| `critique a` | `P=$(codex-orch prompt RUN critique a)`; `codex-workers start --engine pi --provider opencode2api --model <other family> --read-only --workspace <lane path> --prompt-file "$P" --label RUN-a-critic`; `codex-orch worker RUN a --role critic --worker-id C`; `codex-workers wait C`; `codex-orch critique RUN a --worker-id C` (the lane argument is required) |
| `correct a` | `P=$(codex-orch prompt RUN correct a)`; `codex-workers followup W --prompt-file "$P"` (**same session**: it keeps its context); `codex-workers wait W`; `codex-orch verify RUN a` |
| `add_lane b` | lane a stalled. `codex-orch lane RUN b --engine pi --model <family ≠ a and ≠ the last critic> --strategy "<a materially different approach>"`, then the same implement, verify, critique and correct cycle in lane b, with a critic of yet another family where possible. Lane b **never** sees lane a's diff |
| `review` | `P=$(codex-orch prompt RUN review)`; start a read-only Pi reviewer; `codex-orch review RUN --worker-id R` |
| `finish` | `codex-orch finish RUN`, `codex-orch usage RUN`, `codex-orch report RUN`, then integrate (below), close workers, `codex-orch cleanup RUN` |

Lane paths are printed by `codex-orch lane` and in `next`'s `How:` line.
Every step is guarded: if `codex-orch` refuses a command, read its message and
re-run `next`. Do not work around it with `--force`.
Held-out checks: put them in a directory **outside** the repository, pass
`--acceptance-dir DIR`, and pass `--worker-verify '<visible-only command>'` so
workers can still run the visible tests. Hidden check names are never shown to
workers.

Arms for daily use: `D` is the default (cheap when L0 passes, escalates only on
evidence). `B` means no breadth. `A` means a single attempt plus verification.

### Rules the arbiter enforces (so you know why)

- At most D rounds (default 2, max 3). Lane b gets at most 2.
- A round that scores **worse** than the best checkpoint is reverted
  (`reset --hard` plus `clean -fd`). Equal counts as no progress. Two
  no-progress rounds in a row mean a stall.
- The critic says "no concrete defect", or marks a defect non-localized or the
  approach unsound: lane a stalls, then escalates under arm D.
- The verifier picks the winner. A reviewer is consulted only on a tie, then
  the smaller diff wins. A failing best is reported as `stalled` or `unresolved`,
  never as success.
- Malformed critic output becomes "no concrete defect" plus a recorded error.
  That is safe, but tell the user if it keeps happening with one model.

## Integrating the result

After `finish`, the winning checkpoint is on branch `orch/RUN/LANE`.

1. Read `git -C REPO diff BASE COMMIT` (both printed by `finish` and `report`).
   Check that it is in scope, has no test deletions or tampering, and no
   unrelated churn. The verifier passing is necessary, not sufficient.
2. Apply it without committing:
   `git -C REPO diff BASE COMMIT | git -C REPO apply --3way`.
   Commit only if the user asked.
3. Re-run the verifier in the user's checkout.
4. `codex-workers close W` for every reusable worker, then
   `codex-orch cleanup RUN` (removes worktrees and keeps the branches). Delete the
   branches only when the user agrees: `git -C REPO branch -D orch/RUN/a ...`.

Report to the user: outcome, winner lane and model, score trajectory, rounds,
reverts, critic families, tokens (`codex-orch report RUN`), and anything you
could not verify. Keep it short.

## Parallel fan-out (independent subtasks)

For several independent tasks with **disjoint file ownership**, create one run
per task (each run has its own lane worktrees) and drive them concurrently. Or,
for unverifiable chores, use plain `codex-workers open` workers, each in its own
worktree. Rules:

- One writer per worktree. Read-only workers may share.
- At most 2–3 concurrent workers and **one verifier at a time**: the machine has
  about 7 GB RAM, shared.
- Give each worker its scope, the files it owns, "do not commit, do not delegate,
  others are editing nearby", and the report format: outcome, files, checks run,
  doubts, blockers.
- Integrate one at a time and re-verify after each.

## Quota and failures

- Muse reports `Subscription quota exhausted` (a five-hour quota): stop assigning
  Muse work. Report the reset time and **ask the user** whether to wait or use a
  specific Pi model for that lane. Never switch silently.
- A worker that fails (non-zero `wait`) is not success. Read
  `codex-workers logs ID --stderr`, retry once only with a changed hypothesis,
  otherwise `codex-orch finish RUN --abort --reason "..."` and report.
- A completed worker's report is not proof. Only `codex-orch verify` and your own
  re-verification count.
- Never run worker tasks against production systems or real data.
