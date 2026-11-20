# Host Wait Mechanics, Hooks, and Messaging

Full CLI syntax and exit codes live in `~/repo/claive/docs/command-reference.md` and `docs/harness-integration.md`.

## Per-harness wait and inbox patterns

- **Antigravity (AGY):** `claive start` exits in ~50ms. Arm `claive wait ID ... --all --heartbeat 600 --timeout 1800` via `run_command` with `WaitMsBeforeAsync: 500` before ending your turn. On `exit 125`, inspect the preview, steer/cancel if stuck, and re-arm.
- **Claude Code:** Run `claive wait ... --all --heartbeat 600` in Bash with `run_in_background: true` (or foreground `--timeout 540` with Bash `timeout: 600000`). Foreground `sleep` is blocked. Consumer: `claude` (or `--session`).
- **T3 Code:** Delegate to the configured Claive ACP provider for a native task row and completion wake. For Claive launches from the shell, use the wait or park behavior of the thread's harness.
- **Codex CLI:** Use `claive-worker` in a managed shell session with a short initial yield, keeping both worker ID and shell session ID (`claive-routing-codex` covers Codex-host specifics). Non-reactive after turn end: wait in-turn or `--park`.
- **Pi / OpenCode / shell:** Run via bash (`--session` auto-tags `$PI_SESSION_ID` / `$OPENCODE_SESSION_ID`).

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
