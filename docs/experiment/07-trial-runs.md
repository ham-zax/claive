# 07: Live trial runs (shakedown, 2026-10-06)

These are the first end-to-end runs with real workers: Claude Code (Opus 5.5) as
the parent, Muse `muse-spark-1.3-contributor` as the implementer, and Pi
`nemotron-3-ultra-free` as the critic. They checked the mechanics. They are
**not** experimental evidence: one tiny task, no repeats.

Task: implement `parse_duration` / `format_duration` in a throwaway repository
with 6 visible unit tests. Arm D, rounds 2, experiment `trial`.

| Run | Setup | Result |
|---|---|---|
| `92a2e5a338f0` | Visible tests only | L0 passed (6/6), outcome verified, 55 s, 177k input tokens (137k cached) |
| `3017aa387a46` | Task text gave away the hidden requirement | Aborted by the parent and recorded as aborted (the abort path works) |
| `bd886bb32632` | Plus a held-out test (`format_duration(..., sep=...)`) not mentioned in the task, forcing L1 | L0 6/7, then the nemotron critic named the exact defect (localized, evidence = verifier), then the same Muse session corrected it, then 7/7 verified. 245 s, 668k input tokens (544k cached) |

## Findings and fixes

1. **`claive-orch prompt` had no sequence guard.** The parent (with a mistyped
   `critique` command) generated a correction prompt before the critique was
   recorded. `verify` correctly refused afterwards, but the correction had
   already been sent without the critique. Fixed: `prompt` now refuses any kind
   or lane other than the one `next` names. `--force` overrides it and records a
   warning in the run report.
2. **Held-out check names leaked.** Worker prompts showed the full `--verify`
   command, which named the hidden test module. Muse tried to run it, and the
   Pi critic made 2 failing `read` calls for the missing file. Fixed: when
   `--acceptance-dir` is set, workers see only `--worker-verify` (the visible
   command, if given) plus a note that held-out checks exist and are not in the
   workspace. The verifier *output* is still shown on failure, because that
   feedback is the point of refinement.
3. Tokens are dominated by cached input (Muse re-reads its session every
   step). Compare arms on total and on uncached input.
4. Free-tier Pi via `opencode2api` answered within about a minute and followed
   the fenced-JSON critic schema on its first attempt.

## Not yet exercised live

Revert of a regressing round, a stall leading to escalation to lane b (arm D
breadth), the tie reviewer, arms R/R'/B0, and the Muse quota path. These are
covered by unit tests with fake workers only.
