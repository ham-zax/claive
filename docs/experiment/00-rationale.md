# Jev-Gated Test-Time Compute Orchestrator Experiment

## Status

Planning repository, revision 2. No orchestration runtime has been implemented yet.

## Why this experiment exists

Modern coding agents can solve many tasks in one pass, but a single fixed execution policy wastes capability in two opposite ways:

1. **Easy work is over-served.** Expensive reasoning gets spent on triage, retries, and obvious decisions that could have been handled cheaply.
2. **Hard work is under-served.** A single run can commit early to one approach, repeat the same failure on retry, or stop without an independent challenge or objective verification.

Research on test-time compute shows that extra inference compute helps on hard tasks **when it is allocated well**. In particular, *Refining Over Resampling* (arXiv 2608.05643) shows that spending it on critique-and-correction of existing attempts beats spending it on more independent samples.

This experiment asks a narrower engineering question:

> For real coding work with frontier agents, does verifier-gated refinement, with selective breadth only when refinement stalls, beat a single run and compute-matched resampling, and can cheap bounded decisions control it?

The goal is **not** to reproduce an academic search engine. The goal is to find the simplest orchestration policy that is measurably better than today's single-agent default, or to show credibly that there is none.

## What we take from the paper

*Refining Over Resampling: Test-Time Self-Correction for LLM Reasoning* (Bilal et al., arXiv 2608.05643).

**Method.** Sample N independent rollouts (N=8, τ=0.7). Refine each through D rounds (D=4) of *continuation → self-critique → self-correction*, using role-conditioned prompts on the same model. Aggregate the refined answers by plurality vote, with no external verifier.

**Findings, and how this design uses each:**

| Paper finding | Design consequence |
|---|---|
| Resampling saturates: on AIME-24, unique semantic clusters grow only from ≈1.2 at N=2 to ≈2.5 at N=32. | Breadth is capped at **2** candidates, and the second must differ *deliberately* (harness, model family, or strategy), not by temperature. |
| Refining each rollout recovers incorrect answers across rounds. | **Depth is the first escalation.** A failing attempt is refined before anything is resampled. |
| Removing explicit critique hurts (Qwen2.5-1.5B: AIME25 6.67% → 0.0%; MATH500 58.0% → 55.6%). | Critique is a **separate stage with structured output** (concrete defects with evidence), not folded into "try again". |
| Self-correction is noisy for a single rollout; models can miss their own errors or make answers worse. Voting suppresses the noise. | Coding has no meaningful vote at N≤2, so the **verifier plus checkpoint revert** suppresses noise instead: a round that worsens verification is discarded. The critic should be a **different model family** where available, since self-critique misses the worker's own blind spots. |
| No monotonic best (N, D): AMC23 reaches the same score under (5,4) and (10,4). | N and D are **configuration**, not invariants. Defaults are small and measured. |
| Gains shrink for stronger models (≈17 points per 10³ TFLOPs for Qwen2.5-1.5B, η≈1.16 for Qwen2.5-Math-7B over RM@8). | Frontier coding agents may gain little. The experiment **must include compute-matched baselines** and accept a null result. |

**What does not transfer:**

- **Verifier-free aggregation.** The paper votes over closed-form math answers. Code diffs are not votable, but code usually *has* an executable verifier, which is stronger evidence than a vote. When a task has no verifier, we build held-out acceptance checks (see `01-design.md` §6.5) instead of falling back to opinion.
- **Scale of N and D.** In the paper a "rollout" is a single generation from a small open model. Here it is a full agent session that costs minutes and real money, so v0 uses N≤2 and D≤3.
- **Domain.** The paper evaluates math benchmarks with well-defined answers. Whether its findings hold for multi-file coding is exactly what this experiment tests.

## Core idea

The experiment separates five responsibilities that are often collapsed into one LLM call.

### 1. Coding workers

Mature agent harnesses do the real coding: **Codex, OpenCode, Pi, and Claude Code**, all through one adapter contract. None is an architectural dependency. The orchestrator distinguishes the **harness** from the **underlying model**; Pi, for example, can run several model families.

### 2. Critics

A critic inspects a candidate's diff and verification output and returns **concrete, localized defects with evidence**, or explicitly "no concrete defect found". This is the paper's critique stage, made structured. A critic proposes defects; it does not decide correctness.

### 3. Verification

When objective evidence exists, it outranks every model opinion: compilers, type-checkers, repository test commands, task-specific executable checks. A model saying "this looks correct" is not verification. Verification produces a **score** (for example, failing checks out of total), not just pass/fail, so refinement progress is measurable.

### 4. Bounded decisions (Jev)

Jev answers narrow questions cheaply: is the worker stuck, is a critic's defect localized enough to repair, is completion plausible when no verifier exists. Jev produces signals, never actions. Its value is **measured against a rules-only controller**, not assumed.

### 5. Deterministic runtime

Ordinary code owns allowed actions, budgets, round limits, process lifecycle, workspace isolation, checkpoints, state transitions, and stop conditions. Models propose; the runtime decides whether a proposal is allowed to happen.

## The execution policy: an escalation ladder

```text
L0 DIRECT
  one worker runs the task -> verify
  pass -> finish

L1 REFINE (depth; the paper's critique -> correct loop)
  critic finds concrete defects (from diff + verifier output)
  same worker session corrects them
  verify; keep it only if the score is no worse than the best checkpoint
  repeat up to D rounds (default 2, max 3)
  stop early on pass, no improvement, or "no concrete defect"

L2 BREADTH (only after refinement stalls)
  a second, deliberately different candidate in an isolated workspace
  it also gets up to D refinement rounds
  select by verifier; the reviewer compares only on a tie
  finish with the best verified state, or "unresolved"
```

