---
name: claive-orchestration
description: Operate Claive workers across Pi, Codex, Claude Code, OpenCode, and Antigravity. Use plain claive for bounded delegation or fan-out; use claive-orch for verifier-gated refinement. Covers worker lifecycle, reuse, engines, waiting/notifications, and verification.
---

# Claive orchestration

You are the parent: you own scope, assignment, integration, and final verification. A worker's exit `0` or report is evidence, never acceptance.

Checkout and docs live at `/home/hamza/repo/claive` (laptop) or `/home/ubuntu/repo/claive` (ARM). Paths such as `docs/...` and `reference/...` resolve there; paths under `references/...` belong to this skill.

## When to delegate, and worker reuse

- Respect your host's delegation rule (ask first if your `AGENTS.md` or `CLAUDE.md` requires it).
- Delegate when isolated execution, parallel coverage, or an independent check clearly beats doing the work inline. Keep small, tightly coupled, or design-fluid work inline.
- Before launching, verify the deliverable, the check that proves it, and why delegation beats inline work. When the user explicitly asks to delegate, do not refuse over ceremony; surface missing checks or risks and proceed.
- **Reuse for context and prompt caching:** Reuse an active worker (`claive followup`, `send`) when its loaded context and prompt cache still serve the scope, especially across continuous work or short interruptions. Start fresh only when the repo or task shifts substantially, stale assumptions interfere, or heavy compaction clouds it.
- Batch coherent slices into one brief with all target files, checks, and acceptance criteria instead of dripping micro-follow-ups. If a worker needs more than ~10 follow-ups, reassess the slice.

## Engines, models, and config

- Never rely on hardcoded model rosters. Run `claive engines --json`, `claive models NAME`, and `claive config check` for live enabled engines, defaults, per-model efforts (`default_model_efforts`, `disabled_model_efforts`), and `fallback_models`.
- Config lives at `$CLAIVE_CONFIG`, else `config.yaml|yml|json` in `~/.config/claive/`. Do not edit the shared file while another agent runs: copy it and export `CLAIVE_CONFIG` for isolated parallel waves, or pass `--set '{...}'` for single-worker overrides.
- Passing `--model` on Pi remembers it as Pi's global default; AGY also accepts a family stem plus `--effort` (e.g. `--model gemini-4-hydrogen --effort high`).
- Check cost with `claive usage --all --since 12h` (host) and `claive usage ID --json` (worker). Pass `--no-fallback` to suppress configured `fallback_models` (`--enable-steering` and non-default `--provider` suppress them automatically).

## Launch, wait, and wake reliably

- **T3 Code, direct CLI:** Use `claive run` in a managed shell command for one-shot work and yield so T3 can wake the parent when the command exits. For reusable workers, track a managed `claive wait` for each assigned turn. See [host wait mechanics](references/hosts-and-waits.md#t3-code-direct-cli).
- **Identity and scoping:** Pass `--alias NAME` (`--reuse-alias` safely rebinds a settled alias with no open `ASK` or unaccepted dirty worktree). Launches auto-tag `parent_session` from harness env (`$CLAUDE_SESSION_ID`, `$CODEX_THREAD_ID`, `$ANTIGRAVITY_CONVERSATION_ID`, `$OPENCODE_SESSION_ID`, `$PI_SESSION_ID`) or `--session S`.
- **Detached launches do not auto-wake idle turns:** `claive start`, `open --detach`, and `batch start` exit `0` in ~50ms. Before ending a turn with active non-parked workers, arm a background `claive wait` (reactive harnesses) or wait in-turn (non-reactive harnesses); otherwise use `claive park ID` (`--park`) for intentional handoffs.
- **Always use `--heartbeat 600`:** Run `claive wait ID1 ID2 ... --all --heartbeat 600 --timeout 1800` (`--any` for first settled). `inotify` wakes in `<5ms` on settlement (`0` done, `1` failed, `3` `ASK`); `125` prints a compact progress preview (`phase`, `steps`, `changed_files`, `active_tool`) and the exact `Re-arm wait:` command.
- **Inspect without fighting sandboxes:** Use `claive show ID --json`, `claive list --active`, `claive inbox --session S [--since-seq N]`, `claive peek ID`, or `claive-mcp` (`claive_wait`, `claive_list`). Do not call `read_file` on `~/.local/state/claive/.../status.json` from workspace-sandboxed sessions.
- **Bound and rescue turns:** Always pass `--turn-timeout`. Use `--tool-idle-timeout SECONDS` or `claive interrupt-tool ID` to abort only a hung silent child tool so the worker recovers. Never `cancel` a working worker blindly: `claive steer` or `send` for a summary first.
- **Questions (`exit 3`):** An `ASK` turn pauses with `needs_parent`; reply on reusable workers with `claive answer ID --message ...`. Workers cannot launch claive workers (`CLAIVE_WORKER_ID` guard).

## Worktrees, checks, and acceptance

- **One writer per isolated worktree:** Pass `--worktree` (all engines). Add `--carry-dirty` to copy uncommitted parent edits without committing, `--link-deps` to symlink `.venv`/`node_modules`, and `--worktree-setup CMD` for one-time setup. Clean up with `claive worktree list` / `clean --dry-run`.
- **Declare checks and ownership at launch:** Pass `--check NAME=CMD` (pipelines/`cd` auto-wrap in `bash -lc`) or `--checks-file` (`[...]` or `{"checks": [...]}` with `"command"` or `"shell"`), plus repeated `--owned-path`. Add `--verify-in-worktree` when checks need `.git`, `node_modules`, or `.venv` (newly created gitignored build outputs like `dist/` do not fail mutation checks).
- **Separate execution, verification, and acceptance:** After settlement, inspect `claive show ID --json`, run `claive verify ID`, and integrate/accept with `claive apply ID --close` (or `claive accept ID`). Never invent `--exception` to pass a failed mandatory check.

## References (load only when needed)

- `references/hosts-and-waits.md` — Per-harness wait/hook recipes (`AGY`, `Claude`, `Codex`, `Pi`, `OpenCode`, `T3 Code`), `claive hook`, push sinks (`--notify-worker`, `notifications.sinks`), `claive-mcp`, and peer/scheduled messaging (`send`, `ask`, `conversation`, `schedule`).
- `references/engines-and-budget.md` — Engine capability table (`claude`, `codex`, `pi`, `agy`, `muse`), sandbox/config flags, `extra_args`, and Claude budget recovery (`exit 3` / `budget_recovery`).
- `references/ladder.md` — `claive-orch` verifier-gated ladder (`init`, `next`, `integrate`, `cleanup`), parallel fan-out, `claive-memcap`, and quota fallbacks.
