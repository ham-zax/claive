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

Source, tests and docs: the Claive checkout (`/home/hamza/repo/claive` on the laptop,
`/home/ubuntu/repo/claive` on ARM). Paths below such as `docs/...` are relative to it.
The TTC experiment is deferred and is not part of this workflow.

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
  turn. Read its summary, check the artifact, then `claive followup` the next task.
- **Bound every turn.** Pass `--turn-timeout`. Make the first wait short
  (`claive wait ID --timeout 300`); if it prints `Reasoning-heavy` or no file or report
  has appeared, cancel and narrow the task or lower the effort.
- **No suitable authorized verifier?** Use direct work or plain `claive` delegation
  with focused evidence review. Do not introduce tests, scripts, or extra scope solely
  to force a task through the arbiter. A model review is not executable verification.
- **Performance, profiling, debugging:** measure the baseline first; see the
  measurement rules at the top of `docs/orchestration-ladder.md`.
- Respect the host's rules on delegation. Hamza's Claude Code setup grants
  standing permission for Codex and Pi workers; elsewhere, ask first if the host
  requires it.

## Engines and policy

`claive` runs ordinary bounded tasks on `--engine muse|pi|claude|codex`. The runtime
pins Muse to `muse-spark-1.3-contributor`, Claude to `claude-haiku-5-5`, and Codex to
`gpt-6-luna` at `max` (or `gpt-6.1-sol` at `low`, `medium`, `high` or `xhigh`; `medium` when no effort is passed); Pi uses `opencode2api` and its selected model. `claive-orch` uses only
Muse/Pi for its implementer/critic ladder; its roster does not authorize Claude/Codex
workers. Engine sandbox, settings and option limits: `README.md` and
`docs/harness-integration.md`. Codex parents also follow `claive-routing-codex`.

**Check `claive engines [--json]` before choosing an engine.** It lists each engine's
enabled state, the user's reason, default and disabled models and efforts, and steering
transport. A launch the user disabled is refused: do not retry or work around it.

## Roles and roster

| Role | Default | Why |
|---|---|---|
| Implementer (lane a) | Muse `muse-spark-1.3-contributor` (effort `xhigh`; `max` for very complicated tasks) | Workhorse: follows instructions precisely |
| Implementer fallback (Muse quota exhausted only) | Pi `muse-spark-1.3-contributor-free`, effort `max` | Same family through the free gateway; pre-approved by the user |
| Critic (read-only) | Pi, in this order: `mimo-v2.6-flash-free`, `big-pickle`, `space-bunny-free`; rarely `longcat-2.5-preview-free` | A different family sees different blind spots; Muse is a weak critic of itself |
| Second candidate (lane b, arm D) | Pi, from the same list, a family different from lane a and the last critic, **plus a different strategy** | Diversity must be deliberate |
| Reviewer (tie only) | Pi read-only, a different family from both lanes if possible | Called at most once |
| Verifier | The verify command | Final judge |

**Do not use** `nemotron-*` or `ling-3.1-flash-free` (user preference). Use
`longcat-2.5-preview-free` only when the three preferred families are already
used in the run or failing. `muse-spark-1.3-free` and
`muse-spark-1.3-contributor-free` on Pi count as **the same family as Muse**:
never use them as critic or lane b against Muse.

