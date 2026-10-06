# AGENTS.md: Project Handoff Instructions

## Purpose

This repository defines an experiment for a **Jev-gated test-time-compute coding orchestrator**.

The project tries to answer one practical question:

> For frontier coding agents, does verifier-gated refinement, with selective two-candidate breadth only when refinement stalls, beat a single run and compute-matched resampling, and can cheap bounded decisions (Jev) control it better than simple rules?

Do not expand the project into a general agent platform unless experiment evidence demonstrates a concrete need.

## Current status

The repository is in the **planning phase (revision 2)**. No orchestration runtime has been implemented yet.

Planning workspace (WebHarness ARM):

```text
/home/ubuntu/repo/jev-ttc-orchestrator-experiment
```

The intended implementation workstation is the user's actual **WebHMF** environment. Do not guess WebHMF tool versions, paths, auth mechanisms, or process interfaces.

## Required reading order before implementation

Read these completely before writing runtime code:

1. `docs/00-rationale.md`
2. `docs/01-design.md`
3. `docs/02-implementation-plan.md`
4. `docs/03-research-references.md`

Phase 0 on WebHMF creates, before any runtime code:

5. `docs/04-webhmf-environment.md`: factual environment authority
6. `docs/05-experiment-protocol.md`: task corpus, arms, repeats, metrics

## Non-negotiable architecture rules

1. **Jev proposes bounded signals; deterministic code owns side effects.**
2. **Objective verification outranks model opinion.** It replaces the paper's majority vote as the aggregator.
3. **One worker is the default. Extra compute requires a recorded evidence reason.**
4. **Refine before resampling.** Depth (critique → correct → verify) is the first escalation; breadth comes only after refinement stalls, or in an explicit experiment arm.
5. **Refinement is critique-explicit, verifier-gated, and checkpointed.** A round that makes verification worse is reverted, never accepted.
6. **v0 breadth is at most two isolated candidates, and they must differ deliberately** (harness, model family, or strategy). Temperature-only resampling does not count as diversity.
7. **Every added mechanism is compared against a simpler arm at matched compute.** Without that comparison it is not evidence.
8. **The worker harness and the underlying model are separate identities.**
9. **Codex, OpenCode, Pi, and Claude Code are interchangeable workers.** None is an architectural dependency, and roles are never bound to a vendor.
10. **Nested subagents inside workers are disabled during experiment runs, or counted in the budget.**
11. **One append-only run history is the source for state and post-run explanation.**
12. **No provider credential, token, cookie, auth cache, or workstation secret belongs in Git.**
13. **Do not claim sandboxing unless a real isolation boundary exists.**
14. **No MCTS, PRMs, broad beam search, large Best-of-N, or multi-agent debate in v0.**
15. **The pure core (policy, budgets, router, event fold, config validation) has unit tests.**

## External investigation map

Use `docs/03-research-references.md`.

Primary references:

- arXiv 2608.05643, *Refining Over Resampling*: the depth-over-breadth thesis
- Jev / TypeSafe: bounded structured decisions
- JevRouter: capability routing
- Foreman: Jev-supervised Codex/OpenCode workers
- Claudexor: isolated candidates / cross-model review / repair
- JevLoop: deterministic runtime and projection boundaries
- Official Codex, OpenCode, Pi, and Claude Code harness docs

Do not rely on social-media summaries when the primary source is available.

## WebHMF implementation gate

Before implementing any real worker adapter on WebHMF, inspect and record:

- installed `codex`, `opencode`, `pi`, and `claude` versions and structured interfaces,
- Jev API/SDK/auth availability,
- cancellation semantics,
- session continuation (needed to correct work in the same session),
- steering semantics,
- structured event surfaces,
- token/usage reporting (many subscription logins report no dollar cost),
- how to disable or observe each harness's own subagents,
- session/workspace behavior,
- model-selection mechanisms.

Write only non-secret findings to `docs/04-webhmf-environment.md`. Do not begin adapter implementation until this file exists.

## Implementation sequence

Follow the phases in `docs/02-implementation-plan.md`. Each phase ends with a **go/no-go gate** based on recorded runs. Do not build the next phase's machinery if the gate's evidence says it is not worth it.

The first runnable milestone is a walking skeleton that produces baseline data:

```text
corpus task
  -> one real worker
  -> append-only events
  -> one verifier
  -> run report
  -> repeated k times per task to measure baseline variance
```

## Design discipline

When proposing a new subsystem, answer:

- What concrete failure, observed in run reports, does it solve?
- Could deterministic code solve it more cheaply?
- Is this new information or just another model opinion?
- Which experiment arm isolates its effect at matched compute?
- Does it introduce new side-effect authority?
- Does it preserve inspectable reasons for actions?

If the answer is unclear, keep the feature out of v0.

## Documentation discipline

If implementation evidence invalidates a design assumption:

1. update `docs/04-webhmf-environment.md` with observed facts,
2. update `docs/01-design.md` if the architecture changes,
3. update `docs/02-implementation-plan.md` if task sequencing/interfaces change,
4. append an entry to the revision log at the end of `docs/01-design.md` explaining the change rather than silently diverging.

## Source reuse

The external projects are **references, not dependencies by default**.

Before copying or adapting source code:

- inspect the exact current license,
- record whether usage is inspiration, dependency, or adapted code,
- preserve required attribution,
- avoid importing an entire control plane when only one pattern is needed.

## Experiment principle

The project succeeds if it produces a trustworthy comparison, even if the answer is negative. The paper reports shrinking gains for stronger models, so a null result for frontier agents is plausible and acceptable.

Core comparison (see `docs/01-design.md` §15):

```text
A  single worker
R  compute-matched resampling (two independent runs, verifier picks)
B  single worker + verifier-gated refinement, rule-based control
C  B with Jev decision signals instead of rules
D  full ladder: refinement then diverse breadth
```

More agent activity is not itself a success metric.
