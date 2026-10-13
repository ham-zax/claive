---
name: claive-orchestration
description: Operate Claive workers across Claude Code, Codex, and shell parents. Use plain claive for bounded delegation or fan-out; use claive-orch for authorized, verifier-gated Muse/Pi refinement. Covers worker lifecycle, engine selection, evidence, and integration.
---

# Claive orchestration — delegation and verified runs

You are the **parent**: own the task scope, worker assignment, final verification, and integration.
For a verifier-gated `claive-orch` run, its deterministic arbiter decides round progression,
reverts, escalation, and candidate selection. Follow `claive-orch next RUN`.
For plain `claive` delegation, these arbiter rules **do not apply**: the parent judges
the evidence and makes authorized decisions. A worker report alone is not proof.

Source and tests: the Claive checkout (`/home/hamza/repo/claive` on the laptop,
`/home/ubuntu/repo/claive` on ARM; `bin/claive-orch`, `bin/claive`).
The TTC experiment is deferred; `docs/experiment/README.md` is its status note,
not part of the active workflow.

## When to use it, and when not

- **Use** for a task with an executable check (tests, a type-check, a build, or a
  script you can write) that is big enough that a wrong first attempt is likely or
  costly.
- **Do it yourself** when the task is small or tightly coupled to what you are
  doing, or when delegation overhead exceeds the work.
- **Fit gate.** Before any launch, state `existing output that already answers it |
  executable check | cost of a wrong first attempt | why not inline`. Read the README of
  the tool that produced the data before briefing a worker to analyse it. If a part is
  missing, do the work inline and say so, even when the user asked to delegate.
- **One scoped task per turn.** For multi-part work open one reusable worker
  (`claive open --detach`) and send one small task with one checkable deliverable per
  turn. Read its summary, check the artifact, then `claive followup` the next task. Do
  not put several deliverables in one brief.
- **Bound every turn.** Pass `--turn-timeout`. Make the first wait short
  (`claive wait ID --timeout 300`); if it prints `Reasoning-heavy` or no file or report
  has appeared, cancel and narrow the task or lower the effort.
- **No suitable authorized verifier?** Use direct work or plain `claive` delegation
  with focused evidence review. Do not introduce tests, scripts, or extra scope solely
  to force a task through the arbiter. A model review is not executable verification.
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
- Respect the host's rules on delegation. Hamza's Claude Code setup grants
  standing permission for Muse and Pi workers, so no per-task ask is needed
  there; elsewhere, ask first if the host requires it.

## Worker manager versus verifier-driven arbiter

`claive` supports `--engine muse`, `--engine pi`, `--engine claude`, and
`--engine codex` for ordinary bounded worker tasks. The installed runtime pins
Muse to `muse-spark-1.3-contributor`, Claude to `claude-haiku-5-5`, and Codex to
`gpt-6-luna`; Pi uses `opencode2api` and its selected model. Claude and Codex
worker engines were added after the original Muse/Pi skill workflow.

`claive-orch` **still uses Muse/Pi** for its default implementer/critic ladder;
its model choices and verifier protocol below do not automatically authorize
Claude/Codex workers or change the general worker manager's engine limits.
Choose ordinary delegation when the full verifier ladder adds no value.
Engine-specific sandbox, settings, and option restrictions are documented in
`~/repo/claive/README.md` and `docs/harness-integration.md`.

Use `claive-routing-codex` for Codex's managed-terminal integration. Claude Code
can follow this shared orchestration skill and its existing global CLAUDE.md
policy directly; there is no separate Claude routing skill. The TTC experiment
is deferred and has no active skill or protocol in this repository.

## Roles and roster

| Role | Default | Why |
|---|---|---|
| Implementer (lane a) | Muse `muse-spark-1.3-contributor` (engine `muse`, model pinned, effort `xhigh`; `max` for very complicated tasks) | Workhorse: follows instructions precisely |
| Implementer fallback (Muse quota exhausted only) | Pi `muse-spark-1.3-contributor-free`, effort `max` | Same model family through the free gateway; pre-approved by the user |
| Critic (read-only) | Pi, in this order: `mimo-v2.6-flash-free`, `big-pickle`, `space-bunny-free`; rarely `longcat-2.5-preview-free` | A different family sees different blind spots; Muse is a weak critic of itself |
| Second candidate (lane b, arm D) | Pi, from the same preferred list, a family different from lane a and the last critic, **plus a different strategy** | Diversity must be deliberate |
| Reviewer (tie only) | Pi read-only, a different family from both lanes if possible | Called at most once |
| Verifier | The verify command | Final judge |

