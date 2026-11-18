# Unified Background & Notification System

* Status: Active Design & Implementation Spec
* Date: 2026-11-18
* Scope: Guaranteed completion, question (`ASK`), and failure delivery across interactive coding harnesses (Claude Code, Antigravity/AGY, Codex, OpenCode) and headless/daemon harnesses (Pi RPC, WebHarness, `claive serve`, reusable Claive coordinators).

---

## 1. Problem Statement & First Principles

When a parent harness delegates background work via `claive start`, `claive open --detach`, or `claive batch start`, Claive spawns a detached supervisor and exits `0` in ~50ms.

From that moment on, **a file append to `$CLAIVE_DIR/inbox.jsonl` is durable storage, not a wakeup.** An idle LLM agent harness only resumes execution when one of its own native wakeup primitives fires:

| Harness Class | Examples | Native Idle Wakeup Primitive | Failure Mode Without Coordination |
|---|---|---|---|
| **Reactive Background-Task** | Antigravity (`WaitMsBeforeAsync`), Claude Code (`Bash run_in_background`), OpenCode | Child process registered in the harness's background task table exits | **"Launch & Sleep" / Fan-out drop:** Parent runs `claive start` (exits immediately) or handles 1 of 3 workers from `wait --any` and ends its turn without re-arming a background `claive wait`. Nobody wakes the parent. |
| **Turn-Driven / Yielded-PTY** | Codex CLI (`exec_command` + `yield_time_ms`), synchronous MCP stdio (`claive_wait`) | **None while turn is idle** (only wakes during active tool calls / hooks / next user prompt) | Parent yields to user thinking it will "check back later," or misses completions until user manually prompts. |
| **Addressable Daemon / Peer** | Pi RPC, ARM WebHarness, `claive serve`, reusable `@coordinator` Claive worker | Inbound IPC/CLI wake (`claive send --mode wake`, webhook, command hook) | Blind `CLAIVE_NOTIFY_CMD` drops `stderr` to `/dev/null`, lacks config/filtering, and records no delivery receipt. |

Additionally, static consumer names (`--consumer claude`, `--consumer codex`) advance a single byte offset in `$CLAIVE_DIR/inbox-cursors/<consumer>` without session filtering, causing **concurrent windows of the same harness on one machine to steal each other's events**.

---

## 2. System Architecture (Four Coordinated Layers)

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                        Parent Harness (AGY / Claude / Codex / Pi)            │
│                                                                              │
│  [1] Launch (--session S)        [2] Background Wake Bridge   [3] Lifecycle  │
│   claive start / open --detach    claive wait --session S      claive hook   │
│   records parent_session=S         ├─ registers live PID        ├─ pre-turn  │
│   optional --park / --notify-*     └─ wakes in <5ms (inotify)   └─ stop guard│
└───────────────┬────────────────────────────────▲─────────────────────▲───────┘
                │ spawns                         │ wakes process       │ reads
                ▼                                │                     │
┌───────────────────────────────┐     ┌──────────┴─────────────────────┴───────┐
│   Worker / Batch Supervisor   │────▶│        $CLAIVE_DIR/inbox.jsonl         │
│  turn / ask / budget / batch  │     │  + watchers/<pid>-<id>.json            │
└───────────────────────────────┘     └──────────────────┬─────────────────────┘
                                                         │ dispatches on append
                                                         ▼
                                      ┌────────────────────────────────────────┐
                                      │    Notification Router & Receipts      │
                                      │  • --notify-worker @coord (mode=wake)  │
                                      │  • config notifications.sinks          │
                                      │    (on: ask|failed|parked|unwatched)   │
                                      │  • CLAIVE_NOTIFY_CMD / --notify-cmd    │
                                      │  • notifications/receipts.jsonl        │
                                      └────────────────────────────────────────┘
