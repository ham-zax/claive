---
name: ttc-experiment
description: Run the controlled "refine vs resample" test-time-compute experiment (arXiv 2608.05643 applied to coding agents) with the claive-orch arbiter and Muse/Pi workers. Covers corpus validation, arms A/R/R'/B/B0/D, k repeats, fixed per-batch models, token accounting, `claive-orch compare`, pre-registered gates and batch notes. Use only when the user asks to run, continue, or analyse the orchestration experiment or a batch of it; for everyday delegated work use worker-orchestration instead.
---

# TTC experiment (refine vs resample on coding tasks)

This skill turns you into a **careful experimenter**. The hypotheses, design and
protocol are in `/home/hamza/repo/claive/docs/experiment/`. Read
these before the first batch of a session:

- `README.md`: index, provenance, what is runnable.
- `paper-notes.md`: what the paper shows and what it does not show.
- `01-design.md` §15–16: arms, hypotheses H1–H7, measures.
- `05-experiment-protocol.md`: corpus, repeats, compute matching, **gates**.
- `06-skill-driven-implementation.md`: how `claive-orch` maps to the design, and
  the deviations.

Per-run mechanics (launching workers, the `next` loop, host specifics) are
identical to the `worker-orchestration` skill. Load it too and follow its
"verified ladder" table. This skill adds the experimental discipline.

## Non-negotiables

1. **Follow `claive-orch next` literally.** Do not skip, reorder, improvise, or
   add a lane or round. You are the measuring instrument; any judgement you add
   contaminates the arm. If you must deviate (a crash, a gateway outage), record
   it in the batch notes and finish the run with
   `claive-orch finish RUN --abort --reason "..."`.
2. **Register every worker** with `claive-orch worker` before waiting on it.
   Unregistered workers are invisible to the report: that is the main threat to
   validity (see `06` deviation 1).
3. **Fixed per batch:** implementer engine, model, effort and step cap; critic
   model; lane-b engine, model and strategy text; rounds D; harness versions. No
   critic rotation within a batch unless critic diversity is the variable under
   test. A free gateway model that fails is an incident, **not** a reason to swap
   models silently. Pause and ask the user. Defaults: Muse at `xhigh`, every Pi
   model at `max`, Pi models from `mimo-v2.6-flash-free`, `big-pickle`,
   `space-bunny-free` (rarely `longcat-2.5-preview-free`; never `nemotron-*` or
   `ling-3.1-flash-free`).
4. **Never reveal** `difficulty`, the reference commit, other arms' results, or
   held-out checks to any worker. Task text is the manifest's `task` field
   verbatim.
5. **Fresh everything per run:** a new run, new lane worktrees at `base`, new
   worker sessions. Never reuse a worker across runs.
6. Corpus repos are **disposable clones**. Never production systems or real data
   (for example, never the FMGE/LMS site or its database).
7. **One verifier at a time**, at most 2–3 concurrent workers (about 7 GB RAM).
   Running runs sequentially is the default.
8. Do not edit thresholds in `05` after seeing data. Changes go in a dated
   amendment with a reason.

## Before the first batch

- Check that every `TBD` gate in `05-experiment-protocol.md` has been filled in
  by the user. If not, **ask the user** for the values (suggested values are
  listed) and record them with the date. No recorded batch without fixed gates.
- Record versions: `muse --version`, `pi --version`, your host version,
  `python3 --version`, `git --version`. Compare them with `04-webhmf-environment.md`.
- Check that Muse quota is available and the chosen Pi models answer. Per model,
  `echo 'Reply OK' > /abs/scratch/ping.md` and then `claive run --engine pi
  --provider opencode2api --model M --reasoning-effort max --read-only --workspace /abs/scratch
  --prompt-file /abs/scratch/ping.md` is enough.

## Corpus validation (once per task, before it enters the corpus)

Manifest format: `docs/experiment/corpus.example.json`. For each task:

