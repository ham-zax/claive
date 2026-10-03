# Codex Worker Engine Decoupling Design

## Status

Approved architectural direction: decouple Muse-specific execution from the generic Codex worker lifecycle before adding any Pi integration.

Baseline commit: `e0c90c7 Initial commit: Codex Muse workers`.

This design intentionally excludes Pi implementation. Pi will be a separate follow-up plan after the generic engine boundary is proven by a test-only second engine.

## Goals

- Preserve the current Codex worker lifecycle and observable behavior while removing Muse-specific assumptions from generic supervision.
- Make Muse one execution engine behind a small engine contract.
- Rebuild each turn command from structured configuration instead of mutating persisted argv arrays.
- Keep existing v1 worker state readable and controllable after upgrade.
- Characterize Muse worktree ownership with real Git worktrees before deciding whether ownership can move without changing behavior.
- Keep every intermediate commit installable; no commit may deploy a launcher whose imported modules were not installed with it.
- Prove the engine boundary with a non-Muse test engine that exercises the same lifecycle core.

## Non-goals

- No Pi engine or Pi-specific code.
- No change to Muse's pinned production model, provider policy, reasoning defaults, quota policy, or fallback policy.
- No packaging/virtualenv/third-party Python dependency.
- No automatic migration/rewrite of historical state files merely because they are read.
- No silent provider/engine fallback.
- No unrelated redesign of the tmux UI or Codex host integration.

## Current Coupling

The current `bin/codex-workers` owns both generic worker orchestration and Muse transport details. Muse-specific assumptions currently appear in:

- `MUSE_WORKER_BINARY` and the pinned Muse model.
- command construction for `muse exec`.
- Muse JSONL event names and terminal event schema.
- `muse_session_id` state.
- direct argv mutation for follow-up turns.
- Muse stderr parsing for created worktree paths.
- `muse export` usage accounting.
- Muse subscription quota string recognition.
- dashboard/reporting text.
- CLI provider/model constraints.
- installer names and routing documentation.
- tests whose fake worker emits Muse-shaped events.

The refactor must move these assumptions behind explicit compatibility or engine boundaries rather than adding engine conditionals throughout the current file.

## Target Architecture

```text
Codex / shell
     |
     v
bin/codex-workers
     |
     v
codex_workers.cli
     |
     v
Worker Core
 |- registry/state
 |- reusable-turn queue
 |- process supervision
 |- cancellation/process identity
 |- logs/results
 |- normalized event handling
 |- outcome precedence
 |- workspace isolation
 |- dashboard/reporting
 '- fallback policy
     |
     v
Engine interface
     |
     +-- MuseEngine
```

A later Pi integration should require a new engine adapter and engine-specific tests, not changes to generic supervision, cancellation, registry, reusable-turn logic, or dashboard lifecycle.

## Repository Layout

The first module split will introduce a sibling package installed alongside the executable:

```text
bin/
  codex-workers
  muse-worker
  codex-with-workers
  codex_workers/
    __init__.py
    cli.py
    core.py
    state.py
    engine.py
    compat/
      __init__.py
      v1_muse.py
    engines/
      __init__.py
      muse.py
```

Files may be introduced incrementally, but the first commit that imports `codex_workers` must also update the installer to copy that package.

The project remains standard-library-only.

## Launcher Identity and Detached Supervision

The executable path is part of the runtime contract. Detached supervision currently re-executes the launcher. Moving logic into `core.py` must not change that path.

The wrapper will resolve its own executable path and pass it to the CLI/core:

```python
launcher_path = Path(__file__).resolve()
return main(launcher_path=launcher_path)
```

The core stores or receives this launcher path and uses it for detached supervision. It must never derive the re-exec path from `core.py.__file__`.

Installed-copy tests must verify detached supervision from an installed executable outside the repository.

## Install-safe Module Introduction

Installer support moves into the same commit that introduces the first package import.

The installed layout is:

```text
<bin_dir>/
  codex-workers
  muse-worker
  codex-with-workers
  codex_workers/
    ...
```

`install.sh` must:

- install/update the package directory atomically enough that the executable never intentionally targets missing modules;
- preserve mode and backup behavior for existing executable files;
- back up or safely replace an existing installed `codex_workers` directory;
- reject unsafe symlink/non-file destinations consistently with current behavior;
- keep dry-run output accurate for both files and the package directory.