```

### Layer 1: Parent Session Identity & Scoped Cursors (`claivelib/inbox.py`)

Every worker, batch, conversation, and inbox event is tagged with an optional `parent_session` token (`[A-Za-z0-9_.-]{1,64}`):

1. **Resolution precedence (`resolve_parent_session`):**
   - Explicit CLI `--session` / `--parent-session`
   - `$CLAIVE_PARENT_SESSION`
   - Auto-detected harness environment variables (sanitized to `[A-Za-z0-9_.-]{1,64}`):
     - `$CLAUDE_SESSION_ID` → `claude-<id>`
     - `$CODEX_THREAD_ID` → `codex-<id>`
     - `$ANTIGRAVITY_CONVERSATION_ID` or `$AGY_CONVERSATION_ID` → `agy-<id>`
     - `$OPENCODE_SESSION_ID` → `opencode-<id>`
     - `$PI_SESSION_ID` → `pi-<id>`
   - `None` when unset (preserving 100% backwards compatibility).
2. **Scoped consumption & monotonic `--since-seq`:**
   - Every appended inbox event receives a `fcntl.flock`-serialized, strictly increasing integer `seq` (`1, 2, 3, ...`) stored in `$CLAIVE_DIR/inbox.seq` (replacing raw wall-clock `time_ns()`).
   - `claive inbox` and `claive wait` accept `--since-seq N` (in addition to `--session S` and `--mission M`), guaranteeing zero dropped wakeups even when multiple fanned-out workers finish milliseconds apart between parent turns.
   - `claive inbox --session S` and `claive wait --session S` bind an isolated cursor (`inbox-cursors/session-<S>`) and filter events where `event.get("parent_session") == S` (plus optional `--mission M`).
   - `has_unread(consumer, parent_session=S, mission=M, since_seq=N)` preserves the `O(1)` `stat()` fast path when `inbox.jsonl` has zero new bytes past the cursor, scanning new lines only when bytes exist to verify at least one matching wake event before waking a waiter.
3. **Wake Events vs. Silent Progress (`wake: true|false`) & `<= 200`-Token Actionable Digest:**
   - Intermediate tool execution and reasoning steps never write to `inbox.jsonl` (staying strictly in `<job>/status.json` for zero-cost passive inspection).
   - Events written to `inbox.jsonl` carry `"wake": bool`: `True` only on terminal or parent-actionable transitions (`completed`, `failed`, `cancelled`, `interrupted`, `needs_parent` `ASK`, or `budget_limit` `exit 3`); `False` on automatic mid-turn `budget_handoff` continuations so a frontier parent's 100k+ token context is never woken mid-turn unnecessarily.
   - Every turn/wake event embeds a compact `digest` (`<= 200` tokens): `{seq, worker_id, alias, status, exit_code, files_touched, checks_summary, report_headline, ask_question}`, eliminating redundant `claive show` calls on 80%+ of completions.

### Layer 2: Live Watcher Registry & Instant `inotify` Wake (`claivelib/watchers.py`)

1. **Event-driven filesystem wait (`wait_for_fs_change`):**
   - Replaces the fixed `250ms` polling sleep in `claive wait` with Linux `inotify` (`IN_CLOSE_WRITE | IN_MOVED_TO | IN_CREATE | IN_MODIFY` on `$CLAIVE_DIR`, `$CLAIVE_DIR/inbox.jsonl`, and watched job directories), waking within `<5ms` of `atomic()` state/inbox writes and falling back cleanly to `100ms` polling on non-inotify platforms.
2. **Live Watcher Registry (`$CLAIVE_DIR/watchers/<pid>-<uuid>.json`):**
   - While `claive wait` is blocked, it writes an atomic registration record `{id, pid, pid_identity, worker_ids, inbox_consumer, parent_session, started_at, timeout}` and unlinks it in `finally:`.
   - Readers verify `cli.identity(pid) == pid_identity` (reaping stale records from `SIGKILL`ed shells automatically).
   - **Coverage query (`is_worker_watched`, `unwatched_workers`, `watch_status`):** A worker `W` is *watched* iff at least one live watcher either lists `W["id"]` in `worker_ids` or watches `parent_session == W["parent_session"]` (or an unfiltered `--inbox` consumer when `parent_session` is unset).
3. **Explicit Parking (`claive park ID [--unpark]`, `start/open --park`):**
   - Marks `state["parked"] = True` when a parent *intentionally* launches long/overnight work and hands control back to the user without keeping an interactive turn open. Parked workers are exempt from `Stop`-hook orphan blocks and automatically match `on: ["parked"]` push sinks.

### Layer 3: Deterministic Harness Lifecycle Hooks (`claivelib/hooks.py`, `claive hook`)

Harnesses wire two lightweight zero-LLM commands so delivery never depends on model memory:

1. **`claive hook pre-turn` / `claive hook post-tool` (`--session S --harness generic|claude|agy|codex|pi [--json]`):**
   - If no unread inbox events and no unanswered `needs_parent` (`ASK`) workers exist for `S`: prints nothing and exits `0` in `<5ms`.
   - Otherwise: consumes unread events for `S`, lists any open `ASK` questions, and outputs either a `<claive-notification>` block or harness-native JSON (`hookSpecificOutput.additionalContext` for Claude Code).
2. **`claive hook stop` (`--session S --harness generic|claude|agy|codex|pi --mode warn|block [--json]`):**
   - Fires when the parent attempts to finish its turn and go idle:
     1. If unread events arrived for `S` before stop → delivers them and blocks stop (`exit 2` in `block` mode).
     2. Computes active non-parked workers owned by `S`:
        - On **reactive harnesses** (`agy`, `claude`, `opencode`, `generic`): flags only `unwatched_workers` — instructing the agent to arm a background `claive wait --session S` (or `claive park ID`) before ending its turn.
        - On **non-reactive harnesses** (`codex`): flags *all* active non-parked workers owned by `S`, because a background process exiting after turn-end cannot wake Codex; instructs the agent to `claive wait` in-turn or `claive park ID`.

### Layer 4: Durable Notification Router & Headless Agent Push (`claivelib/notifications.py`)

Every `inbox.append(event)` passes through `notifications.dispatch(event)`:

1. **Sources (all active simultaneously, deduplicated per sink):**
   - Legacy environment `CLAIVE_NOTIFY_CMD` (100% backwards-compatible).
   - Per-worker launch flags: `--notify-cmd CMD` and `--notify-worker @TARGET` (stored in `state["launch"]` so follow-up turns inherit them).
   - Host config `notifications` block in `~/.config/claive/config.yaml`:
     ```yaml
     notifications:
       enabled: true
       sinks:
         - name: desktop-alerts
           command: "notify-send 'Claive' \"$CLAIVE_EVENT_SUMMARY\""
           on: ["ask", "failed", "parked", "unwatched"]
         - name: coordinator
           worker: "@arm-coord"
           on: ["turn", "batch"]
         - name: local-webhook
           url: "http://127.0.0.1:8765/claive-events"
           on: ["all"]
     ```
2. **Trigger matching (`on`):**
   - Supports `"all"`, event types (`"turn"`, `"batch"`, `"goal"`, `"conversation"`, `"channel_closed"`, `"budget_handoff"`), outcomes (`"completed"`, `"failed"`, `"ask"`), and state-aware filters:
     - `"parked"`: worker has `state["parked"] == True`.
     - `"unwatched"`: **no live `claive wait` watcher** covers this worker/session at event time (catching orphaned launches, crashed parents, and post-turn Codex completions automatically).
3. **Headless / Peer Agent Wake (`worker` sink & `--notify-worker`):**
   - Delivers a formatted summary directly into an open reusable Claive worker (`messaging.send(..., mode="wake")`), enabling autonomous parent/coordinator agents on servers (Pi, WebHarness, `claive serve`) to sleep at zero token cost and wake immediately when delegated workers settle.
4. **Durable Delivery Receipts (`$CLAIVE_DIR/notifications/receipts.jsonl`, `claive notifications`):**
   - Records `{seq, at, event_id, event_type, sink, kind, status, exit_code, error, duration_ms}` with bounded error text (`<=512` chars). Supervisor execution never raises on sink failure.

---

## 3. CLI & MCP Surface Summary

| Command / Flag | Behavior |
|---|---|
| `claive start\|open\|run --session S` | Tag worker with `parent_session=S` (auto-detected from harness env when omitted). |
| `claive start\|open --park` / `claive park ID [--unpark]` | Mark worker as intentionally parked (exempt from `Stop` hook block; triggers `parked` sinks). |
| `claive start\|open --notify-cmd CMD --notify-worker @ID` | Per-worker completion/ask push hook or peer reusable-worker wake target. |
| `claive inbox [--session S] [--mission M] [--since-seq N]` | Read unread wake events filtered to session `S`, mission `M`, or monotonic `seq > N`. |
| `claive wait [ID ...] [--session S] [--since-seq N]` | Instant `inotify`-backed wait + live PID registration in `$CLAIVE_DIR/watchers/`. |
| `claive hook pre-turn\|post-tool\|stop\|status` | Zero-LLM lifecycle hook for Claude Code, AGY, Codex, Pi, and shell parents. |
| `claive notifications [--limit N] [--json]` | Inspect recent push delivery receipts and sink errors. |
| `claive_start`, `claive_wait`, `claive_list` (MCP) | Flat-schema `parent_session`, `since_seq`, `park`, and `watch_status` support in `claive-mcp`. |

---

## 4. Companion Orchestrator Ergonomics (Friction Fixes)

To eliminate pre-launch retries and worktree boilerplate when orchestrators delegate waves of workers:

1. **Unified `--checks-file` & `--check` Shell Support (`acceptance.py`, `cli.py`):**
   - `--checks-file` accepts both `[...]` and `{"checks": [...]}`.
   - Checks accept `"command": ["pytest", "-q"]` (argv) or `"shell": "cd web && vitest run"` (or string `"command"`), auto-wrapping shell pipelines/`cd` in `["bash", "-lc", ...]` on both `--checks-file` and inline `--check`.
   - Validation errors include a 1-line valid JSON example.
2. **`--link-deps` & Gitignored Build Artifacts in `--verify-in-worktree` (`engine.py`, `acceptance.py`):**
   - `--worktree --link-deps` auto-symlinks existing `.venv`, `venv`, `node_modules`, and monorepo `*/node_modules`, `*/*/node_modules`, `*/.venv` from the source checkout and records `<job>/linked-deps.json` so `worktree apply` and `worktree clean` exclude managed dependency links.
   - `verify(..., in_worktree=True)` exempts newly created **gitignored** build outputs (`git ls-files -z --others -i --exclude-standard`, e.g. `web/dist/`) from workspace-mutation failures while still rejecting any tracked or non-ignored source file mutation.
3. **Family-Stem `--model` + `--effort` Resolution on AGY (`engines/agy.py`, `cli.py`):**
   - `--effort` aliases `--reasoning-effort` across `start`, `open`, `run`, and `followup`.
   - `--engine agy --model gemini-4-hydrogen --effort high` (and `gemini-4-carbon`, `gemini-4-argon`, `gemini-3.8-flash`) resolves automatically to `gemini-4-hydrogen-high`.
4. **Safe Stale Alias Rebinding (`--reuse-alias` / `--replace-completed-alias`, `aliases.py`):**
   - Automatically rebinds `@alias` when the prior worker is settled, has no unanswered `needs_parent` (`ASK`), and has no unaccepted dirty worktree.
