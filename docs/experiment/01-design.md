# Architecture and Experiment Design

## 1. Design objective

Build the smallest orchestration control plane that can answer:

> For frontier coding agents, does verifier-gated refinement, with selective two-candidate breadth only after refinement stalls, beat a single run and compute-matched resampling, and does Jev control it better than simple rules?

The system is an **experiment runner** first. It has a clean path to becoming a reusable local orchestrator only if the experiment justifies that.

## 2. Design constraints

- Codex, OpenCode, Pi, and Claude Code are interchangeable workers behind one contract; none is required.
- Worker harness choice is separate from underlying model choice.
- Jev may score or recommend; it may not execute side effects.
- Deterministic policy owns budgets, round limits, process control, workspace isolation, checkpoints, and stop conditions.
- Objective verification outranks critic and reviewer opinion.
- Depth (refinement) comes before breadth (a second candidate).
- Refinement never accepts a verification regression.
- Breadth is at most two candidates, deliberately different.
- No MCTS, large Best-of-N, learned reward models, or debate.
- Every escalation is inspectable after the run.
- Every mechanism has a compute-matched comparison arm.
- The control plane is portable from WebHarness ARM to the WebHMF workstation.

## 3. Technology shape

**TypeScript on Node.js**, as a small local CLI.

- Pi exposes a TypeScript SDK; Claude Code has a TypeScript Agent SDK; Codex and OpenCode expose structured process/server interfaces.
- Typed decision and event schemas are straightforward.
- No workflow engine. Node built-ins handle subprocesses and CLI parsing; `node:test` handles unit tests.
- Jev access sits behind a `DecisionProvider` interface, so no specific SDK is required.

## 4. System overview

```text
                         CORPUS TASK / USER TASK
                                   |
                                   v
                        +---------------------+
                        |   Task Normalizer   |  -> TaskEnvelope (+ verifier spec)
                        +----------+----------+
                                   |
                                   v
                        +---------------------+
                        | Verifier Preparation|  existing checks, or held-out
                        +----------+----------+  acceptance checks (§6.5)
                                   |
                                   v
     +-----------------------------------------------------------+
     |  DETERMINISTIC ARBITER (policy, budgets, legal actions)   |
     |                                                           |
     |   L0 DIRECT ---fail/stuck---> L1 REFINE ---stall---> L2 BREADTH
     |      |                         |  critic -> correct      |  2nd diverse
     |      |                         |  -> verify -> keep best |  candidate,
     |      |                         |  checkpoint             |  also refined
     |      v                         v                         v
     |   Worker adapters (Codex / OpenCode / Pi / Claude Code)   |
     |   Critic   Verifier   Checkpoint store   Reviewer (tie)   |
     |                                                           |
     |   Decision signals: RulesProvider | JevProvider           |
     +---------------------------+-------------------------------+
                                 |
                                 v
                 append-only event log -> run report
```

## 5. The escalation ladder

### 5.1 L0 DIRECT

```text
start worker -> stream events -> (supervision checkpoints) -> worker claims completion
  -> verify -> pass: FINISH
            -> fail/partial: ESCALATE to L1
```

Supervision during L0 is coarse. It reacts to a command failing repeatedly, no diff change for a configured interval, or the worker asking a question, and it can STOP a stuck worker early. Every task starts here unless an experiment arm says otherwise.

### 5.2 L1 REFINE (depth)

This is the paper's *critique → correct* loop, made verifier-gated.

```text
for round in 1..D:
    critique  = Critic(task, diff vs base, verifier output, previous critiques)
    if critique.noConcreteDefect: STOP (stalled)
    correct   = same worker session, given critique + verifier output
    score     = Verify(candidate)
    if score > best: checkpoint(candidate); best = score
    elif score == best: keep the candidate; count as no-progress
    else: revert to best checkpoint; count as regression
    if pass: FINISH
    if no-progress or regression twice in a row: STOP (stalled)
-> stalled: ESCALATE to L2 if allowed, else FINISH with best verified state
```

Defaults: `D = 2`, hard maximum `3`.

Key properties:

