# Worker Engine Decoupling Implementation Plan

**Goal:** Separate Muse transport details from the generic Codex worker lifecycle so a future Pi engine can be added without rewriting supervision.

**Architecture:** Keep `codex-workers` as the stable CLI/worker manager. Move reusable orchestration into a standard-library package, represent launch settings structurally, and put Muse command/event/usage logic behind an engine adapter. Preserve v1 job compatibility and current behavior throughout.

**Spec:** `docs/superpowers/specs/2026-10-03-worker-engine-decoupling-design.md`

## Stage 1 — Install-safe module foundation

**Files**
- Modify: `bin/codex-workers`
- Create: `bin/codex_workers/__init__.py`
- Create: `bin/codex_workers/cli.py`
- Modify: `install.sh`
- Modify: `tests/test_workers.py`

**Changes**
- Turn `bin/codex-workers` into a thin launcher.
- Move the current implementation into `bin/codex_workers/cli.py` without behavioral changes.
- Pass the launcher path explicitly into the implementation so detached supervision re-executes the launcher rather than `cli.py`.
- Install the `codex_workers` package in the same commit.
- Extend installer backup/symlink protections to the package directory.
- Add an installed-copy test that invokes the installed launcher from outside the repo/install tree.

**Verification**
- Existing worker tests.
- Installer dry-run/install tests.
- Installed launcher works from an unrelated cwd.
- Detached `start` still spawns via the launcher.
- shellcheck.

## Stage 2 — Structured v2 state and v1 compatibility

**Files**
- Create: `bin/codex_workers/state.py`
- Create: `bin/codex_workers/compat/__init__.py`
- Create: `bin/codex_workers/compat/v1_muse.py`
- Modify: `bin/codex_workers/cli.py`
- Modify: `tests/test_workers.py`

**Changes**
- Persist `schema_version=2`, `engine`, generic `session_id`, and complete structured launch configuration for new jobs.
- Keep the generated command for diagnostics only.
- Decode old v1 Muse state/argv into normalized configuration in `compat/v1_muse.py`.
- Never rewrite historical state just by reading it.
- Preserve the old request/policy/control-file protocol for a live v1 supervisor.

**Verification**
- Construct representative v1 states covering provider/model, read-only, web, schema, logging and isolation flags.
- Confirm normalized values are complete.
- Confirm `show/list/logs/wait/followup/effort/close/cancel` remain compatible with v1-style state/control files.

## Stage 3 — Engine contract and fresh Muse commands

**Files**
- Create: `bin/codex_workers/engine.py`
- Create: `bin/codex_workers/engines/__init__.py`
- Create: `bin/codex_workers/engines/muse.py`
- Modify: `bin/codex_workers/cli.py`
- Modify: `tests/test_workers.py`

**Changes**
- Introduce an engine registry with Muse as the only production engine.
- Move Muse executable validation and command construction into `MuseEngine`.
- Materialize a structured turn request each turn.
- Rebuild a fresh argv for every initial/follow-up turn.
- Stop mutating old command arrays.
- Leave current Muse worktree creation behavior intact.

**Verification**
- Existing command-line behavior remains equivalent.
- Follow-up effort/step overrides produce a fresh correct command.
- Model/provider/read-only/web/schema/session flags remain unchanged.

## Stage 4 — Normalized events and fixture engine

**Files**
- Modify: `bin/codex_workers/engine.py`
- Modify: `bin/codex_workers/engines/muse.py`
- Modify: `bin/codex_workers/cli.py`
- Add test fixture engine/helper under `tests/`

**Changes**
- Move Muse JSON event interpretation and quota detection into `MuseEngine`.
- Define normalized lifecycle events consumed by the generic supervisor.
- Keep final outcome precedence in the core.
- Add a test-only second engine that uses the same supervisor path.

**Verification**
- Fixture engine covers success, reusable follow-up, task warning, terminal failure, non-zero exit, malformed stream, missing completion, cancellation, escalation and graceful close.
- Task warnings remain warnings rather than automatic final failure.
- Generic lifecycle tests pass without importing MuseEngine.

## Stage 5 — Real worktree characterization

**Files**
- Modify/add tests only.

**Changes**
- Build temporary real Git repositories.
- Record current Muse worktree semantics before changing ownership.
- Cover default/explicit base, selected branch/path, reuse, reopen, failure/collision behavior, and retention after close/cancel.
- Keep existing production implementation unchanged.

**Verification**
- Real `git worktree list --porcelain` assertions.
- Installed Muse echo transport checks when available; skip clearly when unavailable.

## Stage 6 — Generic workspace ownership

**Files**
- Create: `bin/codex_workers/workspace.py`
- Modify: `bin/codex_workers/cli.py`
- Modify: `bin/codex_workers/engines/muse.py`
- Modify: tests.

**Changes**
- Move worktree create/existing resolution into the generic workspace component.
- Supply Muse an already-resolved workspace.
- Remove Muse stderr worktree discovery only after parity is established.

**Verification**
- Stage 5 characterization tests continue passing.
- Follow-ups reuse exactly the same worktree.
- Failed creation leaves the same observable state as before.

## Stage 7 — Engine usage and neutral presentation

**Files**
- Modify: `bin/codex_workers/engines/muse.py`
- Modify: `bin/codex_workers/cli.py`
- Modify: tests.

**Changes**
- Move `muse export` and usage parsing behind MuseEngine.
- Keep historical inspection available without Muse execution support.
- Make aggregate dashboard/status wording engine-neutral while showing each job's engine.
- Keep Muse quota information structured and visible.

**Verification**
- Existing usage accounting expectations.
- Historical list/show/logs/watch/status work without loading Muse execution.
- Engine-dependent operations return clear errors when unavailable.

## Stage 8 — Documentation and final audit

**Files**
- Modify: `README.md`
- Modify: `skills/subagent-routing/SKILL.md`
- Modify installer/tests as needed from the final audit.

**Changes**
- Document generic worker core + Muse engine.
- Keep `muse-worker` as compatibility launcher.
- Audit generic modules for Muse transport leakage.
- Do not add Pi code.

**Final verification**
- `python3 tests/test_workers.py`
- shellcheck for shell scripts
- install to a temporary prefix and invoke from an unrelated cwd
- confirm clean Git status
- inspect generic modules for Muse CLI/event/export/quota coupling