The verification test installs into a temporary bin directory, changes cwd to a directory outside both the repository and install destination, then executes the installed launcher. This proves imports come from the installed package rather than the source checkout.

## Generic Persisted State

New jobs use schema version 2. The persisted command array is no longer the source of truth for launch behavior.

Representative state:

```json
{
  "schema_version": 2,
  "engine": "muse",
  "session_id": "uuid",
  "workspace": "/repo",
  "launch": {
    "provider": "meta",
    "model": "muse-spark-1.3-contributor",
    "read_only": false,
    "web": false,
    "output_schema": null,
    "session_logging": true,
    "isolation": {
      "mode": "none",
      "base": null,
      "existing_path": null
    }
  },
  "reasoning_effort": "high",
  "max_model_steps": 100
}
```

The exact JSON layout may use nested or flat keys, but all information required to rebuild a turn must be persisted structurally.

A turn command is constructed from:

1. persisted launch configuration;
2. persisted/default turn policy;
3. queued per-turn overrides;
4. the prompt file;
5. the resolved workspace;
6. engine-specific session state.

Generic code must not inspect or mutate command-line flags to reconstruct these values.

## Turn Request

The core will materialize a neutral per-turn request before invoking an engine. Its logical fields are:

- engine name;
- workspace/actual workspace;
- prompt file;
- session ID;
- provider;
- model;
- reasoning effort;
- max model steps;
- read-only/tool policy;
- web/network policy;
- output schema;
- session logging setting;
- isolation metadata.

The engine receives this structured request and returns a fresh argv for that turn.

Follow-ups therefore rebuild commands instead of replacing values inside the previous turn's argv.

## V1 Compatibility

Legacy state compatibility is a dedicated subsystem outside generic supervision:

```text
codex_workers/compat/v1_muse.py
```

It knows the exact v1 Muse argv/state format and may decode:

- Muse binary path;
- provider;
- model;
- prompt file;
- session UUID;
- reasoning effort;
- max model steps;
- read-only flags;
- web-tool flags;
- output schema;
- session logging flag;
- create/existing worktree mode;
- worktree base;
- existing worktree path.

The compatibility decoder returns normalized in-memory configuration.

### No eager state rewrite

Reading a v1 record must not rewrite it as v2. A still-running v1 supervisor may continue to write v1 state. Normalization is in-memory only unless a future explicit migration command is designed.

### Live v1 control compatibility

A new CLI must still control a worker whose supervisor was started by the v1 implementation.

For v1 workers:

- `followup` writes the existing request JSON shape;
- `effort` preserves the existing `policy.json` shape;
- `close` preserves `close.request`;
- `cancel` continues to use supervisor PID plus process identity;
- `wait`, `show`, `list`, and `logs` remain compatible with v1 state.

New jobs use v2. Existing live v1 jobs are not upgraded in place.

## Engine Contract

The production engine registry initially contains only Muse.

The contract stays intentionally small. Exact Python signatures will be finalized in the implementation plan, but responsibilities are:

- validate engine-specific launch configuration;
- build a fresh command for a normalized turn request;
- translate one native engine event into normalized worker events;
- finalize engine-specific turn metadata if needed;
- collect engine-specific session usage when supported;
- expose capability information needed for explicit CLI validation.

The engine does not own generic process supervision, cancellation, job status, registry persistence, follow-up queues, dashboard rendering, or generic worktree lifecycle.

## Normalized Worker Events

Muse native JSONL is translated inside `MuseEngine`. The core consumes normalized events such as:

- `model_step`;
- `tool_started`;
- `tool_finished`;
- `output_delta`;
- `task_warning`;
- `terminal_completed`;
- `terminal_failed`;
- `quota_exhausted`.

The generic core must not contain Muse event names such as `task.lifecycle.proposed` or `run.terminal.completed`.

Engine event translation may attach engine-specific details for logs/diagnostics, but generic state transitions depend on the normalized event type.

## Outcome Precedence

The existing success semantics are preserved explicitly.

After a child process finishes:

1. if cancellation was requested, the turn is `cancelled`;
2. otherwise, a non-zero child exit means `failed`;
3. otherwise, absence of a valid normalized terminal-completed event means `failed`;
4. otherwise, any malformed native event records mean `failed`;
5. otherwise, the turn is `completed`.

