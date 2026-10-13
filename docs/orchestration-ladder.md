# Verified orchestration ladder (`claive-orch`)

Step-by-step protocol for a verifier-gated `claive-orch` run. The
`claive-orchestration` skill holds the rules for choosing this path and for the
model roster; read this file once a run is actually starting. The deterministic
arbiter decides round progression, reverts, escalation and candidate selection:
follow `claive-orch next RUN`.

## Before you start: measurement and verifiers

- **Measure before you fan out.** For performance, profiling, or debugging, get the
  baseline or the reproduced mismatch first, then launch lanes only at that measured
  hot spot. A lane report written before a baseline is a set of estimates; it cannot
  rank costs.
- **Performance verifier.** An equivalence test must pass after the change, with
  expected output captured from base. A timing or call-count gate must fail at base
  and pass after; a gate that already passes at base does not measure the change.
- **Check before measuring.** Check a new driver's parsing against one real response,
  and check the precondition (an index that is not ready, a missing fixture). A run
  that stops there gives no result, so do not count it.
- **Keep fixtures.** A harness that deletes its state at exit throws away an
  expensive build. Keep the state, and say so in the brief. Never let a probe's
  mutating flag (for example `--edit`) touch the user's checkout.

## The verified ladder (default: arm D, rounds 2)

Everything starts from the repository's **committed** `HEAD` (or `--base REV`).
Uncommitted parent edits are not in the lanes. Do not commit them just so lanes can see
them; commits are the user's call. Check the checkout's risk at integration (below).
Worker writes go only into lane worktrees, never into the user's checkout.

```bash
# 0. Task text: goal, constraints, files and symbols, acceptance criteria.
#    Do not include the solution. Write it to an absolute file. Check every path, symbol,
#    and shell variable against the repo before running (a brief once named a file that
#    did not exist, and an unset variable broke a launch). Label what you have not checked
#    as unverified, and treat handoff notes as hypotheses until `git log` or the code
#    confirms them.
claive-orch init --repo /abs/repo --task-file /abs/task.md \
  --verify 'python3 -m pytest -q tests/test_x.py' --arm D --rounds 2 \
  --category bug-fix   # change|bug-fix|feature|debugging|refactor|docs|other
#    -> "Run <RUN>". It runs the verifier at base. If the verifier already
#       passes, the run is invalid: fix the check.
#    --category feeds `claive-orch stats` (how often each kind needs the ladder).
#    Options: --post-pass-critic (arms B/D) adds one critic pass after the
#    tests pass, looking for behaviour changes beyond the task.
#    --setup 'ln -s "$CLAIVE_ORCH_REPO/node_modules" node_modules' runs in the
#    base check and in every new lane worktree. Files it creates are lane-local:
#    never committed into checkpoints and kept on revert (nor are __pycache__,
#    *.pyc and pytest/mypy/ruff caches).
#    --cache-key 'sha256sum package-lock.json' --cache-path dist reuses build
#    outputs across verifies with the same key (the verifier should skip its
#    build when the cached outputs exist). $CLAIVE_ORCH_CACHE is exported too.
#    --verify-memory 2G caps the verifier and its children (RSS, sampled; over
#    it the round is an error). Workers are told to run their checks under
#    `claive-memcap 2G -- CMD`, which works inside sandboxes without systemd-run.

claive-orch next RUN          # always ask; it prints NEXT / Why / How
```

Then loop on `claive-orch next RUN` and do what it names:

