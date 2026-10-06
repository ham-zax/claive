# 06: Skill-driven implementation

The plan in `01`/`02` specifies a TypeScript control plane that owns the worker
processes. This repository builds the same control loop from smaller parts:

```text
frontier parent (Claude Code / Codex)   follows skills/worker-orchestration or skills/ttc-experiment
  │  plans, writes prompts it is told to write, launches workers, integrates
  ├── claive-orch     deterministic arbiter: run history, verification, checkpoints,
  │                  budgets, legality, and the ONE next action (`claive-orch next`)
  └── claive  worker lifecycle for Muse and Pi (open / wait / followup / close / usage)
```

The design principle "models propose; deterministic code disposes" holds because
the parent never decides acceptance, reverts, round limits, escalation, or
selection. It asks `claive-orch next`, performs the one allowed action, and
reports the result back with `claive-orch verify|critique|review`. `claive-orch`
refuses out-of-sequence commands: verifying while a critique is due, a critique
under arm B0, adding a lane the ladder did not ask for, a second lane that is not
diverse, a same-family critic, or finishing early without `--abort`.

## Mapping to the design

| Design (`01-design.md`) | Here |
|---|---|
| §5 escalation ladder L0/L1/L2 | `decide()` in `orchestration.py`, a pure function over folded events, with unit tests |
| §6.2 WorkerAdapter | `claive` engines (`muse`, `pi`). Continuation uses `followup` on the same session |
| §6.3 Critic, structured output | `claive-orch prompt RUN critique LANE` (diff and verifier output inline), plus `claive-orch critique` (fenced-JSON extraction and validation; invalid output becomes no-concrete-defect plus an error) |
| §6.4 DecisionProvider (rules) | The rules are code in `lane_status()`: pass, round limit, 2 non-improving rounds, no concrete defect, and repair-unlikely (non-localized defect or unsound approach) escalating lane a under arm D |
| §6.4 DecisionProvider (Jev) | **Not implemented** (arm C). No Jev access on this workstation |
| §6.5 Verification, score | `run_verifier()`. It parses passed/total for unittest, pytest, jest, vitest, node:test/TAP, and cargo, or uses `--score-regex`, falling back to pass/fail. Output is stored in the run directory and bounded in prompts |
| §6.5 held-out acceptance checks | `--acceptance-dir`, outside the repository, copied in only while the verifier runs. The test-author worker that writes the checks is a manual step |
| §6.6 Checkpoint store | Git: each lane is a worktree on branch `orch/<run>/<lane>`. An accepted round commits; a worse round runs `reset --hard` to the best checkpoint plus `clean -fd`. Branches survive `cleanup` |
| §6.7 Reviewer | `claive-orch review`, used only on a verification tie between two lanes under arm D, once |
| §6.8 Deterministic arbiter | `decide()` plus the per-command guards |
| §7 event history | `runs/<id>/events.jsonl` (append-only, sequence-numbered, flock-protected). State is always re-folded from it |
| §9 budgets | Rounds (D ≤ 3, breadth D_B ≤ 2), ≤ 2 candidates, an optional `--max-minutes`, and verifier timeout. Token budgets are measured (`usage`), not enforced |
| §10 stop conditions | `lane_status()` and `finish()` outcomes: verified, stalled, unresolved, budget, aborted, failed, plus invalid when the base already passes |
| §11 selection | Verifier first, then the reviewer's preference, then the smaller diff. Ties are recorded |
| §15 arms | A, R, R', B, B0, D implemented. C and E not implemented |
| §16 measures | `claive-orch report --json` per run, `claive-orch compare --experiment` across runs |

## Deliberate deviations

1. **Parent-in-the-loop instead of an owned process supervisor.** The parent LLM
   launches workers, so a misbehaving parent could skip a step. `claive-orch`
   prevents illegal *recorded* steps but cannot stop the parent from running a
   worker it never registers. The skills make registration mandatory, and the
   report lists only registered workers. This is the main threat to validity
   in experiment runs, so the experiment skill makes the parent follow `next`
   literally and log deviations.
2. **Cross-family critic by default.** The paper uses the same model. See `paper-notes.md`.
3. **Round 0 revert.** A first attempt that scores below the base revision is
   reverted to base, never accepted. Refinement then starts from base with the
   session's context intact.
4. **Muse's own worktree mode is not used for lanes.** Lanes need to outlive one
   turn and carry checkpoint commits, so `claive-orch lane` creates them, and
   workers run with `--workspace <lane path>`.
5. **Token matching is post hoc.** R is capped at 2 candidates and compared to B
   where its median tokens are comparable (see `05`).

## Run directory

```text
${CLAIVE_DIR:-~/.local/state/claive}/runs/<run-id>/
  events.jsonl      append-only history (source of truth)
  task.md           frozen copy of the task text
  prompts/          every generated prompt
  verify/           verifier output per lane and round (base.txt = base revision)
  lanes/a, lanes/b  git worktrees (removed by cleanup; branches kept)
```

## Not implemented / future work

- Arm C (Jev signals) and arm E (predictive entry). Adding them means a decision
  provider that only *adds signals* folded into `lane_status()`, never actions.
- Stuck detection inside a turn (repeated failing command, no diff for T minutes).
  The parent can observe this with `claive logs` and `cancel`, but the
  arbiter does not see it yet.
- A test-author worker for held-out checks.
- Enforced token budgets (they need live usage while a turn is running).
- Codex, OpenCode, and Claude Code as *workers*. They are parents here. A new
  engine in `bin/claivelib/engines/` would make them workers.
