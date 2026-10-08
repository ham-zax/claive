# 08: Public benchmark instead of home-made tasks (proposal, 2026-10-08)

**Status: proposal.** Nothing here is built yet. Experiment x3 (`hard/`) keeps running as the pilot.
This document says why the next measurement should use a public benchmark, which one, and how to run
single-model and multi-model setups on it fairly.

## Why

Every corpus we built ourselves was solved by a single attempt, so it could not show what extra models add:

| Corpus | Single attempt (arm A) |
|---|---|
| calibration v1 (10 qap fixes) | 83%, then 100% (Gate 1 failed) |
| calibration v2 (18 qap tasks), x1 | space-bunny 30/30, R 29/29 |
| x2, same corpus | Haiku 5.5 2/3 before it was stopped as too easy |
| hard v1 (20 sqlglot fixes, tests visible) | Haiku 5.5 5/5 |
| hard v2 (tests hidden), x3 | running |

Home-made tasks have three problems:

- **Difficulty drifts.** A task is only as hard as the statement and the tests we write, and a failing
  test that names the expected output is most of the solution. Each fix of this costs a rebuild and a
  restart.
- **The answer isn't comparable.** "D beats A by 20 points on our 20 tasks" can't be set against anyone
  else's numbers, and an outside reader can't rule out that we tuned the tasks to the result.
- **Too few tasks.** With 20 tasks × 3 repeats, a gain from 40% to 60% is about 2 standard errors and
  less once repeats of the same task are counted as correlated. A clear answer needs 100+ tasks.

A public benchmark fixes all three: the tasks, tests and scoring are fixed by someone else, there are
hundreds of them, and the results sit next to published leaderboards.

## The question

On the same tasks, at a recorded token cost:

1. How does each free Pi model score alone (one attempt, and two attempts with test feedback)?
2. How much does a team add: implementer + critic, implementer + critic + reviewer, a second lane from
   another model family?
3. Does the team beat the **same budget spent on one model** (best of n)? Without this control a team
   result says nothing about the extra models. This is arm R in `05`.
4. Which configuration should `claive-orch pick` use by default, and is it worth its cost?

The answer is a table: configuration → pass rate with a 95% interval, median tokens, median wall time.

## Which benchmark

| Benchmark | Size | Fits this machine | Notes |
|---|---|---|---|
| **Aider Polyglot** (first) | 225 Exercism exercises in C++, Go, Java, JavaScript, Python, Rust | Yes: unit tests, no Docker, each test run takes seconds | Needs `go` and `cmake`, which are missing (`sudo apt install golang cmake`). Its published protocol has two attempts: `pass_rate_1`, then `pass_rate_2` after seeing the failing test output, which maps directly onto arms A and B0. Hard enough: frontier models score well below 100%. |
| SWE-bench Verified (second) | 500 real GitHub issues, 12 Python repos | A 50–100 task subset only | One Docker image of several GB per task; Docker is not installed and WSL has ~7 GB free. The standard citation for agentic coding. |
| Terminal-Bench | ~100 terminal tasks | Needs Docker | Later, if Docker is set up. |
| ARC-AGI | Abstract puzzles | — | Not code, not tool use; a critic/reviewer setup doesn't map onto it. |

**Contamination.** Public benchmarks are probably in the models' training data, so absolute scores may be
inflated. Comparisons between configurations of the same models are still valid, and that is the question
here.

## Fairness rules

- The benchmark's own protocol decides what the model sees. For Polyglot, the hidden test result is shown
  only where the protocol allows it (the second attempt). A critic or reviewer sees only what the
  implementer could see. That keeps the numbers comparable with the leaderboard.
- Every configuration runs on the same tasks with the same seed. Comparisons are paired by task
  (McNemar test, bootstrap intervals over tasks).
- Every configuration gets a matched-budget control: one model, the same number of attempts.
- Infrastructure failures (gateway 429/503, timeouts with no model output) are re-run and never scored as
  model failures. This is the rule `drive.sh` already follows.
- Models and configurations are fixed before a batch starts, as in `05`.
- **No network except the model gateway.** In x3 v2 the implementer fetched the upstream fix from GitHub
  in all 4 runs it passed (`~/corpus/FINDINGS.md`, item 12). Exercism and SWE-bench solutions are public
  too. Workers run where only the gateway host is reachable (a firewall rule or network namespace; a
  one-time root setup), and the post-run audit still checks every tool call.

## Design: screen, then combine

All 10 free models × every role is thousands of runs, so the search goes in stages:

1. **Screen.** Each available Pi model alone, one attempt plus one test-feedback attempt, on a fixed
   random 60-task subset. Rank the models.
2. **Pairs.** The top 3 implementers × the top 3 critics (arm B), each with its matched control (R and B0),
   on the same 60 tasks.
3. **Teams.** The best pairs plus a reviewer and a diverse second lane (arm D).
4. **Confirm.** The best team against the best single model with the same budget, on all 225 tasks.

Models excluded as in x3: Muse (production), `nemotron-*`, `ling-3.1-flash-free`.

## Capacity

The opencode2api models are free and treated as unlimited, so the plan runs 5–7 workers at once spread
across models. Two observations temper that:

- In x1 the gateway returned `503 pool_backpressure` above 2–3 concurrent requests, and space-bunny hit
  `429 FreeUsageLimitError` after its daily free quota.
- Work spread across models avoids most of the per-model limit.

So the runner measures before it scales: it starts with a short concurrency probe per model, then
adjusts each model's concurrency during the run (more when calls succeed, fewer after a 429 or 503). A
quota stop pauses that model's queue; it doesn't fail runs. Polyglot test runs are light, so RAM is not
the limit at 7 workers; x3's whole-suite runs are (about 1.9 GB each), so a large benchmark batch waits
until x3 is done or runs with fewer workers next to it.

## Output

- A results database (`sqlite`, one row per attempt: task, configuration, models, outcome, tokens,
  time) and a report with the pass-rate table, paired comparisons and cost.
- The winning configuration becomes the default for `claive-orch pick` (implementer, critic, when to add
  a second lane), replacing the heuristics in `06`.
- A write-up of the measured result, with the method, so others can reproduce it.

## Next steps

1. Install `go` and `cmake` (needs root), fetch the Polyglot exercises, check that every reference
   solution passes and every stub fails.
2. Write the harness: a task adapter (exercise → `claive-orch init` manifest with the benchmark's test
   command as `verify`), the adaptive per-model scheduler, and the results database.
3. Pilot: 5 models alone on 20 tasks, to check the harness and measure gateway capacity.
4. Run stages 1–4.