- **Explicit critique.** Corrections are driven by named defects, not by "try again". The paper's ablation shows critique matters.
- **Cross-family critic** where available (the critic uses a different model family than the worker). If none is available, the critic falls back to the same family, and the report says so.
- **Same-session correction.** The worker keeps its context. If the harness cannot resume a session, it gets a fresh session on the checkpointed workspace plus the critique, and the report records this.
- **Monotonic acceptance.** The workspace never ends worse than the best verified checkpoint.

### 5.3 L2 BREADTH

```text
create isolated workspace B from the base revision
start candidate B with a deliberately different configuration
  (different harness or model family; optionally a different strategy instruction)
  -- B does NOT see A's diff (independence) --
B gets its own L0 -> L1 (D_B <= 2)
select:
  one candidate passes, the other fails      -> pick the passing one
  scores differ                              -> pick the higher score
  tie (both pass or equal partial scores)    -> one Reviewer comparison
  still unresolved                           -> report "unresolved" + both artifacts
```

The design deliberately allows **one** second candidate. Candidate A's best checkpoint is never discarded.

### 5.4 Optional predictive entry (experiment arm E only)

Jev routing signals can be computed from the task envelope, letting the run start directly at L1 (with a critic pass after the first attempt) or start A and B concurrently. This trades cost for wall-clock time. It is not the default, because the cascade only spends breadth after evidence says it is needed.

## 6. Components

### 6.1 TaskNormalizer

Turns the raw task plus repository context into a typed `TaskEnvelope`: task text, repository path, base revision, explicit constraints, verifier spec, and category (for corpus runs). It does not choose a worker.

### 6.2 WorkerAdapter

```ts
interface WorkerAdapter {
  id: string;
  capabilities(): Promise<WorkerCapabilities>; // steering, resume, events, usage, subagent control
  start(input: WorkerStartInput): Promise<WorkerHandle>;
  events(handle: WorkerHandle): AsyncIterable<WorkerEvent>; // all target harnesses stream
  send?(handle: WorkerHandle, message: string): Promise<void>; // follow-up turn in same session (correction)
  steer?(handle: WorkerHandle, instruction: string): Promise<void>; // mid-turn steering
  terminate(handle: WorkerHandle): Promise<WorkerResult>;
}
```

Candidate integration surfaces, to be **confirmed on WebHMF**, not assumed:

- Codex: App Server (threads/turns, steering) or `codex exec --json` plus resume,
- OpenCode: server API, ACP, or `opencode run --format json`,
- Pi: RPC or SDK,
- Claude Code: Agent SDK or `claude -p --output-format stream-json` plus `--resume`.

If an adapter cannot `send`, correction uses a fresh session on the checkpoint. If it cannot `steer`, steering becomes stop-and-continue.

**Nested subagents.** Harnesses can spawn their own subagents, which hides cost and activity from the orchestrator's budget. During experiment runs, adapters disable them where the harness allows. Otherwise they count observed subagent usage as worker usage, and the report flags "nested subagents active".

### 6.3 Critic

```ts
type Critique = {
  noConcreteDefect: boolean;
  defects: Array<{
    location: string;                     // file/function/test
    description: string;
    evidence: "verifier" | "diff" | "both";
    localized: boolean;                   // repairable in place vs needs a new approach
  }>;
  approachSound: boolean;                 // false suggests breadth over more depth
};
```

The critic is a frontier model, either a configured worker in read-only mode or a direct API call. It sees the reviewer projection (§8). Output is schema-validated; unparseable output counts as `noConcreteDefect` with an error event, and never triggers an action on its own.

### 6.4 DecisionProvider: rules vs Jev

One interface with two implementations, so the experiment can isolate Jev's contribution.

```ts
type SupervisionSignals = {
  appearsStuck: number;          // 0..1, during a worker run
  completionPlausible: number;   // 0..1, used only when no verifier exists
};

type RefinementSignals = {
  repairLikely: number;          // 0..1, given the critique: will in-place correction work?
};

type RoutingSignals = {          // arm E only
  difficulty: 0 | 1 | 2 | 3 | 4;
  breadthValue: number;          // 0..1
};
```

