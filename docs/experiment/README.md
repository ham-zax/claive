# TTC orchestration experiment

This directory is the experiment's home inside `claive`. It contains
the planning documents, the paper notes, and the account of how the plan runs
here: a frontier parent (Claude Code or Codex) follows a skill, launches Muse
and Pi workers through `claive`, and lets the deterministic arbiter
`claive-orch` decide each step.

## Provenance

Files `00`–`03`, `rules.md` (the original `AGENTS.md`) and
`original-README.md` are verbatim copies of
[ham-zax/jev-ttc-orchestrator-experiment](https://github.com/ham-zax/jev-ttc-orchestrator-experiment)
at revision 2, commit `54bf4bd` (2026-10-06). They describe a TypeScript control
plane that was never built. Read them as the specification. Where this
repository departs from them, `06-skill-driven-implementation.md` says how and
why. Their own relative links (for example `AGENTS.md`, `docs/04-...`) refer to
the original layout; in this directory those files are `rules.md`, `04-...`, and so on.

## Read in order

1. [paper-notes.md](paper-notes.md): *Refining Over Resampling* (arXiv 2608.05643), its numbers and what transfers to coding.
2. [00-rationale.md](00-rationale.md): why the experiment exists, hypotheses H1–H7, and the principles.
3. [01-design.md](01-design.md): the escalation ladder, components, stop conditions, arms (§15), and measures (§16).
4. [02-implementation-plan.md](02-implementation-plan.md): the data-first phases and go/no-go gates.
5. [03-research-references.md](03-research-references.md): references.
6. [rules.md](rules.md): the experiment's non-negotiable rules.
7. [04-webhmf-environment.md](04-webhmf-environment.md): the Phase 0 discovery for this workstation.
8. [05-experiment-protocol.md](05-experiment-protocol.md): corpus format, arms, repeats, compute matching, and gates. Draft: fix the thresholds before the first data.
9. [06-skill-driven-implementation.md](06-skill-driven-implementation.md): how the design maps onto `claive-orch`, the skills, and the workers, and what is not implemented.

## Where the runnable parts live

| Part | Path |
|---|---|
| Deterministic arbiter CLI | `bin/claive-orch`, `bin/claivelib/orchestration.py` |
| Arbiter tests | `tests/test_orch.py` |
| Worker manager (Muse, Pi) | `bin/claive`, `bin/claivelib/` |
| Daily orchestration skill | `skills/worker-orchestration/SKILL.md` |
| Experiment-running skill | `skills/ttc-experiment/SKILL.md` |
| Codex launch mechanics skill | `skills/subagent-routing/SKILL.md` |
| Corpus manifest example | `docs/experiment/corpus.example.json` |

Live shakedown results and the fixes they triggered: [`07-trial-runs.md`](07-trial-runs.md).
The hard sqlglot corpus (experiment x3): [`hard/README.md`](hard/README.md).
Why the next measurement uses a public benchmark, and how: [`08-public-benchmark.md`](08-public-benchmark.md).