| `next` says | Do |
|---|---|
| `add_lane a` | `claive-orch pick RUN implementer` and run the `claive-orch lane` command it prints: Muse `muse-spark-1.3-contributor`, or Pi `muse-spark-1.3-contributor-free` (effort `max`) when Muse is missing or its quota is exhausted. Tell the user when it picked the fallback |
| `implement a` | `P=$(claive-orch prompt RUN implement a)`; launch `claive open --detach --workspace <lane path> --prompt-file "$P" --label RUN-a --reasoning-effort xhigh --max-model-steps 100` (`max` for very complicated tasks; for a Pi lane use `--engine pi --provider opencode2api --model <lane model> --reasoning-effort max` and no step cap); `claive-orch worker RUN a --role implementer --worker-id W`; `claive wait W` |
| `verify a` | after the implementer's turn ends: `claive-orch verify RUN a`. It runs the tests, then checkpoints or **reverts a regression automatically** |
| `critique a` | `P=$(claive-orch prompt RUN critique a)`; `claive-orch pick RUN critic --lane a` prints the `claive start ... --read-only --fallback-models ...` launch (critic and fallbacks never lane a's family); run it as printed, with the generated prompt and **no** `--role`/`--report` (the critic prompt carries its own JSON contract; a role adds a competing report format); `claive-orch worker RUN a --role critic --worker-id C`; `claive wait C`; `claive-orch critique RUN a --worker-id C` (the lane argument is required) |
| `correct a` | if a suggested defect is wrong (e.g. relaxes a task constraint), first `claive-orch reject RUN a --defect N --reason "..."`; then `P=$(claive-orch prompt RUN correct a)`; `claive followup W --prompt-file "$P"` (**same session**: it keeps its context); `claive wait W`; `claive-orch verify RUN a` |
| `add_lane b` | lane a stalled. `claive-orch pick RUN breadth` prints `claive-orch lane RUN b --engine pi --model <family ≠ a and ≠ a's critics> --strategy "..."`; write the strategy (a materially different approach) yourself, then the same implement, verify, critique and correct cycle in lane b, with a critic of yet another family where possible. Lane b **never** sees lane a's diff |
| `review` | `P=$(claive-orch prompt RUN review)`; start a read-only Pi reviewer; `claive-orch review RUN --worker-id R` |
| `finish` | `claive-orch finish RUN`, `claive-orch report RUN` (collects token usage automatically), then integrate (below) |

Lane paths are printed by `claive-orch lane` and in `next`'s `How:` line.
Every step is guarded: if `claive-orch` refuses a command, read its message and
re-run `next`. Do not work around it with `--force`.
Held-out checks: put them in a directory **outside** the repository, pass
`--acceptance-dir DIR`, and pass `--worker-verify '<visible-only command>'` so
workers can still run the visible tests. Hidden check names are never shown to
workers. If a held-out check (or the verifier) itself was wrong, fix it, then
`claive-orch rescore RUN --reason "..."` re-verifies every lane without using a
round.

Arms for daily use: `D` is the default (cheap when L0 passes, escalates only on
evidence). `B` means no breadth. `A` means a single attempt plus verification.

### Self-tuning picks (no experiments needed)

Every run is data. `claive-orch stats [--json]` reads all runs' events: per
critic, whether the lane's next checkpoint improved, stayed equal, was reverted,
or the critic found nothing (`helpful_rate`); per implementer, first-try and
eventual pass rates; per `--category`, how often L0 failed and the ladder
recovered. `claive-orch pick` uses it: each critic gets 3 scored uses first
(cold start), then the best smoothed helpful rate wins with 20 % seeded
exploration; breadth models rank by lane pass rate. Picks are recorded in the
run (`pick.made`). They are suggestions inside the rules above: the arbiter
still refuses a same-family critic.

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
   unrelated churn. The verifier passing is necessary, not sufficient: check the diff against the mechanism
   you reproduced, and run `git -C REPO status --short` before integrating.
2. `claive-orch integrate RUN`: applies the diff to the checkout **unstaged**
   (the user's staged changes stay staged), re-runs the verifier there, and
   closes the run's idle reusable workers. Exit 1 means a `Conflict: <path>`
   (resolve it by hand) or a failing verifier in the checkout. `--no-verify`
   skips the re-run. `--paths P ...` / `--exclude P ...` apply only part of the
   diff. It never commits; commit only if the user asked. The re-run includes the user's
   uncommitted work, so its pass covers the combined tree, not the lane alone.
3. `claive-orch cleanup RUN --branches` removes the worktrees and lane
   branches (an unintegrated winner is kept unless `--force`).
   `claive-orch prune --repo REPO` lists stale `orch/*` branches from old runs;
   add `--apply` to delete them.

Report to the user: outcome, winner lane and model, score trajectory, rounds,
reverts, critic families, tokens (`claive-orch report RUN`), and anything you
could not verify. Keep it short.

## Quota fallback inside a run

- Muse reports `Subscription quota exhausted` (a five-hour quota): stop assigning
  Muse work and use the **pre-approved fallback**, Pi
  `muse-spark-1.3-contributor-free` at `max`. Tell the user the reset time and
  that the fallback is in use. A run's lane cannot switch engines, so: note the
  best checkpoint commit (`claive-orch report RUN`), `claive-orch finish RUN
  --abort --reason "muse quota"`, then `claive-orch init` a new run with
  `--base <that commit>` (or the original base if none) and
  `claive-orch lane NEW a --engine pi --model muse-spark-1.3-contributor-free`.
  Any other fallback model needs the user's approval. Go back to Muse after the
  reset. `claive-orch pick RUN implementer` does this choice for new runs: it
  reads the quota state recorded by earlier Muse workers (and whether the Muse
  binary exists) and picks the fallback until the reset time passes.