- **RulesProvider** derives signals deterministically. Examples: the same failing command at least twice means stuck; no workspace change for T minutes means stuck; a critique with any non-localized defect, or `approachSound=false`, makes repair unlikely.
- **JevProvider** asks Jev the same bounded questions over the compact projection.

v0 deliberately keeps **few signals**. Each one is a threshold to tune, and a small corpus cannot support many thresholds without overfitting. Add signals only when run reports show a decision the current ones cannot make.

### 6.5 Verification

```ts
type VerificationResult = {
  status: "pass" | "fail" | "partial" | "unknown" | "error";
  score?: { passed: number; total: number };     // enables progress measurement
  output: BoundedText; artifactPath?: string;
  source: "repository" | "task" | "held-out-acceptance";
};
```

Verifier sources, in order of preference:

1. **Task-specified checks:** corpus tasks carry their own (for example, the fix commit's tests).
2. **Repository checks:** test/lint/typecheck commands from configuration or authoritative repository instructions. The orchestrator never invents them.
3. **Held-out acceptance checks:** when 1 and 2 are missing or do not cover the task, a different worker writes acceptance tests from the task text before implementation starts. The checks are stored outside the candidate workspace, so the implementer cannot see or edit them, and are copied in only at verify time. They are valid only if they **fail on the base revision** and run cleanly. Invalid checks are discarded and the run continues as `unknown`.
4. **None:** status `unknown`. This is never success. Completion then depends on configured policy (critic plus `completionPlausible`) and is reported as **unverified**.

### 6.6 Checkpoint store

Each candidate workspace is a Git worktree. Every refinement round that is accepted becomes a commit on the candidate branch, tagged with its verification score. Revert means resetting to the best checkpoint commit. Checkpoints are never deleted during a run, so the full refinement trajectory stays inspectable.

### 6.7 Reviewer

A reviewer is used **only** for an L2 tie. It compares the two candidates' diffs and evidence and returns a structured preference with reasons, or "no preference". It never overrides verification. A remaining tie is reported as `unresolved`.

### 6.8 Deterministic Arbiter

```ts
type RuntimeAction =
  | "CONTINUE" | "STOP_WORKER" | "VERIFY"
  | "CRITIQUE" | "CORRECT" | "ACCEPT_CHECKPOINT" | "REVERT_CHECKPOINT"
  | "START_SECOND_CANDIDATE" | "REVIEW"
  | "FINISH" | "FAIL";
```

A pure function `(RunState, signals, evidence, budget, config) -> { action, reason }`. It returns exactly one legal action with a machine-readable reason. Illegal transitions are errors, never silent fallbacks. It is unit tested.

## 7. Event-sourced run history

Every observation and decision is an append-only JSONL event: run ID, sequence number, timestamp, type, source, typed payload, and candidate ID.

```text
run.started            config.snapshot          verifier.prepared
worker.started         worker.event             worker.finished
verification.completed critique.completed       decision.signals
arbiter.action         checkpoint.accepted      checkpoint.reverted
candidate.started      review.completed         run.finished / run.failed
```

`RunState` is reconstructed by folding events (unit tested). The report answers questions like "why was a second candidate started?" from recorded evidence, signals, thresholds, and budget.

## 8. Projections

- **Decision projection (Jev/rules):** compact facts only: level, round, last verifier score and trend, failing-check count, changed-file count, last error summary, elapsed/budget fraction, critique summary flags.
- **Critic/reviewer projection:** task, constraints, diff against base, verifier output (bounded), previous critiques in this run.
- **Runtime projection:** processes, workspaces, checkpoints, counters, budget, legal next actions.

No model owns runtime truth.

## 9. Budgets and compute accounting

Hard per-run limits: wall-clock, worker sessions, correction rounds per candidate (D), candidates (≤2), critic calls, reviewer calls (≤1), Jev calls.

Compute is accounted **per call** in whatever unit the harness reports: input/output tokens, provider cost, or subscription quota. Wall-clock is always recorded. Subscription logins often report no dollar cost, so arms are compared on tokens and wall-clock, with dollars only where available. **Compute matching** between arms (§15) uses the same unit.

Default limits:

```text
candidates:          max 2
refinement rounds:   D = 2 per candidate (hard max 3)
critic calls:        <= D per candidate
reviewer calls:      max 1 (L2 tie only)
```

## 10. Stop conditions

- **Verified success:** the verifier passes.
- **Unverified completion:** no verifier exists and the configured completion policy is satisfied. Reported as unverified.
- **Stalled:** refinement stalled and breadth is not allowed or already used. Return the best verified state.
- **Budget stop:** any hard limit reached. Return the best verified state.
- **Policy stop:** user cancellation, forbidden action, unavailable capability, unrecoverable worker failure.
- **Unresolved:** L2 tie the reviewer could not break.

The runtime never labels a non-passing state as success.

## 11. Breadth selection rules

- Candidate B must differ from A in at least one recorded dimension: harness, model family, or strategy instruction. Same harness plus same model plus a different temperature is rejected by config validation, unless the run is arm R.
- B is independent: it does not see A's diff or critiques. Feeding A's failure into B is a different strategy ("informed restart") and would need its own arm.
- Selection is by verifier first. The reviewer is consulted only on ties.

## 12. Worker registry

```ts
type WorkerDescriptor = {
  id: string;
  harness: "codex" | "opencode" | "pi" | "claude-code" | string;
  model?: string;
  modelFamily?: string;           // used for cross-family critic and diversity checks
  costClass: "cheap" | "standard" | "frontier";
  roles: Array<"worker" | "critic" | "reviewer" | "test-author">;
  tags: string[];
};
```

Roles are configuration, not brand identity. The same harness may appear several times with different models.

## 13. Failure and fallback

- **Jev unavailable:** use RulesProvider and record the fallback.
- **Critic unavailable:** refinement uses verifier output only, and the report flags "critic-less refinement".
- **Worker unavailable:** use the next compatible descriptor by deterministic availability policy.
- **Reviewer unavailable:** an L2 tie becomes `unresolved`; never fake consensus.
- **Verifier unavailable:** status `unknown`, reported as unverified.

## 14. Configuration

Declarative and environment-neutral; secrets come only from environment references.

```yaml
ladder:
  refineRounds: 2              # D, hard max 3
  stallAfterNoProgress: 2
  breadthEnabled: true
decisions:
  provider: rules              # rules | jev
  stuckThreshold: 0.7
  repairLikelyThreshold: 0.5
budgets:
  maxCandidates: 2
  maxWallClockMinutes: 60
  maxReviewerCalls: 1
workers:
  - { id: codex-primary,  harness: codex,       modelFamily: openai,    roles: [worker] }
  - { id: claude-critic,  harness: claude-code, modelFamily: anthropic, roles: [critic, reviewer] }
  - { id: pi-alt,         harness: pi,          modelFamily: other,     roles: [worker, test-author] }
  - { id: opencode-alt,   harness: opencode,    roles: [worker] }
verification:
  sources: [task, repository, held-out-acceptance]
experiment:
  disableNestedSubagents: true
```

## 15. Experiment arms

Every arm shares the same adapters, verifier, and event log. Only the policy differs.

| Arm | Policy | Isolates |
|---|---|---|
| **A** | single worker, verify once | baseline |
| **R** | two independent runs (same configuration), verifier picks | resampling at matched compute |
| **R'** | two runs with *different* configurations, verifier picks | diversity without refinement |
| **B** | L0 → L1 with RulesProvider | refinement (H1 vs R, H2 vs A) |
| **B0** | B without the critic (verifier output only) | value of explicit critique (H3) |
| **C** | B with JevProvider | Jev vs rules (H4) |
| **D** | full ladder L0 → L1 → L2 (provider = winner of B vs C) | breadth after stall (H5, H6 vs R') |
| **E** *(optional)* | Jev predictive entry | prediction vs cascade |

**Compute matching:** R and R' are budgeted to B's median compute on the same task set, so "refinement beats resampling" is a fair claim.

## 16. What to measure

Per run: arm, final status (verified / unverified / stalled / budget / unresolved), verifier score trajectory, levels reached, rounds used, regressions reverted, candidates, critic/reviewer/Jev calls, tokens/cost/quota per call, wall-clock, harness and model versions, nested-subagent flag, and the arbiter's reason for every transition.

Per arm across the corpus: verified success rate with confidence intervals over the k repeats, cost distribution, and **paired** per-task comparisons against A and R. Recovery rate is also tracked: of tasks that failed L0, the share recovered at L1 and at L2. That is the coding analogue of the paper's per-depth recovery rate.

The experiment never relies on provider-reported "confidence".

## 17. Repository layout target

```text
src/
  cli/main.ts
  core/        types.ts events.ts state.ts policy.ts budgets.ts errors.ts
  config/      schema.ts load.ts
  decisions/   provider.ts rules.ts jev.ts schemas.ts
  workers/     adapter.ts registry.ts process.ts codex.ts opencode.ts pi.ts claude-code.ts
  critic/      critic.ts
  review/      reviewer.ts
  verification/ adapter.ts command.ts acceptance.ts
  runtime/     orchestrator.ts workspace.ts checkpoints.ts event-store.ts
  experiment/  corpus.ts arms.ts runner.ts
  reporting/   run-report.ts compare.ts
test/          (unit tests for pure core)
corpus/        (task manifests; no secrets, no private repos' contents)
config/example.yaml
```

This is a design target; nothing is implemented yet.

## 18. WebHarness ARM → WebHMF handoff

Implementation begins on WebHMF with **discovery, not assumptions**: inspect each harness's version, structured interface, session resume, steering, cancellation, usage reporting, subagent control, and model selection, plus Jev client/auth availability. Record the non-secret findings in `docs/04-webhmf-environment.md`. Transfer the repository through Git. Never copy credentials or auth caches from ARM.

## 19. Explicit non-goals for v0

MCTS, beam search, more than two candidates, learned PRMs, debate, autonomous long-term memory, distributed queues, multi-user hosting, web UI, plugin marketplace, credential management, and self-modifying routing policy.

## 20. Design summary

```text
Paper:  breadth saturates; explicit critique + correction recovers errors.
Ours:   depth first (critique -> correct -> verify, keep best checkpoint),
        one deliberately different second candidate only after a stall,
        verifier instead of voting,
        Jev vs rules measured, not assumed,
        every mechanism compared at matched compute.
```

## 21. Revision log

### Revision 2 (2026-10-06)

Reworked around arXiv 2608.05643 after a design review.

- **Escalation ladder replaces upfront routing.** v1 predicted DIRECT/SUPERVISED/BREADTH from the task text. Difficulty prediction from prompts is unreliable, and the paper's lesson is to refine existing attempts first. Predictive routing is now optional arm E.
- **Refinement is a first-class level (L1)** with explicit critique, D≤3 rounds, checkpoints, and no-regression acceptance. v1 allowed one repair, which underused the paper's main mechanism and had no guard against degradation.
- **Breadth only after a stall**, with independence and mandatory diversity, and each candidate is also refined, as in the paper.
- **Verifier replaces voting.** Held-out acceptance checks were added for tasks without verifiers.
- **Experiment arms expanded:** compute-matched resampling (R, R'), a critic ablation (B0), and Jev vs rules (B vs C). v1 could not separate "the loop helps" from "Jev helps" or "refinement beats resampling".
- **Fewer Jev signals.** Six supervision floats became two plus one refinement signal, to avoid overfitting thresholds on a small corpus.
- **Claude Code** is now a first-class optional worker/critic adapter; it is still not required.
- **Nested subagents** are disabled or counted during experiments.
- **Event-streaming worker contract** (`AsyncIterable`) replaces polling `observe()`, and session continuation was added for in-session correction.
- **Unit tests** for the pure core. v1 excluded tests entirely.
- **Phase 0** (corpus, protocol, baseline variance) now comes before building the control plane; see `02-implementation-plan.md`.
