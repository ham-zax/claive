# Design note: let workers use their engine's own tools

Status: approved 2026-10-15. All slices (S1 to S5, and the Codex/Muse web change) are merged
into main, not committed. Still open: the live Claude `ExitPlanMode` check in `--print`.

## Intent

Each model works best with the tools and the work pattern that its own harness gives it. One
model reads a `git diff` before anything else. Another greps first. A third runs the tests to
find the failure. When claive replaces the harness's tool set with its own short list, it breaks
that rhythm. The worker then works around the gap: for example, a read-only Claude worker with
only Read, Grep and Glob reads full files because it cannot run `git diff`. That costs context
and time, and the result is worse.

claive is a supervisor. It is not a second sandbox. Its jobs are to start, track, steer, verify
and record workers. The engines (Claude Code, Codex, Muse, Pi) own their tools and their safety
modes.

## Principles

1. **Default: the engine's full default tool set.** claive passes no tool list.
2. **Read-only uses the engine's own read-only mode** (Claude plan mode, the Codex sandbox, and
   so on). claive never makes a read-only mode from a short tool list or command patterns.
3. **An engine with no own read-only mode refuses `--read-only`.** The refusal is honest. A fake
   read-only mode is not.
4. **Limits are opt-in configuration, not defaults.** Where a user wants a limit, they set it in
   `config.json`. If one option for each limit makes claive too complex to maintain, the limit
   goes away, and one generic setting (D6) takes its place.
5. **These stay**, because they are not tool limits: the recursion guard (workers do not start
   workers or send messages to workers), the user's policy in `config.json` (engine pins, the
   models allowlist, the workspace allowlist), turn timeouts, and parent verification and
   acceptance.

## Current limits (2026-10-15)

