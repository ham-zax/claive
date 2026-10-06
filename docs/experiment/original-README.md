# Jev-Gated Test-Time Compute Orchestrator

Planning repository for a small, practical multi-agent coding experiment.

## Experiment question

Does **refining** a coding agent's work beat **resampling** it, and can a lightweight orchestration layer decide cheaply when to spend extra compute?

The layer combines:

- Codex / OpenCode / Pi / Claude Code as interchangeable coding workers,
- objective verification as the arbiter of correctness,
- a verifier-gated refinement loop (explicit critique, then correction, then verification) as the first escalation,
- a second, deliberately different candidate only when refinement stalls,
- Jev for cheap bounded decision signals, measured against a rules-only baseline,
- deterministic code for policy, budgets, and side effects.

The design follows *Refining Over Resampling* (arXiv 2608.05643), adapted from small-model math reasoning to frontier coding agents. See `docs/00-rationale.md` §"What we take from the paper" for what carries over and what doesn't.

## Current status

**Planning revised (revision 2); implementation has not started.**

This repository was created on WebHarness ARM. The implementation is designed to run on the actual WebHMF workstation, where the installed Codex / OpenCode / Pi / Claude Code interfaces must be inspected before writing worker adapters.

## Read in order

1. [Takeover and implementation rules](AGENTS.md)
2. [Why this exists, the paper, and what we test](docs/00-rationale.md)
3. [Architecture and experiment design](docs/01-design.md)
4. [Phased implementation plan](docs/02-implementation-plan.md)
5. [Research and investigation references](docs/03-research-references.md)

Created during Phase 0 on WebHMF, before any runtime code:

6. `docs/04-webhmf-environment.md`: observed workstation facts
7. `docs/05-experiment-protocol.md`: task corpus, arms, repeats, metrics

## Control loop: an escalation ladder

```text
task
  -> L0 DIRECT     one worker -> verify ---------------------------> pass: finish
  -> L1 REFINE     critique -> correct -> verify, up to D rounds
                   checkpoint every round, never accept a regression -> pass: finish
  -> L2 BREADTH    one deliberately different candidate in an isolated
                   workspace, which also gets refinement
                   -> select by verifier; reviewer compares only on a tie
  -> best verified state + inspectable run report
```

Escalation is driven by evidence: a failing verifier, a stuck worker, or a stalled refinement loop. Predicting difficulty upfront is an optional experiment arm, not the default.

## Architectural rule

> Jev answers bounded questions. Workers write code. Critics find concrete defects. Verifiers decide correctness. Deterministic code owns control flow.

## Source inspirations

This experiment borrows ideas, not entire control planes, from:

- [Refining Over Resampling (arXiv 2608.05643)](https://arxiv.org/abs/2608.05643)
- [JevRouter](https://github.com/BillionsBobby/JevRouter)
- [Foreman](https://github.com/thruwire/foreman)
- [Claudexor](https://github.com/razzant/claudexor)
- [JevLoop](https://github.com/parkavenue9639/jevloop)
- [awesome-agent-orchestrators](https://github.com/andyrewlee/awesome-agent-orchestrators)
- [awesome-jev](https://github.com/cobanov/awesome-jev)

See `docs/03-research-references.md` for canonical URLs, worker documentation, and open questions.
