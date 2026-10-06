# Implementation Plan (revision 2)

**Goal:** A portable local experiment runner that executes coding tasks under several orchestration policies (arms) and produces comparable, inspectable evidence on whether verifier-gated refinement and selective breadth beat a single run and compute-matched resampling.

**Architecture:** See `01-design.md`. A TypeScript/Node.js control plane owns run state, budgets, worker lifecycle, workspaces, checkpoints, and the event log. Workers (Codex / OpenCode / Pi / Claude Code) sit behind one streaming adapter contract. A critic drives refinement. Verifiers decide correctness. Rules or Jev supply bounded signals to a pure deterministic arbiter.

**Tech stack:** TypeScript, Node.js, `node:test`, JSONL events, YAML config, Git worktrees for workspaces and checkpoints, one schema-validation library, one YAML parser.

## Why phases instead of a full build

Revision 1 built the whole control plane (17 tasks) before collecting any data. Agent runs vary a lot from run to run, so it is easy to build everything and then be unable to tell whether it helped. Revision 2 is **data-first**:

1. define the corpus and measure the single-worker baseline and its variance,
2. add one mechanism at a time,
3. compare each mechanism against the simpler arm at matched compute,
4. pass a **go/no-go gate** before building the next phase.

## Global constraints

- Follow every rule in `AGENTS.md`.
- No adapter code before `docs/04-webhmf-environment.md` exists.
- No comparative claims before `docs/05-experiment-protocol.md` exists and the baseline variance is recorded.
- The pure core (policy, budgets, event fold, config validation, selection rules) has unit tests under `node:test`. Adapters are covered by recorded smoke runs, not mocks.
- Experiment runs never touch production systems or real user data. Corpus repositories are disposable copies.

---

## Phase 0: Discovery, corpus, protocol (no runtime code)

### 0.1 WebHMF environment discovery → `docs/04-webhmf-environment.md`

For each of `codex`, `opencode`, `pi`, `claude`, record:

- version and the structured interfaces available (App Server / exec JSON / server API / ACP / RPC / SDK / stream-json),
- **session continuation** (can a follow-up message be sent to the same session after completion?),
- mid-turn steering and cancellation semantics,
- event-stream format, and how completion, tool calls, and errors appear,
- **usage reporting** (tokens, cost, quota) and its granularity,
- **nested subagent control** (can it be disabled? is subagent usage visible?),
- model selection and model-family options,
- workspace behavior (cwd handling, files written outside the repository).

Also record Jev API/SDK availability and auth mechanism (no secret values) and Node.js / Git versions.

Choose **one** primary integration surface per harness, plus a fallback only where observed behavior justifies it.

**Acceptance:** enough observed detail to write each adapter without guessing; no secrets or user-specific absolute paths in portable defaults.

### 0.2 Task corpus → `corpus/` + protocol section

- 20–40 tasks across categories: straightforward change, bug fix, multi-file feature, ambiguous debugging, refactor, and at least a few tasks **without** existing tests (to exercise held-out acceptance checks).
- Each task has a manifest: repository source, base revision, task text, verifier spec (command plus expected pass condition), category, and an estimated difficulty (for analysis only, never given to the router).
- Preferred sources: historical bug-fix commits from the user's own repositories (revert the fix, keep its tests as the verifier), plus a small public subset such as SWE-bench-style tasks if feasible on WebHMF.
- Every verifier is checked to **fail on the base revision and pass on the reference solution** before the task enters the corpus.

### 0.3 Experiment protocol → `docs/05-experiment-protocol.md`

- Arms (from `01-design.md` §15) and the order they are introduced.
- Repeats: k ≥ 3 per (task, arm).
- Isolation: every run starts from a fresh worktree at the base revision.
- Pinned harness and model versions per comparison batch; record them in every report.
- Compute unit for matching (tokens or wall-clock, depending on what 0.1 shows is reported).
- Analysis: paired per-task comparisons and success rate with intervals. State upfront that a small corpus supports exploratory conclusions only.
- Go/no-go criteria for every phase gate below, written **before** the data is seen.