**Do not use** `nemotron-*` or `ling-3.1-flash-free` (user preference). Use
`longcat-2.5-preview-free` only when the three preferred families are already
used in the run or failing. `muse-spark-1.3-free` and
`muse-spark-1.3-contributor-free` on Pi count as **the same family as Muse**:
never use them as critic or lane b against Muse. `claive-orch worker` refuses
same-family critics.

**Reasoning effort.** Pi models are free, so always run them at `max`
(`--reasoning-effort max`, the Pi engine default). Pi clamps it to each model's
highest level: measured mimo and big-pickle → `high`,
muse-spark-1.3-contributor-free → `xhigh`, space-bunny → `max`. Muse uses `xhigh`
for normal tasks (the default) and `max` for very complicated ones (deep
debugging, cross-cutting design, subtle concurrency or data-format work). Use
`high` or `medium` only rarely, for trivial mechanical chores, and say why.
Claude and Codex workers use `high` unless the user names a level for that task: at
`max` a small Claude model spent 63% of a 40-minute turn in hidden reasoning and wrote
nothing.

Pi rules: **always pass `--model`** (an explicit model also becomes Pi's global
default, and omitting it inherits whatever was used last), plus `--engine pi
--provider opencode2api`. Pi has no `--max-model-steps`, `--output-schema`,
`--web`, or worktree flags. Pi read-only has **no bash**, so the critic prompt
`claive-orch` generates already contains the diff and the verifier output. The
gateway models are free tiers: on a rate limit or an error, retry once, then pick
another family and mention it in your report.

## Host mechanics

The same protocol works in every host (Claude Code, Codex, a Pi agent, a plain
shell); only how you wait differs. Full protocol:
`/home/hamza/repo/claive/docs/harness-integration.md`.

All paths must be absolute. The ID is the 12-hex value on the first output
line: `Worker <id> | label | path`. Exit codes everywhere: 0 completed, 1 failed,
3 needs a parent decision, 124 `--timeout` expired (still running), 130 cancelled.

- **Launch without blocking:** `claive start ...` (one turn),
  `claive open --detach ...` (reusable implementer; returns at once),
  `claive batch start PLAN.json` (parallel lanes of stages). Never use `&`,
  `nohup`, or `sleep` polling loops.