**Reasoning effort.** Pi models are free: run them at `max` (the Pi engine default;
Pi clamps it to each model's highest level). Codex (`gpt-6.1-sol`) takes its effort
from the kind of turn: `xhigh` for planning, logic or design debates and other important
high-level discussion; `medium` (used when no effort is passed) or `low` for mechanical
implementation where the brief states the exact steps; `high` for complex implementation
where the brief gives a goal and the worker decides what to do and how. Never `max` or
`ultra`: the engine refuses both for this model. Claude workers use `high` or `xhigh`
unless the user names a level for that task: at `max` a small Claude model spent 63% of
a 40-minute turn in hidden reasoning and wrote nothing.

**Pi rules.** Always pass `--model` (an explicit model also becomes Pi's global
default, and omitting it inherits whatever was used last), plus `--engine pi
--provider opencode2api`. Pi has no `--max-model-steps`, `--output-schema`,
`--web`, or worktree flags, and Pi read-only has **no bash**. The gateway models are
free tiers: on a rate limit or an error, retry once, then pick another family and
mention it in your report.

## Host mechanics

The same protocol works in every host; only how you wait differs. Full protocol:
`docs/harness-integration.md`.

All paths must be absolute. The ID is the 12-hex value on the first output
line: `Worker <id> | label | path`. Exit codes everywhere: 0 completed, 1 failed,
3 needs a parent decision, 124 `--timeout` expired (still running), 130 cancelled.

- **Launch without blocking:** `claive start ...` (one turn),
  `claive open --detach ...` (reusable; returns at once),
  `claive batch start PLAN.json` (parallel lanes of stages). Never use `&`,
  `nohup`, or `sleep` polling loops.
- **Wait:** `claive wait ID [ID ...] --any --timeout S` returns the first worker
  to settle (with its report and code); on 124 do other work and wait again.
  Several IDs need `--any`: without it the wait exits 1 at once and waits for nothing.
  `--inbox CONSUMER` (IDs optional) also returns on new inbox events for that consumer.
  To check whether a worker still runs, read `claive show ID --json` status, not a
  `pgrep` pattern (your own shell's command line contains the pattern).
- **After a context reset or between turns:** `claive inbox --consumer <host>`
  lists every finished turn since your last read (`ASK` lines carry the
  question). Use one consumer name per parent.
- **Long tasks:** `claive mission new --title T --goal-file F`, then
  `export CLAIVE_MISSION=<id>` (or `--mission`) so launches link themselves;
  `mission note` decisions; `claive mission show ID` prints `Next:`; close it with
  `claive mission close ID`.
- **Questions:** a turn asking for a decision exits 3, shows `ASK`, and records
  `needs_parent`; reply with `claive answer ID --message ...` (reusable workers only).
- **Roles and reports:** `--role scout|worker|reviewer|oracle` sets engine, model,
  effort, read-only and step-cap defaults and requests a `claive-report` block;
  `--report` requests it without a role. `--output-schema /abs/schema.json` (Muse,
  Claude) makes the result schema-shaped JSON.
- Workers cannot launch workers (`CLAIVE_WORKER_ID` guard).

Host specifics:
- **Claude Code:** plain Bash for launches. Wait either in the foreground
  (`wait ... --any --timeout 540`, Bash `timeout: 600000`) or with
  `run_in_background: true` and no `--timeout` to be notified. Inbox consumer
  `claude`. Output of a `run_in_background` call goes to its task file; read that file.
  Foreground `sleep` is blocked: wait with the command above or use Monitor.
- **Codex:** follow `claive-routing-codex`. Prefer `claive-worker` in a managed shell
  with a short initial yield; retain both worker ID and shell session ID. Consumer
  `codex`.
- **Pi agent or shell:** the same commands through bash. Consumer
  `pi-<name>` or `shell`. Optional push: `CLAIVE_NOTIFY_CMD` gets the event JSON
  on stdin.

Common commands: `claive show ID --json`, `logs ID [--stderr]`,
`usage ID --json`, `followup ID --prompt-file F`, `close ID`, `cancel ID`.
Read output in bounded pieces: `claive logs ID --lines 40 | cut -c1-300` (raw JSONL,
one event can be several KB) and `claive show ID | head -n 80 | cut -c1-300` for the
final text. `show --json` has `status`, `exit_code`, `failure_kind`, parsed reports,
and separate verification/acceptance evidence. `claive doctor [--json]` is a read-only
health check that never launches a model (`--live` adds one tiny Pi turn).

## Plain workers: evidence and parent acceptance

Keep three claims separate: the supervisor records **execution**, the verifier
records **checks**, and the parent records **acceptance**. Exit 0 or a worker's
`done` report establishes only its own scope; neither grants acceptance.

For consequential work, declare checks and file ownership at launch
(`--checks-file /abs/checks.json`, repeated `--owned-path`), then `claive verify ID`
after the turn settles and `claive accept ID` as the parent; nothing is accepted
automatically. Rules that matter:

- Declare mandatory analyzers individually: a suite exiting 0 while skipping one
  does not prove it ran.
- One writer per isolated worktree (Muse: `--worktree`; other engines: an
  externally managed worktree as `--workspace`).
- A blocked or unrun mandatory check keeps acceptance pending; only an explicitly
  authorized parent exception (`--exception 'reason'`) resolves it. Failed mandatory
  checks cannot be excepted. Never invent an exception to obtain a green status.
- An ownership violation needs an explicit scope change
  (`claive scope ID --owned-path ... --reason ...`), not a worker widening its task.
- New turns start unverified and pending; later workspace edits make evidence stale.

Manifest format, check recording flags, snapshot rules and the reporting fields:
`docs/harness-integration.md`, "Execution, verification, and parent acceptance".

**Live steering.** Launch Pi, Codex or Claude with `--enable-steering`, then
`claive steer ID --message 'instruction' --wait 10 --json`. The instruction enters
after the current tool calls and before the next model call. An acknowledgement
proves the provider accepted it, not that the model complied. Muse rejects steering;
`followup` is a separate queued turn and `cancel` stops execution. Steering cannot be
combined with single-turn model fallbacks. Receipt states: `docs/steering.md`.

**Messages.** `claive send ID --message '...' [--mode note|wake|now] [--id HEX32] --json`
gives a reusable worker context without a new task. `note` (default) waits for its next
turn, `wake` starts a turn when it is idle, `now` steers and falls back to `wake`. The
message is context, not a change of assignment. A repeated `--id` returns the first receipt. Question a worker with `claive ask ID --message '...' --timeout 120` (0 answered, 124
withdrawn/expired, 3 needs the parent); it answers with `claive reply MSG_ID` or its final
text. `claive peek ID [--steps N] [--start N]` shows its numbered recent steps without
interrupting it. IDs accept a unique 4..11 character prefix.

**Workers talking to each other.** When two or more perspectives should react to each
other's reasoning (design debate, proposer/critic rounds, cross-checking a diagnosis),
open reusable workers, name them with `claive alias set NAME ID`, and run
`claive conversation start @a @b --message-file /abs/topic.md --rounds 2 --timeout 300
--json`. Each reply reaches the next worker as evidence, never as an instruction or a
grant of permissions. Not for independent subtasks (use a batch) or anything with an
executable check (use the ladder). Exit codes and recovery:
`docs/harness-integration.md`, "Coordinate a bounded conversation".

**Unattended queue.** `claive goal add` plus `claive serve` run goals with no parent
session; a parked goal shows as `goal ID ASK` in the inbox and is answered with
`claive goal answer ID --message ...`. Details: `docs/harness-integration.md`.

**Installed behavior disagrees with source?** Run `claive runtime --source-dir
/abs/claive --json` before theorizing about flags, and sync with
`install.sh --runtime-only`, not by editing installed files. On Pi-only hosts, respect
that host's engine policy and memory budget instead of the laptop's Muse roster.

## The verified ladder (`claive-orch`)

**Read `docs/orchestration-ladder.md` before starting a run.** It has the full
step table, the arbiter's rules, integration, and the quota fallback. In short:

```bash
claive-orch init --repo /abs/repo --task-file /abs/task.md \
  --verify 'python3 -m pytest -q tests/test_x.py' --arm D --rounds 2 --category bug-fix
claive-orch next RUN          # always ask; it prints NEXT / Why / How
```

- Lanes start from committed `HEAD` (or `--base REV`); uncommitted parent edits are
  not in them, and you do not commit just so lanes can see them.
- The verifier must fail at base; one that already passes makes the run invalid.
- The task file states goal, constraints, files and acceptance criteria, not the
  solution. Check every path, symbol and shell variable in it against the repo first.
- Loop on `next` and do exactly the one action it names. If `claive-orch` refuses a
  command, read its message and re-run `next`; never work around it with `--force`.
- The verifier picks the winner. A failing best is reported as `stalled` or
  `unresolved`, never as success.
- After `finish`: read the diff, then `claive-orch integrate RUN` (applies unstaged,
  re-verifies, never commits), then `claive-orch cleanup RUN --branches`.

## Parallel fan-out (independent subtasks)

For several independent tasks with **disjoint file ownership**, create one run
per task (each has its own lane worktrees) and drive them concurrently. For
unverifiable chores, use plain `claive open` workers, each in its own worktree.

- One writer per worktree. Read-only workers may share.
- No fixed cap on concurrent workers (the models run remotely), but run **one heavy
  job at a time** (tests, builds, verifiers), capped with `claive-memcap <N>G -- CMD`
  or `systemd-run --user --scope -q -p MemoryMax=<N>G -p MemorySwapMax=0 -- CMD`.
  Check `uptime` first, re-run timings taken under load, and kill only processes you
  started.
- Give each worker its scope, the files it owns, "do not commit, do not delegate,
  others are editing nearby", and the report format: outcome, files, checks run,
  doubts, blockers.
- Integrate one at a time and re-verify after each.

## Quota and failures

- Muse reports `Subscription quota exhausted` (a five-hour quota): stop assigning
  Muse work and use the **pre-approved fallback**, Pi
  `muse-spark-1.3-contributor-free` at `max`. Tell the user the reset time and that
  the fallback is in use; any other fallback model needs the user's approval. Moving
  a running `claive-orch` lane: `docs/orchestration-ladder.md`.
- A worker that fails (non-zero `wait`) is not success. Read
  `claive logs ID --stderr`, retry once only with a changed hypothesis,
  otherwise stop (`claive-orch finish RUN --abort --reason "..."` in a run) and report.
- Only `claive-orch verify` and your own re-verification count as proof.
- Never run worker tasks against production systems or real data.
