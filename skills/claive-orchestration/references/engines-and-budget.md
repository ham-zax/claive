# Engines, Policy, and Claude Budget Recovery

Check `claive engines --json` and `claive engine config NAME` before launching; do not hardcode models or efforts.

## Capability matrix

Workers use each engine's default tools (Claive passes no restricted tool list).

| Engine | Read-only | Web | Steering | Fork | Step cap | `extra_args` |
|---|---|---|---|---|---|---|
| `claude` | Soft/default tools (plan mode) | Always on | Stream JSON | Yes | Refused | Yes |
| `codex` | Read-only sandbox | Live every turn | App Server | Yes | Refused | Exec only |
| `pi` | Soft instruction, full tools | `--web` refused | RPC | Yes | Refused | Yes |
| `agy` | Soft instruction, full tools | Refused | Refused | Refused | Refused | Yes |
| `muse` | `:read-only` profile, `--disable-write` | Always on | Refused | Refused | Opt-in | Yes |

## Engine-specific notes

- **Use `--require-read-only`** when read-only enforcement is a hard requirement (Claude, Pi, and AGY use soft instructions without hard write barriers).
- **Claude:** Write turns use `bypassPermissions` and `--setting-sources ""` (parent hooks stay out; MCP/skills and repo `CLAUDE.md` + `~/.config/claive/claude/CLAUDE.md` load; `~/.claude/CLAUDE.md` does not).
- **Codex:** Repo `AGENTS.md` and skills load; `~/.codex/config.toml` is opt-in via `engines.codex.user_config: true` or `CLAIVE_CODEX_USE_USER_CONFIG=1`. Steering/fork refuse nonempty `extra_args`.
- **Pi:** Runs `--offline --no-extensions --no-prompt-templates --no-themes --no-approve`. Passing `--model` updates Pi's global default; omit it only when inheriting the current default is intended.
- **AGY:** Accepts full slugs or family stem + `--effort` (`--model gemini-4-hydrogen --effort high`). Usage totals are aggregate telemetry, not discrete model calls.
- **`extra_args`:** Accepts nonempty strings (refuses `--` and Claive-owned flags).

## Claude budget mode and recovery

- Claude context budget mode is on by default (`engines.claude.budget`, turned off by `budget: false` or `--output-schema`).
- At the stop limit, Claive denies further tool calls via `PreToolUse`, captures a `[claive-handoff]` (requires a nonblank `## Owed to the parent` section), and continues silently (`wake: false` on `budget_handoff`) up to `max_handoffs`, after which it exits `3` (`budget_limit`).
- **On `exit 3` (`budget_limit` / `budget_recovery`):**
  1. Read `claive show ID --json` for artifact paths and restore any missing or corrupt inputs.
  2. Resume a reusable worker with `claive answer ID` or `followup`, or launch a fresh worker from the saved handoff context.
  3. An unanswered `ask` stays open across a budget pause: retry the same question ID and schema after recovery (its original deadline still applies).
  4. Treat intermediate handoffs as recovery state, not final answers; verify claims before accepting.