Task-warning/task-failure events remain separate accounting signals. They increment the task-failure count and remain visible to the caller, but do not by themselves convert an otherwise successfully completed turn into a failed process outcome.

Quota exhaustion is normalized by the engine adapter and attached as structured state; the generic supervisor does not parse Muse error strings.

## Test-only Engine

A second engine is required to prove the boundary, but it is test-only and never registered in the installed production CLI.

The fixture engine must use the same worker core to exercise:

- initial launch;
- successful completion;
- reusable idle state;
- follow-up;
- turn-policy override;
- task warning;
- terminal failure;
- non-zero process exit;
- malformed event stream;
- missing terminal-completed event;
- cancellation;
- process-group escalation;
- graceful close/drain.

Passing these tests without importing `MuseEngine` is the primary proof that lifecycle orchestration is engine-neutral.

## Historical Operation Without Muse

Historical registry inspection must not depend on the Muse adapter or Muse executable.

The following operations must remain usable for stored jobs when Muse execution support is unavailable:

- `list`;
- `show`;
- `logs`;
- `status-line`;
- `watch`;
- `cancel`, when a matching live supervisor/process can be safely identified.

Engine-dependent operations must fail with a clear capability error rather than making the registry unusable. For example, Muse usage collection may report that the Muse engine/binary is unavailable while preserving access to stored metadata and logs.

The v1 compatibility decoder therefore cannot require importing or executing Muse.

## Worktree Ownership

Worktree ownership is deliberately separated from command reconstruction.

### Phase 1: Characterize current behavior

Before production ownership changes, tests must use a real temporary Git repository and capture current Muse worktree semantics.

Characterization must cover:

- default base revision;
- explicit `--worktree-base` resolution;
- branch naming;
- worktree path/location selection;
- collision behavior;
- failed creation behavior;
- partial branch/worktree retention on failure;
- exact `actual_workspace` recorded by the manager;
- reuse of the same worktree on subsequent turns;
- reopening via `--worktree-existing` without creating another worktree;
- graceful-close retention;
- cancellation retention/removal behavior.

Where the installed Muse echo transport can create the real worktree, use it. Tests that require installed Muse must skip clearly when Muse is unavailable, while generic Git characterization may run unconditionally.

The existing fake test that creates an ordinary directory and prints a simulated Muse workspace path is insufficient as characterization evidence.

### Phase 2: Keep ownership engine-specific in this refactor

Characterization on Muse 1.4.2 shows that `-w create` creates a genuine linked Git worktree under `.muse/worktrees`, on branch `muse/session-<session UUID>`, based at the requested ref, and removes both the worktree and branch when `muse exec` exits. Moving creation into the generic core would therefore also change cleanup/lifetime semantics.

This refactor keeps creation and cleanup owned by Muse. Muse-specific workspace discovery moves behind `MuseEngine`, so generic supervision no longer parses Muse stderr or assumes Muse worktree naming. A future isolation redesign may intentionally introduce manager-owned persistent worktrees as a separate behavior change.

## Usage Accounting

Generic code exposes the concept of session usage; engines implement collection when supported.

Muse usage continues to derive from `muse export --session ... --redacted` and preserve existing accounting semantics:

- count each model completion once;
- avoid duplicate attribution records;
- expose known input/output/cached/reasoning counters;
- keep unknown values unknown;
- do not treat cumulative accounting as current context size or an invoice.

The Muse export command and Muse event schema live in `engines/muse.py`, not generic core code.

## CLI and Compatibility

The generic CLI gains an engine concept. Initially, omitting `--engine` means `muse` for backward compatibility.

`muse-worker` remains a compatibility launcher for reusable Muse workers.

Existing production Muse constraints remain unchanged during this refactor:

- model remains `muse-spark-1.3-contributor`;
- normal reasoning remains high;
- existing supported effort levels remain unchanged;
- no automatic fallback;
- echo remains a Muse transport fixture where currently supported.

Engine-specific flags must either be validated through the selected engine or represented as generic policy fields. The generic parser should not accumulate Pi-specific or Muse-specific conditionals.

## Dashboard and Reporting

Lifecycle presentation becomes engine-neutral:

- aggregate summary uses `Workers:` instead of `Muse:`;
- individual job state exposes the engine;
- generic phases use neutral terms;
- Muse-specific quota/reporting messages may still be rendered when the engine supplies that structured condition.

