# Calibration corpus (10 tasks)

One-time, isolated calibration for the experiment in `../05-experiment-protocol.md`.
Ten tasks from the user's own `qap` repository: real fix commits whose tests become the verifier.
Exploratory only: 10 tasks support no more than that.

| Task | Category | Difficulty |
|---|---|---|
| qap-enum-metadata | bug-fix | easy |
| qap-basket-catalog | bug-fix | easy |
| qap-bridge-boundary | bug-fix | medium |
| qap-decimal-context | bug-fix | medium |
| qap-storage-neutral | change | easy |
| qap-attempt-rename | refactor | easy |
| qap-resolver-contract | feature | medium |
| qap-oos-seal | debugging | medium |
| qap-observation-invariants | feature | hard |
| qap-plan-authority | refactor | hard |

`difficulty` is for analysis only. Never show it, or the reference, to a worker.

## Isolation

- `build.sh` clones nothing live. It needs `~/corpus/qap` (a disposable clone with `uv sync --frozen`)
  and writes everything under `~/corpus` (override with `CORPUS_ROOT`).
- `~/corpus/tasks/<id>` is a one-commit repo: the fix's parent plus the fix's tests. Workers cannot
  find the fix in history. `~/corpus/_ref/<id>` has the same base plus the reference commit and is
  used only for validation.
- Verifier: `PYTHONPATH=src ~/corpus/qap/.venv/bin/python -m pytest -q ...`. `PYTHONPATH` makes the
  lane's own `src/` win over the venv's editable install. Each task takes seconds.
- No network, secrets, production systems or real data are involved.
- `build.sh` validates every task: the verifier fails at base and passes at the reference.
  Run it again after changing `tasks.json`. Hashes are deterministic.

## Before the first batch (ttc-experiment skill)

1. Fill the `TBD` gates in `../05-experiment-protocol.md` and date them. Suggested: Gate 1 A > 80%,
   Gate 2 B beats R by +10 points, Gate 4 recover 1 in 4 at 2x tokens. **Needs the user's decision.**
2. Record versions (`muse --version`, `pi --version`) and fix the batch models: implementer Muse
   `muse-spark-1.3-contributor` at `xhigh`, step cap 100; critic one of mimo / big-pickle /
   space-bunny at `max`; lane-b model and strategy text for arms R' and D.
3. Check Muse quota and that the chosen Pi models answer (skill section "Before the first batch").

## Overnight schedule

```bash
./plan.py calib1 A,R 3 20261007 > ~/corpus/calib1.tsv     # 60 runs; seed goes in the batch notes
```

Order follows the protocol gates: batch 1 is arms A and R (k=3), then B and B0, then R' and D, each
only after the previous gate passes. Per run (parent follows `claive-orch next` literally):

```bash
jq -r --arg t TASK '.[]|select(.id==$t)|.task' ~/corpus/calibration.json > /abs/scratch/TASK.md
claive-orch init --repo REPO --base BASE --task-file /abs/scratch/TASK.md --verify "$VERIFY" \
  --arm ARM --rounds 2 --experiment calib1 --task-id TASK --repeat K --category CATEGORY --verify-memory 2G
```

`repo`, `base`, `verify` and `category` come from `~/corpus/calibration.json`. Do not use
`--post-pass-critic` or `claive-orch pick` (both are refused in experiment runs). Run one at a time,
one verifier at a time. If Muse quota runs out, pause the batch and ask; the daily Pi fallback does
not apply inside a batch.

Afterwards: `claive-orch compare --experiment calib1`, then `claive-orch stats --experiment calib1`
gives the critic helpfulness and implementer pass rates that seed the daily `pick` choices.