**Gate 0:** discovery file exists, corpus verifiers validated, protocol written.

---

## Phase 1: Walking skeleton + baseline (arms A and R)

### 1.1 Bootstrap

`package.json`, `tsconfig.json`, `.gitignore` (run artifacts, worktrees, local config, env files), `config/example.yaml`, and a CLI `main.ts` with two commands: `run <task>` (one task, one arm) and `experiment <corpus> --arms ... --repeats k`. Typed errors.

### 1.2 Core types, events, budgets, policy skeleton

`core/types.ts`, `events.ts`, `budgets.ts`, `state.ts` (event fold), `policy.ts` (arbiter limited to the L0 actions for now). **Unit tests:** budget checks, event fold, legal and illegal transitions.

### 1.3 Config schema and loading

Validate workers, roles, ladder parameters, and budgets. Reject >2 candidates, D>3, unknown workers, and secret literals. **Unit tests** for validation.

### 1.4 Event store and run report

Append-only JSONL per run, monotonic sequence numbers, config snapshot event, and a terminal + JSON report including the verifier score, compute usage, and the arbiter reason trail.

### 1.5 Worktree workspace provider

Create a worktree from the base revision for each run, record the base revision and changed files, and never auto-delete a worktree with unreported changes after an abnormal stop.

### 1.6 First worker adapter

Implement the **one** adapter that 0.1 showed is simplest and most observable, as an event stream, with usage capture and nested subagents disabled.

### 1.7 Command verifier

Run the task's verifier spec in the workspace, parse a `{passed,total}` score where the runner exposes one (fall back to pass/fail), and bound and store its output.

### 1.8 Experiment runner, arms A and R

Arm A: one run, then verify. Arm R: two independent runs in separate worktrees; the verifier picks; ties are broken by the smaller diff and recorded as ties. Run the corpus with k repeats.

**Gate 1:** baseline success rate and **variance** recorded. If arm A already solves almost everything, the corpus is too easy and must be extended before going on. If R does not beat A at all, note it: the paper predicts weak resampling gains.

---

## Phase 2: Refinement (arms B and B0)

### 2.1 Checkpoint store

Commit per accepted round, tagged with the score; revert to the best checkpoint. **Unit tests** for acceptance rules (better, equal, and worse score).

### 2.2 Session continuation in the adapter

Implement `send` if 0.1 found resume support; otherwise use the fresh-session-on-checkpoint fallback and record it.

### 2.3 Critic

Structured `Critique` schema with validation, run on a cross-family descriptor where configured. Invalid output becomes `noConcreteDefect` plus an error event.

### 2.4 RulesProvider

Stuck detection (repeated failing command, no workspace change for T) and a repair-likely rule from critique flags. **Unit tests.**

### 2.5 L1 loop in the arbiter

CRITIQUE → CORRECT → VERIFY → ACCEPT/REVERT, the stall rules, and the D limit. **Unit tests** for every transition and stop condition.

### 2.6 Arms B and B0

B0 skips the critic and feeds only the verifier output to the correction. Run the corpus with compute recorded, and **re-budget R to B's median compute** for the comparison.

**Gate 2:** compare B against A and R (H1, H2) and B against B0 (H3), with the recovery rate per round. **If B does not beat R at matched compute, stop and reassess before Phase 3 or 4.** The core thesis has not held for coding, and adding Jev or breadth on top would not be justified.

---

## Phase 3: Jev decisions (arm C)

### 3.1 DecisionProvider interface plus JevProvider

Bounded questions over the compact projection, strict schema parsing, out-of-range values rejected, and fallback to RulesProvider on any error (recorded). Record a digest of the input projection and the parsed output for every call.

### 3.2 Arm C

Same as B, with signals from Jev. Record Jev calls, latency, and cost.

**Gate 3:** C against B (H4). If Jev does not improve success or cost, keep RulesProvider as the default and leave Jev as an optional provider. Do not add more Jev signals to rescue it without a specific observed failure.

---