This presentation change comes after engine and state compatibility are established, so it does not obscure functional refactoring.

## Installer Compatibility

The installer must continue to preserve:

- dry-run behavior;
- backups for changed installed artifacts;
- refusal to replace unsafe symlinks/non-files;
- file modes;
- preservation of global AGENTS configuration.

The package directory becomes another installed artifact with equivalent safety guarantees.

No commit after module introduction may require running from the source checkout to function.

## Commit / Implementation Sequence

### 1. Install-safe module foundation

- add package-directory installation and backup handling;
- split the launcher just enough to import installed modules;
- pass launcher identity explicitly for detached supervision;
- add installed-copy execution tests from outside the repository;
- retain existing runtime behavior.

### 2. Structured v2 state and v1 compatibility

- persist complete structured launch configuration for new jobs;
- add `compat/v1_muse.py`;
- normalize v1 state in memory without rewriting it;
- add tests for legacy config recovery;
- add tests proving new CLI commands can control a still-running v1-style supervisor.

### 3. Engine contract and Muse command construction

- introduce engine registry/contract;
- create `MuseEngine`;
- rebuild a fresh Muse command from normalized turn configuration each turn;
- remove generic follow-up argv mutation;
- leave Muse worktree creation unchanged.

### 4. Normalized lifecycle and fixture engine

- move Muse native event parsing and quota detection into `MuseEngine`;
- make generic core consume normalized worker events;
- codify outcome precedence;
- add the test-only fixture engine and run the full lifecycle through generic supervision.

### 5. Worktree behavior characterization

- build real Git worktree characterization coverage;
- capture current base/path/branch/reuse/failure/retention semantics;
- do not change production worktree ownership in this commit.

### 6. Encapsulate Muse worktree handling

- keep Muse responsible for worktree create/cleanup;
- move Muse stderr workspace discovery behind `MuseEngine`;
- keep generic supervision unaware of Muse worktree paths/branch naming;
- preserve current observable cleanup behavior.

### 7. Engine-specific usage and neutral presentation

- route usage collection through engine capability;
- move Muse export parsing fully behind MuseEngine;
- genericize dashboard/reporting language;
- retain Muse-specific structured quota output.

### 8. Documentation and compatibility cleanup

- update README and routing skill;
- document worker core versus engine responsibilities;
- verify installer behavior and installed launcher;
- run full behavioral tests and shellcheck;
- audit generic modules for Muse transport leakage.

Pi remains outside all eight stages.

## Acceptance Criteria

The refactor is complete when all of the following are true:

1. Every commit after the first module split can be installed and executed without the source repository on `sys.path`.
2. Detached supervision always re-executes the launcher, not an internal module.
3. New jobs persist enough structured configuration to rebuild every turn without inspecting prior argv.
4. V1 jobs remain readable, inspectable, cancellable and controllable while a v1 supervisor is still running.
5. Generic supervision contains no Muse event names, Muse CLI argument parsing, Muse quota-string parsing, or Muse export logic.
6. A test-only non-Muse engine passes launch, reusable follow-up, completion, failure and cancellation lifecycle tests through the same core.
7. Outcome precedence matches the baseline: successful exit + completed terminal + no malformed events is required; task warnings remain separate.
8. Real Git characterization documents Muse worktree base, branch, path and cleanup semantics.
9. Generic supervision contains no Muse worktree stderr/path parsing; worktree lifecycle remains engine-owned in this refactor.
10. Historical `list/show/logs/watch/status-line` remain usable when Muse execution is unavailable.
11. Removing the Muse execution adapter does not break generic lifecycle tests or historical-state inspection.
12. Adding a future Pi engine should require an engine adapter, registration and Pi-specific tests rather than modifications to generic lifecycle orchestration.

## Verification Strategy

Keep verification proportional to the risk of each stage rather than following strict TDD ceremony.

- add or adjust focused regression tests for behavior that could break during a stage;
- run the relevant focused tests after structural changes;
- run `python3 tests/test_workers.py` before each stage commit;
- run shellcheck when shell scripts change;
- run installer integration checks whenever package/install behavior changes;
- keep the real Git worktree characterization check green while moving Muse-specific discovery behind the engine;
- commit each independently reviewable architectural stage.

Before final completion, run the complete suite from the repository and execute an installed copy from outside the repository.