```bash
git -C REPO worktree add --detach /abs/scratch/ref REFERENCE
(cd /abs/scratch/ref && bash -c "$VERIFY"); echo "reference exit $?"   # must be 0
git -C REPO worktree remove --force /abs/scratch/ref
```

`claive-orch init` itself refuses a base where the verifier already passes, and
records the base score. If `acceptance_dir` is set, run the reference check with
those files copied in temporarily, then remove them. Drop tasks that fail
validation and note why.

## One run

```bash
printf '%s\n' "$TASK_TEXT" > /abs/scratch/TASK_ID.md
claive-orch init --repo REPO --base BASE --task-file /abs/scratch/TASK_ID.md \
  --verify "$VERIFY" --arm ARM --rounds 2 \
  --experiment BATCH --task-id TASK_ID --repeat K \
  [--score-regex RE] [--acceptance-dir DIR --worker-verify VISIBLE_CMD]
# first output line: "Run <RUN> | arm ..."
```

Then loop `claive-orch next RUN` exactly as in `worker-orchestration`, using the
batch's fixed models. Arm-specific lane setup (`claive-orch` enforces these):

| Arm | Lanes | Critic |
|---|---|---|
| A | a: Muse | none: one turn, verify, finish |
| R | a and b: **identical** Muse config (resampling) | none |
| R' | a: Muse; b: the batch's Pi model (a different family), with the strategy text | none |
| B | a: Muse | the batch's Pi critic |
| B0 | a: Muse | none: the correction prompt carries only verifier output |
| D | a: Muse; b only when `next` says `add_lane b` | the batch's critic; lane b uses its own critic per the batch notes |

After each run: `claive-orch finish RUN` (if `next` says so), `claive-orch usage RUN`
(snapshots tokens; do it **before** closing workers), close reusable workers,
`claive-orch report RUN`, `claive-orch cleanup RUN`. Do **not** integrate
experiment diffs into the corpus repos.

## Schedule

- k ≥ 3 repeats per (task, arm).
- Interleave: iterate repeat → task → arm (shuffled once per batch, recorded),
  not arm-by-arm, so gateway drift and quota windows do not line up with an arm.
- Introduce arms per the plan's gates: **A and R first**, then **B and B0**, then
  **R' and D**. Stop at a failed gate (below).
- Muse quota exhausted: pause the batch, report the reset time, and ask the user.
  The daily-use Pi fallback (`muse-spark-1.3-contributor-free`) does **not**
  apply inside a batch: it changes the implementer, which is fixed per batch.

## Analysis

```bash
claive-orch list --experiment BATCH
claive-orch compare --experiment BATCH          # markdown; --json for raw
```

`compare` gives, per arm: runs, verified rate with a 95% Wilson interval,
recovered after an L0 failure, median seconds and tokens; and the per-task
verified/runs grid for **paired** comparison.

Apply the gates from `05` mechanically:

- **Gate 1:** A's verified rate above the threshold means the corpus is too easy.
  Add harder tasks and do not continue.
- **Gate 2 (core):** B vs R, paired by task, at matched compute (R's median
  tokens ≥ 0.8× B's, or the recorded ratio). **If B does not beat R, stop and
  report the null result. Do not add breadth.**
- **H3:** B vs B0, always reported (does the critic add anything over verifier
  feedback alone? The paper's ablation says it should).
- **Gate 4:** among runs where B stalled, D's recovery rate and token cost
  against the thresholds; compare with R' (H6: deliberate diversity versus
  diversity without refinement).

Write-up for the user: a batch-notes header (template in `05`), the compare
tables, gate verdicts, incidents and deviations, and an explicit sentence that a
corpus this small supports **exploratory** conclusions only. Relate findings to
the paper (`paper-notes.md`): depth beat width on hard math at matched compute,
the critic ablation hurt, and semantic diversity of resamples saturated.
Do not overclaim transfer to coding.
