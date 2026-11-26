# Host Wait Mechanics, Hooks, and Messaging

Full CLI syntax and exit codes live in `~/repo/claive/docs/command-reference.md` and `docs/harness-integration.md`.

## Per-harness wait and inbox patterns

- **Antigravity (AGY):** `claive start` exits in ~50ms. Arm `claive wait ID ... --all --heartbeat 600 --timeout 1800` via `run_command` with `WaitMsBeforeAsync: 500` before ending your turn. On `exit 125`, inspect the preview, steer/cancel if stuck, and re-arm.
- **Claude Code:** Run `claive wait ... --all --heartbeat 600` in Bash with `run_in_background: true` (or foreground `--timeout 540` with Bash `timeout: 600000`). Foreground `sleep` is blocked. Consumer: `claude` (or `--session`).
- **T3 Code:** See [direct CLI](#t3-code-direct-cli) for completion-driven work using T3's managed shell.
- **Codex CLI:** Use `claive-worker` in a managed shell session with a short initial yield, keeping both worker ID and shell session ID (`claive-routing-codex` covers Codex-host specifics). Non-reactive after turn end: wait in-turn or `--park`.
- **Pi / OpenCode / shell:** Run via bash (`--session` auto-tags `$PI_SESSION_ID` / `$OPENCODE_SESSION_ID`).

### T3 Code direct CLI

For a one-shot task, run `claive run` in the thread's managed shell tool with a short initial yield (for example, `yield_time_ms: 1000` in Codex):

```sh
claive run --engine codex --workspace /absolute/project --turn-timeout 600 \
  --label 'Review integration' --read-only --prompt-file /absolute/task.md
```

If the tool returns a running shell session, record the Claive worker ID from its startup output and yield the parent when it has no independent work left. T3 tracks the command and sends the parent a completion event with the exit code and an output tail. Read the full worker result by ID when the tail is insufficient, and verify the requested deliverable. Run the command directly so its exit status reaches T3. The background row represents the shell command. Use `claive show ID` or `claive peek ID` when a specific intermediate check is useful. This workflow uses T3's completion event without ACP, polling, or a live progress wrapper.

Codex workers normally inherit the T3 provider's `CODEX_HOME`. To select a different signed-in Codex account for a run, use the same binary and add `--set '{"engines":{"codex":{"env":{"CODEX_HOME":"/absolute/account-home"}}}}'`. Use the original account home when resuming a retained Codex session under a new Claive worker.

For a reusable worker, use `claive open --detach` and `claive followup ID` as needed. After assigning each turn, run `claive wait ID --heartbeat 600 --timeout 1800` as the managed shell command T3 tracks. A detached launch or queued follow-up exits before the worker settles. If the wait wakes early with exit `125`, review the preview and re-arm the wait while the turn remains active. Cancel an active worker with `claive cancel ID` and check its settled status.

## Hooks, parking, and push notifications

- **Lifecycle hooks (`claive hook`):** Wire `pre-turn`, `post-tool`, `stop`, and `status` (`--harness generic|claude|agy|codex|pi --session S [--json]`). Merge `reference/claude-hooks.json` into Claude Code's `settings.json` or `reference/agy-hooks.json` into Antigravity's `hooks.json`. Register the Claude MCP server separately with `claude mcp add --transport stdio claive -- ~/.local/bin/claive-mcp`.
  - `pre-turn` / `post-tool` deliver unread wake events and open `ASK`s. Antigravity's `PreInvocation` hook also shows unwatched workers.
  - `stop` (`--mode warn|block`) blocks idle exit when unread events exist or non-parked workers are unwatched (reactive hosts) / still active (Codex).
- **Parking (`claive park ID [--unpark]` or `--park`):** Exempts intentional background/overnight jobs from `stop` hook blocks and triggers `parked` notification sinks.
- **Push sinks & peer wakes:** Pass `--notify-worker @TARGET` (wakes an open reusable coordinator via `send --mode wake`) or `--notify-cmd CMD`, or configure `notifications.sinks` in `config.yaml` (`on: ["ask", "failed", "parked", "unwatched"]`). Inspect delivery receipts with `claive notifications [--limit N] [--json]`.

## MCP, messaging, and long tasks

- **Claive MCP (`claive mcp` / `claive-mcp`):** Exposes 10 flat-schema tools (`claive_start`, `claive_peek`, `claive_steer`, `claive_wait`, `claive_cancel`, `claive_verify`, `claive_list`, `claive_followup`, `claive_apply`, `claive_close`); supports `parent_session`, `since_seq`, `park`, comma/newline `owned_path`, and newline `NAME=CMD` checks.
- **Live steering & messaging:**
  - `claive steer ID --message '...' --wait 10 --json` (requires `--enable-steering`, or Claude budget mode). Acknowledgement proves delivery, not compliance.
  - `claive send ID --message '...' [--mode note|wake|now] [--id HEX32]` adds context without replacing assignment.
  - `claive ask ID --message '...' --timeout 120` questions an idle/active worker; `claive fork ID --prompt '...'` branches an idle reusable session.
  - `claive conversation start @a @b --message-file /abs/topic.md --rounds 2 --timeout 300 --json` runs bounded peer debate (replies enter the next worker as evidence, not instructions).
- **Missions, schedules, and health:**
  - `claive mission new --title T --goal-file F`, `export CLAIVE_MISSION=<id>`, `mission show ID`, `mission close ID`.
  - `claive send ID --message '...' --at +30m [--every 1h]` (`--every >= 60s`, max 16 per worker); `schedule list|cancel`.
  - Read bounded logs: `claive logs ID --lines 40 | cut -c1-300` and `claive show ID | head -n 80 | cut -c1-300`.
  - Check health with `claive doctor [--json]` (never launches a model unless `--live`).