Escalation is triggered by **evidence**: a failing verifier, a stuck worker, or a stalled refinement loop. Predicting difficulty from the task text and jumping straight to L1/L2 is an optional experiment arm (E), because predicting difficulty from a prompt is unreliable and the cascade is cheaper by construction.

## Why these design choices

### Why refine before resampling?

This is the paper's central result, and coding makes it stronger: a partially correct diff that fails one test holds a lot of valuable state. Throwing it away to resample discards the work and, per the paper's cluster analysis, often reproduces the same approach anyway.

### Why a verifier instead of voting?

With two candidates there is no meaningful majority, and diffs are not comparable answers. Tests and compilers give better evidence than any vote. Where they are missing, we construct them before the worker starts (held-out acceptance checks written by a different worker).

### Why checkpoint and never accept regressions?

The paper's limitations section notes that naive refinement can degrade answers, and single-rollout correction is noisy. A monotonic acceptance rule (keep the best verified checkpoint) makes refinement safe to try.

### Why a cross-family critic?

Models often fail to see their own errors. A critic from a different family adds new information instead of repeating the worker's blind spots. The worker still does the correction in its own session, so it keeps its context.

### Why Jev, and why measure it against rules?

The loop contains many frequent, bounded decisions. Spending a frontier call on each "stuck?" or "repair or restart?" defeats the economics of adaptive compute. But simple rules (the same command failing twice, no diff change in N minutes, the verifier score not improving) may already capture most of the signal. The experiment separates "the loop helps" from "Jev helps".

### Why not let Jev control processes?

A probabilistic model should not own irreversible control flow. The deterministic arbiter applies thresholds, budgets, permissions, and safety policy before any action, and the signal can be inspected separately from the policy that acted on it.

### What we borrow from each reference project

- **Foreman:** observe a worker, derive bounded checks, let deterministic policy act. This is the supervision shape.
- **JevRouter:** models, agents, and strategies as capabilities; availability and permissions stay deterministic.
- **Claudexor:** isolated candidates, cross-family review, bounded repair. This is the L2 shape, used only as escalation.
- **JevLoop:** one durable event history with purpose-specific projections and a deterministic kernel that owns execution truth.

## What this experiment is not

This repository is not intended to become:

- a general-purpose agent framework,
- a replacement for Codex, OpenCode, Pi, or Claude Code,
- a 32-rollout benchmark harness,
- an MCTS or learned-reward-model project,
- a multi-agent debate system,
- or a prompt-only orchestration trick.

It should stay small enough for one developer to understand the full control flow.

## Hypotheses

- **H1, refine over resample.** At matched compute, verifier-gated refinement (arm B) solves more tasks than two independent runs with verifier selection (arm R).
- **H2, refinement over single run.** Arm B beats a single run (arm A) by enough to justify its added cost.
- **H3, explicit cross-family critique matters.** Refinement driven by a structured critic beats refinement driven only by the raw verifier output. This is a secondary ablation.
- **H4, Jev adds value over rules.** Jev-controlled refinement (arm C) beats rule-controlled refinement (arm B) on success or cost.
- **H5, breadth after a stall helps.** Escalating to a deliberately different second candidate (arm D) recovers tasks that refinement alone could not.
- **H6, harness diversity matters.** Candidates that differ by harness or model family recover more stalled tasks than same-configuration candidates.
- **H7, evidence dominates.** No model opinion ever overrules a deterministic verifier result.

Each hypothesis maps to a pair of arms in `01-design.md` §15. A hypothesis without an arm comparison is not tested.

## Success criteria

The experiment is a success if it produces a **trustworthy comparison**. That means a fixed task corpus, repeated runs, recorded variance, and compute-matched arms, and it holds whether the result is positive or negative.

The approach is *promising* if an adaptive arm improves verified task success, or recovery from bad first attempts, without unacceptable regressions in:

- tokens / quota / dollar cost (whichever the harness reports),
- wall-clock time,
- destructive or invalid actions,
- explainability of "why the orchestrator did that".

## Portability and workstation boundary

This repository was created on WebHarness ARM, which is the planning workspace. Implementation and experiment runs happen on the user's **WebHMF** workstation. Therefore:

- planning must not assume ARM-specific absolute paths,
- worker adapters are configurable and built from observed interfaces,
- process invocation is isolated behind interfaces,
- provider credentials never enter the repository,
- environment discovery happens on the target workstation, not by guessing here.

## Architectural principles

1. **Models propose; deterministic code disposes.**
2. **Refine before resampling.**
3. **Verification decides; critics find; Jev signals; rules act.**
4. **Never accept a regression.**
5. **Spend extra compute only for a recorded evidence reason.**
6. **Diversity is deliberate, never just temperature.**
7. **Compare every mechanism against a simpler arm at matched compute.**
8. **Keep harness and model separate; bind no role to a vendor.**
9. **Keep the event history inspectable; make every escalation explainable.**
10. **Start with the smallest system that can falsify the hypotheses.**

## Sources

- Refining Over Resampling: https://arxiv.org/abs/2608.05643
- JevRouter: https://github.com/BillionsBobby/JevRouter
- Foreman: https://github.com/thruwire/foreman
- Claudexor: https://github.com/razzant/claudexor
- JevLoop: https://github.com/parkavenue9639/jevloop
- awesome-agent-orchestrators: https://github.com/andyrewlee/awesome-agent-orchestrators
- awesome-jev: https://github.com/cobanov/awesome-jev

These are references, not dependencies by default. Any code reuse is evaluated separately for licensing, maintenance cost, and fit.