## Phase 4: Second adapter and breadth (arms R' and D)

### 4.1 Remaining adapters

Add the other harnesses from 0.1 in order of expected value. Each conforms to the same contract and passes a recorded smoke run on two corpus tasks.

### 4.2 Diversity validation

Config validation rejects a breadth pair that differs only in temperature (except in arm R). **Unit tests.**

### 4.3 L2 in the arbiter

Second isolated candidate, independent of A, with its own L0→L1 loop (D_B ≤ 2), verifier-first selection, and an `unresolved` outcome. **Unit tests** for the selection rules.

### 4.4 Reviewer (tie only)

Structured preference or "no preference"; at most one call.

### 4.5 Arms R' and D

**Gate 4:** D against B on tasks where B stalled (H5), and D against R' (H6). Breadth is kept only if it recovers stalled tasks at an acceptable cost.

---

## Phase 5: Held-out acceptance checks, optional predictive entry, conclusions

### 5.1 Held-out acceptance checks

A test-author worker writes checks from the task text, which are stored outside the workspace, validated by requiring failure on the base revision, and copied in at verify time. Evaluate them on the corpus's no-test tasks, and on test-having tasks with the real tests hidden, to measure how well the generated checks agree with ground truth.

### 5.2 Arm E (optional)

Jev predictive entry, compared against D on cost and wall-clock.

### 5.3 Comparative report and documentation

`reporting/compare.ts` produces per-arm tables and paired comparisons. Update `README.md`, `00-rationale.md`, and `01-design.md` (revision log) with the results, the invalidated assumptions, and which next step, if any, the evidence justifies.

---

## Dependency order

```text
Phase 0  discovery + corpus + protocol       (gate 0)
Phase 1  skeleton, 1 adapter, arms A/R       (gate 1: baseline + variance)
Phase 2  refinement, arms B/B0               (gate 2: B vs R at matched compute; core thesis)
Phase 3  Jev, arm C                          (gate 3: Jev vs rules)
Phase 4  more adapters + breadth, arms R'/D  (gate 4: breadth after stall)
Phase 5  acceptance checks, arm E, report
```

Phases 3 and 4 are independent of each other once gate 2 passes, and may run in either order.

## Risk register

| Risk | Mitigation |
|---|---|
| Run-to-run variance swamps any effect | k ≥ 3 repeats, paired comparisons, baseline variance measured first (gate 1) |
| Paper's gains don't transfer to frontier coding agents | Compute-matched R arm; gate 2 stops the project early if refinement doesn't beat resampling |
| Refinement degrades working code | Checkpoints plus no-regression acceptance; the regressions count is reported |
| Self-critique misses the worker's own errors | Cross-family critic; the B0 ablation measures the critic's value |
| Threshold overfitting on a small corpus | Few signals; thresholds fixed in the protocol before runs |
| Workers game visible tests | Held-out acceptance checks stored outside the workspace |
| Hidden cost from nested subagents | Disabled during experiments, or counted and flagged |
| Correlated breadth candidates | Mandatory recorded diversity dimension; R' arm measures it |
| Harness interface drift | Phase 0 discovery; versions pinned per batch and recorded in reports |
| Cost unavailable on subscription logins | Tokens and wall-clock as the matching unit |
| Workspace interference (shared caches, ports, databases) | Worktrees per candidate; no claim of sandboxing; corpus repositories are disposable |
| Orchestrator overhead exceeds benefit | Each phase is gated; null results are reported, not hidden |

## Completion definition for the first experiment

1. The corpus and protocol exist, and the verifiers are validated.
2. Arms A, R, B, and B0 have k-repeat results with variance and matched compute.
3. Gate 2 is decided and documented.
4. If gate 2 passed: arms C, R', and D have results, and gates 3 and 4 are decided.
5. Every run has an event log and a report explaining every escalation.
6. At least two harnesses work through the same contract; Claude Code is supported but not required.
7. No secrets or workstation-specific auth in Git.
8. The documentation states which hypotheses held, which failed, and what, if anything, to build next.