| # | Engine / place | Limit now | Code |
|---|---|---|---|
| L1 | Claude, all turns | `--tools Read,Edit,Write,Bash,Grep,Glob` | `engines/claude.py` WRITE_TOOLS |
| L2 | Claude, read-only | `--tools Read,Grep,Glob` with `bypassPermissions` | `engines/claude.py` READ_TOOLS |
| L3 | Claude, all turns | `--setting-sources ""`, `--strict-mcp-config`, `--disable-slash-commands` | `engines/claude.py` build_command |
| L4 | Claude, Codex, Muse | web tools off unless `--web`; Pi refuses `--web` | each engine |
| L5 | Pi, write | `--tools read,bash,edit,write,grep,find,ls` | `engines/pi.py` |
| L6 | Pi, read-only | `--tools read,grep,find,ls` (no bash) | `engines/pi.py` |
| L7 | Muse, read-only | `--disable-write --disable-shell` | `engines/muse.py` |
| L8 | `worker` role | 100 model steps | `roles.py` |
| L9 | Codex, all turns | `--ignore-user-config` (no `config.toml`: no MCP servers, profiles or features); opt in with `CLAIVE_CODEX_USE_USER_CONFIG=1` | `engines/codex.py` |
| L11 | Pi, all turns | `--offline`, `--no-extensions`, `--no-approve`; skills, MCP and AGENTS.md load | `engines/pi.py` |
| L10 | Codex with steering | a private `CODEX_HOME` with only `auth.json` and `sessions` (no skills), and `project_doc_max_bytes=0` (the repo's `AGENTS.md` is not read) | `engines/codex_appserver.py` |
| -  | Codex, read-only | `--sandbox read-only` (Codex's own mode; the shell stays) | keep as is |
| -  | Codex, write | `--dangerously-bypass-approvals-and-sandbox` (full access); `CLAIVE_CODEX_YOLO=0` gives the `workspace-write` sandbox | keep as is |
| -  | Codex, all turns | `--ignore-rules` (no execpolicy `.rules` files). This removes command limits; it does not add them | keep as is |

## Proposed changes

### C1. Claude: default tools, plan mode for read-only (L1, L2)

- Write turns: `--tools default` with `bypassPermissions`.
- Read-only turns: `--tools default` with `--permission-mode plan`.
- Live test (2026-10-15, Haiku, `--print`): in plan mode the worker ran `git diff` and `git log`
  and did not write with Write, `touch`, a `>` redirect or `git diff --output`. The model itself
  refused those calls, so the test did not prove that the harness blocks a write. Claude's
  documentation says that plan mode blocks edits and commands that change files.
- Before the change: test that `ExitPlanMode` is refused in `--print` mode (nobody can approve it),
  and look for a harness-level denial in the event stream.

### C2. Settings isolation for Claude, Codex and Pi (L3, L9, L11). D1, approved 2026-10-15

These flags are not tool limits for the model. They stop the user's own Claude setup (hooks,
permissions, MCP servers, skills, slash commands) from running inside a worker. If they are
removed, the parent's hooks run in each worker, the parent's MCP servers start in each worker, and
the parent's skills and rules (for example "delegate to claive workers") enter the worker's
context.

Codex has the same question: `--ignore-user-config` keeps out `config.toml`, which holds the
user's MCP servers, profiles and feature flags. Today the user can opt in with
`CLAIVE_CODEX_USE_USER_CONFIG=1`.

Recommendation, the same rule for both engines: capabilities come in, the parent's control
machinery stays out.
- Claude: remove `--strict-mcp-config` and `--disable-slash-commands` (MCP servers and skills
  are capabilities). Keep `--setting-sources ""` (settings carry the parent's hooks and
  permission rules).
- Codex: load the user's skills by default. Keep `--ignore-user-config` as the default, because
  `config.toml` mixes MCP servers with the model, sandbox and approval settings that claive sets
  for each turn; keep `CLAIVE_CODEX_USE_USER_CONFIG=1` as the opt-in, and also accept it as
  `engines.codex.user_config: true` in `config.json`.
- Pi: Pi is the most customizable of the four harnesses, so the same isolation applies. Keep
  `--no-extensions`: extensions change Pi's own loop (hooks, approvals, plan mode, and
  `pi-subagents`, which can start agents outside claive's recursion guard). Also pass
  `--no-prompt-templates` and `--no-themes`. Skills, MCP tools and context files stay on, as
  now (capabilities).

### C3. Web (L4). User decisions, 2026-10-15

- Claude: WebSearch and WebFetch stay in the default tool set on every turn. claive does not
  remove them, and `--web` is not needed for Claude.
- Codex and Muse (user decision, 2026-10-15): web on for every turn, the same as Claude. `--web`
  is accepted and has no effect.
- Pi has no built-in web tool; it keeps refusing `--web`.

### C9. Codex with steering reads the repo's AGENTS.md (L10)

The `codex exec` path reads the workspace's `AGENTS.md`. The steering path sets
`project_doc_max_bytes=0`, so the same worker gets different project rules when steering is on.
Remove `project_doc_max_bytes=0`. Give the private `CODEX_HOME` the user's `skills` folder too,
by the D1 rule.

### C4. Pi: no tool list for write turns (L5)

Drop `--tools` for write turns, so Pi loads its built-in tools and the user's extensions.

### C5. Pi read-only (L6). Open decision D2

Pi has no own read-only mode. Its help says that extensions can add `--plan` (a plan-mode
extension).

- Option a: refuse `--read-only` on Pi.
- Option b: use a Pi plan-mode extension, if one exists and the harness enforces it.
- Option c: keep the short tool list, but only when the user enables it in `config.json`.

Spike (2026-10-15): the plan-mode code is an example in Pi's npm package, loaded with
`-e <path> --plan`. It is not a Pi CLI mode. It filters bash with text patterns, and
`git diff --output=z` gets through.

Decision (user, 2026-10-15): Pi has no official CLI read-only mode, so Pi read-only turns get the
full default tools and a read-only instruction before and after the task text ("This is a
read-only turn. Do not create, change or delete files ..."). This is a request to the model, not
enforcement. The concurrent writer guard and ownership checks still find a write after the turn.

Effects of (a) that need a decision:
- The built-in roles `scout`, `reviewer` and `oracle` are Pi and read-only. They become write
  roles, with "do not edit" in their preamble, or they move to Codex read-only.
- `claive-orch` critics are Pi read-only. A critic that writes into a lane worktree corrupts the
  lane. Ownership checks would find the write, but only after it occurred.
- A workspace that is read-only in the workspace allowlist cannot run Pi workers.

### C6. Muse read-only (L7). Open decision D3

Muse has its own sandbox, approval modes and permission profiles. Check whether
`--disable-write` with the sandbox on (no `--disable-shell`, no `--disable-approval`) keeps shell
writes out of the workspace, or whether a read-only permission profile exists. If so, use that
and keep the shell. If not, keep `--disable-write --disable-shell`, because these are Muse's own
flags.

Spike (2026-10-15): `--disable-write` alone lets shell writes through. The official CLI flag
`--permission-profile :read-only` makes the filesystem refuse writes ("Read-only file system").
It cannot be combined with `--approval-mode`.

Decision (user, 2026-10-15): Muse read-only uses `--permission-profile :read-only` with the shell
on. Before the change, one live read-only review must complete (the spike run stalled after the
refusal); if it stalls, keep `--disable-write --disable-shell`.

Live check (2026-10-15): Muse also refuses `--permission-profile` with `--disable-approval`, so
read-only turns drop `--disable-approval`. With `--permission-profile :read-only --disable-write`
a `git status`/`git diff` review completed (exit 0), and a `touch` probe completed with a refusal;
neither run stalled. Implemented.

### C7. No step cap on the `worker` role (L8)

Set `max_model_steps` to null. `--max-model-steps` and role overrides in `config.json` still let a
user set a cap. Approved by the user, 2026-10-15.

### C8. One generic opt-in setting instead of one option for each limit. Open decision D6

`config.json`: `"engines": {"claude": {"extra_args": ["--tools", "Read,Grep,Glob,Bash"]}}`.
claive adds these arguments to each command for that engine. It refuses flags that claive owns
(output format, print mode, model, effort, session and resume flags). Other flags, such as a tool
list or a permission mode, are the user's own choice. With this one key, a user can put back any limit, and claive
does not need a separate option for each tool.

Decision (user, 2026-10-15): add `extra_args`, and add no other tool options.

Implemented: the config check refuses `--` and each owned flag (also as `--flag=value`). The
arguments go before the command's `--`, or at the end. Codex steering and fork turns run through
the app-server wrapper, which cannot take `codex exec` flags, so such a turn with
`engines.codex.extra_args` set is refused.

## Effects on other parts

- **Workspace allowlist:** a read-only workspace still needs `--read-only`. The engine's own mode
  then enforces it.
- **Concurrent writer guard and ownership checks:** no change. They still find writes after the
  turn.
- **Tests:** `tests/test_workers.py`, `tests/test_pi_engine.py` and the Claude engine tests check
  the exact command lines. They change with each slice.
- **Docs:** README, `docs/harness-integration.md`, `docs/pi-engine.md`, and both skills describe
  the current limits. They change in the planned docs update.
- **Cost:** the Claude default tools include subagents (Task). A Claude worker can then start
  Claude subagents inside its own turn. This is Claude's own feature, and claive cannot see or
  count those subagents.

## Plan

1. Decisions D1, D2, D3, D6 and the Codex/Muse web question: all made by the user, 2026-10-15.
2. Short checks: Claude `ExitPlanMode` in `--print`; Pi plan-mode extension; Muse sandbox with
   `--disable-write` only. Each check uses a throwaway git repo and a forced write by a tool, a
   shell command, a redirect and `git diff --output`.
3. Do the work after the current merge (items F, G, H to M and the retro fixes), on the merged
   main, because the same files change. Slices: S1 Claude (C1, C2, C3), S2 Codex (C2, C9), S3 Pi (C2, C4, C5),
   S4 Muse (C6), S5 role cap and `extra_args` (C7, C8).
4. One worker for each slice, in its own worktree. Review, merge, run the full suite once.
5. Update the docs and skills with the rest of the planned docs update.

## Do-not-add rule

New features do not add tool lists, command allow lists or tool patterns for workers. If a limit
seems necessary, write it as a proposal with the reason and wait for the user's approval.