- **Wait:** `claive wait ID [ID ...] --any --timeout S` returns the first worker
  to settle (with its report and code); on 124 do other work and wait again.
  Several IDs need `--any`: without it the wait exits 1 at once and waits for nothing.
  To check whether a worker still runs, read `claive show ID --json` status, not a
  `pgrep` pattern (your own shell's command line contains the pattern).
- **After a context reset or between turns:** `claive inbox --consumer <host>`
  lists every finished turn since your last read (`ASK` lines carry the
  question). Use one consumer name per parent.
- **Long tasks:** `claive mission new --title T --goal-file F`, then
  `export CLAIVE_MISSION=<id>` (or `--mission`) so launches link themselves;
  `mission note` decisions; `claive mission show ID` prints `Next:`. Close it with
  `claive mission close ID` when the effort ends.
- Workers cannot launch workers (`CLAIVE_WORKER_ID` guard).

Host specifics:
- **Claude Code:** plain Bash for launches. Wait either in the foreground
  (`wait ... --any --timeout 540`, Bash `timeout: 600000`) or with
  `run_in_background: true` and no `--timeout` to be notified. Inbox consumer
  `claude`. Output of a `run_in_background` call goes to its task file; read that file,
  since a later foreground command will not show it. Foreground `sleep` is blocked by
  the harness: wait with the command above, or use Monitor, not a sleep loop.
- **Codex:** follow `claive-routing-codex` for launch mechanics. Prefer
  `claive-worker` in a managed shell with a short initial yield; retain both
  worker ID and shell session ID. Detached launches remain available when
  the task needs them. Consumer `codex`.
- **Pi agent or shell:** the same commands through bash. Consumer
  `pi-<name>` or `shell`. Optional push: `CLAIVE_NOTIFY_CMD`, which gets the event JSON
  on stdin.

Common commands: `claive show ID --json`, `logs ID [--stderr]`,
`usage ID --json`, `followup ID --prompt-file F`, `close ID`, `cancel ID`.
Read output in bounded pieces. `logs` prints raw JSONL, and one event can be several
KB: use `claive logs ID --lines 40 | cut -c1-300`. `show --json` has `status` and
`exit_code`, parsed worker reports when requested, and separate verification/acceptance
evidence. Read final text with `claive show ID | head -n 80 | cut -c1-300`.

## Plain workers: evidence and parent acceptance

Keep three claims separate: the supervisor records **execution**, the verifier
records **checks**, and the parent records **acceptance**. Completing one stage
does not authorize the next. Exit 0 or a worker's `done` report establishes only
its own scope; neither grants parent acceptance. The `claive-orch` arbiter below
keeps its existing protocol; these controls are for ordinary `claive` workers.

Before launching consequential work, declare the required checks and file
ownership. Pass `--checks-file /abs/checks.json` and repeat `--owned-path` for
workspace-relative files, directories, or globs. The manifest is a JSON array:

```json
[{"name":"analyzer","command":["systemd-analyze","verify","job.timer"],"required":true,"timeout":60}]
```

Declare mandatory analyzers individually. A test suite exiting 0 while skipping
one does not prove that analyzer ran. Use one writer per isolated worktree;
Muse can create it with `--worktree`, other adapters use an externally managed
worktree as `--workspace`. Ownership is checked against tracked/deleted and all
untracked files, including ignored build output and the initial dirty baseline.
It validates final changes; the engine sandbox governs filesystem restrictions.
Build/package checks must run in scratch storage. Opted-in evidence snapshots
require Git and `CLAIVE_DIR` outside the worker workspace.

After execution settles, use `claive verify ID` to run manifest commands in fresh
scratch copies. For checks observed elsewhere, record `--check NAME --status
passed|failed|blocked|not_run --reason 'evidence' --environment 'host/context'`.
Then the parent can `claive accept ID`; it never runs automatically. Blocked or
unrun mandatory checks keep acceptance pending. Only an explicitly authorized
parent exception (`--exception 'reason'`) may resolve those; failed mandatory
checks cannot be excepted. Ownership violations require an explicit scope
change (`claive scope ID --owned-path ... --reason ...`), not a worker expanding
its assignment. Never invent an exception to obtain a green status.

Inspect `show --json` evidence for the current turn. New turns start unverified
and pending, evidence changes reset acceptance, and later workspace edits make
verification stale. Managed worktrees with ownership/check contracts are kept
for verification/integration; remove them when that task chain ends. Acceptance
records and the recursion guard are local workflow controls, not an OS security
boundary against a process with the same account's permissions.

Reporting: current/previous-turn warning counts differ from cumulative history;
`phase` differs from last observed activity, especially with overlapping tools;
queue depth/pending follow-ups differ from instructions sent during a turn.
`usage --json` includes recent-call input, cache percentage and growth. Provider
cumulative totals are not the live context-window size, and cache reuse does not
prove efficiency.

For live steering, launch Pi with `--enable-steering` and use `claive steer ID
--message 'instruction' --wait 10 --json`. Native RPC queues input after current
tool calls and before the next model call. Correlated acknowledgement proves
provider acceptance of input, not model consumption/compliance. Default Pi print
mode and current Muse/Claude/Codex transports reject live steering; `followup`
remains a separate queued turn, and `cancel` stops execution. Steering cannot be
combined with single-turn model fallbacks. Read `docs/steering.md` for receipt
states and `docs/harness-integration.md` for the full evidence contract.

When installed behavior disagrees with source, run `claive runtime --source-dir
/abs/claive --json` before theorizing about flags. Sync through the project's
`install.sh --runtime-only` (backups preserved), not individual installed files.
The Skillshare source (`~/.config/skillshare/skills/`) is the canonical copy of this
skill: edit it there, `skillshare sync`, then copy it into the Claive checkout's
`skills/` mirror. On Pi-only hosts, respect that engine policy
and the host's memory budget; do not force the laptop's Muse roster or RAM limit.

### Let workers talk to each other (aliases and conversations)

Workers can exchange replies, so a critic can answer a proposer without the parent
copying text between them. Use it when two or more perspectives (different models or
roles) should react to each other's reasoning: design debate, proposer/critic/reviewer
rounds, cross-checking a diagnosis. Do not use it for independent subtasks (use a batch)
or for anything with an executable check (use the `claive-orch` ladder).

```bash
# 1. Open reusable workers with their own role, engine/model and --report; wait for turn 1.
claive open --detach --engine pi --model mimo-v2.6-flash-free --role reviewer \
  --workspace /abs/repo --prompt-file /abs/role-critic.md        # -> Worker ID
# 2. Name them (any parent sharing $CLAIVE_DIR can reuse the names).
claive alias set critic ID1 ; claive alias set builder ID2 ; claive alias list --json
# 3. Run the bounded exchange in the foreground (use a short yield / run_in_background).
claive conversation start @builder @critic --message-file /abs/topic.md --rounds 2 --timeout 300 --json
claive conversation show CONVERSATION_ID --json
```

- `@name` works wherever a worker ID does (`show`, `wait`, `followup`, `answer`...). A
  name is resolved once per command and everything stored keeps the real worker ID, so
  rebinding (`alias set NAME NEW --replace`) never moves an existing assignment. Names
  never launch or resume anything.
- A conversation uses 2..8 distinct idle reusable workers (no queued work, no unresolved
  failure or question; workers started before conversation support must be reopened with
  their retained `--session-id`). It visits them in order, `--rounds` 1..8 times, and hands
  each the previous participant's reply as evidence, not as an instruction. Each worker
  keeps its session, model, permissions and role; peer text never grants permissions.
  A worker can be in only one running conversation. `CLAIVE_MISSION` links it to a mission.
- Exit codes: 0 completed; 3 a worker asked a parent question (see `needs_parent`, reply
  with `claive answer`, then start a new conversation); 124 timeout; 130 stopped; 1 failure
  or a missing/invalid required `--report`. On 124/130 read `pending_request.response_file`
  before sending anything again: an in-flight turn may still finish.
- Replies come from per-request receipts (`<worker>/responses/<request-id>.json`), never
  from a worker's mutable `result.txt`. The parent still verifies the outcome and owns
  integration.

Health and structured reports: `claive doctor [--json]` is a read-only
check (binaries, config, Pi `models.json` coverage as `pi_provider`, provider
latency, any loopback override) and never launches a model; `--live` adds one
tiny read-only Pi turn. `--role scout|worker|reviewer|oracle`
sets engine, model, effort, read-only, and step-cap defaults and always
requests a `claive-report` block; `--report` requests it without a role.
A turn asking for a decision exits 3, shows `ASK`, and records
`needs_parent`; reply with `claive answer ID --message ...` (reusable
workers only). Failed turns record `failure_kind`, visible in `show --json`.

Unattended queue: `claive goal add --title T --prompt-file F --workspace W
[--write] [--budget 2h]`, run by `claive serve` (systemd user unit in
`systemd/`). Goals are read-only by default and obey the config `workspaces`
allowlist. A parked goal shows as `goal ID ASK` in the inbox; answer it with
`claive goal answer ID --message ...`. `claive serve --stop|--resume|--status`
controls the stop switch.

## The verified ladder (default: arm D, rounds 2)

Everything starts from the repository's **committed** `HEAD` (or `--base REV`).
Uncommitted parent edits are not in the lanes. Do not commit them just so lanes can see
them; commits are the user's call. Run from committed HEAD (or `--base REV`), and check
the checkout's risk at integration (below). Worker writes go only into lane worktrees, never into the user's checkout.

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
still refuses a same-family critic. The deferred TTC experiment mode is outside
this active workflow.

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

## Parallel fan-out (independent subtasks)

For several independent tasks with **disjoint file ownership**, create one run
per task (each run has its own lane worktrees) and drive them concurrently. Or,
for unverifiable chores, use plain `claive open` workers, each in its own
worktree. Rules:

- One writer per worktree. Read-only workers may share.
- No fixed cap on concurrent workers (the models run remotely), but heavy runs (tests,
  builds, verifiers) share a **9 GB** budget (the user's memory note). The caps of heavy
  runs active at once must sum to at most 9G. Cap every heavy run with `claive-memcap <N>G -- CMD`
  or `systemd-run --user --scope -q -p MemoryMax=<N>G -p MemorySwapMax=0 -- CMD`. CLAUDE.md
  still says avoid parallel heavy jobs, and the memory note allows them within the budget.
  Until the user settles that, run **one heavy job at a time**, capped at 9G. Check `uptime`
  first, re-run timings taken under load, and kill only processes you started.
- Give each worker its scope, the files it owns, "do not commit, do not delegate,
  others are editing nearby", and the report format: outcome, files, checks run,
  doubts, blockers.
- Integrate one at a time and re-verify after each.

## Quota and failures

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
- A worker that fails (non-zero `wait`) is not success. Read
  `claive logs ID --stderr`, retry once only with a changed hypothesis,
  otherwise `claive-orch finish RUN --abort --reason "..."` and report.
- A completed worker's report is not proof. Only `claive-orch verify` and your own
  re-verification count.
- Never run worker tasks against production systems or real data.
