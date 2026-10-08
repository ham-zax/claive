# 05: Experiment protocol (draft)

Phase 0.3 of `02-implementation-plan.md`. **Status: draft.** Before the first
recorded batch, fill in every `TBD`, record the date, and do not change the
thresholds after seeing data. If a change is unavoidable, append a dated
amendment that gives the reason.

## Corpus

- 20–40 tasks across these categories: straightforward change, bug fix,
  multi-file feature, ambiguous debugging, refactor, and a few tasks without tests
  that use held-out checks.
- Preferred source: historical bug-fix commits from the user's repositories.
  Revert the fix and keep its tests as the verifier. Corpus repositories are
  **disposable clones**, never live checkouts, production systems, or real data.
  Example: never point a task at the production FMGE/LMS site or its database.
- The manifest is a JSON list. See `corpus.example.json`:

```json
{
  "id": "calc-mul",
  "repo": "/abs/path/to/disposable/clone",
  "base": "<commit with the fix reverted>",
  "reference": "<commit with the fix>",
  "task": "Fix mul() so test_calc passes. Do not change the tests.",
  "verify": "python3 -m unittest -q",
  "category": "bug-fix",
  "difficulty": "easy",
  "acceptance_dir": null,
  "score_regex": null
}
```

- `difficulty` is used only in analysis and is never shown to the workers or the parent.
- **Validation:** before a task enters the corpus, its verifier must fail at
  `base` and pass at `reference`. `claive-orch init` refuses a base that already
  passes. Check the reference by hand with `git worktree add` at `reference`
  and run the verifier there.
- Held-out checks (`acceptance_dir`) live outside the repository and are copied
  into the lane only while the verifier runs. Workers never see them.

## Arms

| Arm | `claive-orch` arm | Notes |
|---|---|---|
| A | `A` | Muse implementer, one turn, verify once |
| R | `R` | Two Muse lanes with the same configuration, verifier picks |
| R' | `R'` | Muse lane a plus a Pi lane b of a different family; verifier picks |
| B | `B` | Muse implementer, cross-family Pi critic, D = 2 |
| B0 | `B0` | B without the critic; the correction prompt carries only verifier output |
| D | `D` | B, then after a stall one diverse lane b (Pi family ≠ critic family where possible), D_B ≤ 2 |
| C, E | not implemented | No Jev access here (see `04`) |

Order of introduction follows the plan's gates: A and R first, then B and B0,
then R' and D.

Fixed per batch (record them in the batch notes):

- Implementer engine and model, reasoning effort, and step cap.
- Critic model, chosen once from a family different from the implementer's.
  Do not rotate critics within a batch unless critic diversity is the variable
  under test.
- Arm D lane-b engine, model, and the strategy text.
- Rounds D (default 2).
- Harness versions (see `04`).

## Repeats and isolation

- k ≥ 3 repeats per (task, arm). Pass `--repeat 1..k`.
- Every run starts from a fresh lane worktree at `base` (`claive-orch lane`).
- Use a new worker session per run. Never reuse a worker across runs.
- Run at most one run's verifier at a time (the machine has about 7 GB RAM).
  Interleave arms across tasks so that gateway drift does not line up with one arm.

## Compute matching

- **Unit:** total tokens (input + output) from `claive-orch usage`, with
  wall-clock as the secondary unit. There is no dollar cost.
- R and R' are capped structurally at 2 candidates. After B has been run on the
  task set, compute B's median tokens per task. H1 is only claimed where R's
  median tokens are at least 0.8× B's (ratio 0.8 confirmed 2026-10-07). Otherwise report
  the comparison as "R under-budgeted" and do not count it as evidence.
- Runs whose token usage is unknown are reported as unknown, not as zero.

## Measures

Recorded in each run's `events.jsonl` and `claive-orch report --json`:

- arm, outcome (`verified`, `stalled`, `unresolved`, `budget`, `aborted`, `failed`)
- verifier score trajectory per lane, rounds, and reverted regressions
- candidates, critic families, and review calls
- tokens per worker and wall-clock
- warnings, including same-family critic overrides

Aggregated with `claive-orch compare --experiment NAME`:

- verified rate per arm with a 95% Wilson interval
- recovered-after-L0-failure (the analogue of the paper's per-depth recovery)
- per-task verified/runs grid for **paired** comparisons
- median tokens and seconds

A corpus this small supports exploratory conclusions only. Say so in every write-up.

## Gates (fix before data)

- **Gate 1 (baseline):** record A's verified rate and its variance across
  repeats. If A verifies more than 80% (fixed 2026-10-07) of tasks, the corpus is
  too easy: add harder tasks before continuing.
- **Gate 2 (core thesis):** B must beat R in paired per-task comparisons at
  matched compute, by at least +10 points (fixed 2026-10-07; suggested +10 points verified rate with a
  non-overlapping or nearly non-overlapping interval, or a clear majority of
  tasks where B ≥ R). **If B does not beat R, stop and report the null result.
  Do not add breadth.**
- **H3:** B vs B0 is reported regardless of the outcome. It is an ablation, not a gate.
- **Gate 4 (breadth):** among runs where B stalled, D must recover at least 1 in 4 (fixed 2026-10-07)
  at a median token cost no more than 2× B (fixed 2026-10-07).
  Compare D with R' (H6).

## Batch notes template

```text
batch: <name> date: <YYYY-MM-DD> experiment: <claive-orch --experiment>
versions: muse <v> pi <v> parent <claude|codex v>
implementer: <engine/model/effort/steps>  critic: <pi model>  breadth: <engine/model + strategy>
rounds D: <n>  repeats k: <n>  corpus: <manifest path @ commit>
deviations / incidents:
```
