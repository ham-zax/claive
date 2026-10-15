---
name: claive-routing-codex
description: Codex-specific delegation through Claive. Launch, supervise, and reuse Muse, Pi, Claude, or Codex workers when authorized; use Codex managed terminals, worktree/read-only controls, and verification without changing task scope.
---

# Claive routing — Codex host

For Codex workers, pass `--engine codex --model gpt-6.1-sol` explicitly; Pi is available explicitly with `--engine pi` through `opencode2api`. Follow the global AGENTS.md delegation criteria; complete small or tightly coupled tasks directly when delegation would add overhead. The parent owns synthesis, integration, and final verification.

The canonical copy is the Skillshare source at `~/.config/skillshare/skills/claive-routing-codex/`: edit it there, then `skillshare sync`. `skills/claive-routing-codex/SKILL.md` in the Claive checkout (`/home/hamza/repo/claive` on the laptop, `/home/ubuntu/repo/claive` on ARM) is a tracked mirror; copy the edited file into it. Do not run the legacy skill installer over Skillshare links. `reference/global-AGENTS.md` is a snapshot, not a replacement for global instructions.

For a task with an executable check, prefer the verifier-gated ladder in the `claive-orchestration` skill (`claive-orch`: Muse implements, a cross-family Pi critic reviews, and the arbiter accepts or reverts each round). This skill governs Codex launch mechanics. The TTC experiment is deferred and has no active skill; do not auto-load or reinstall its former materials.

## Current engine boundaries (Claive main, October 2026)

The general `claive` worker manager supports **four engines**; the optional
`claive-orch` verifier-driven arbiter currently selects **Muse/Pi** candidates.
Do not confuse the arbiter's model policy with a limitation of the worker manager.
Select an engine deliberately and follow the parent's current authorization policy.

| Engine | Runtime contract |
| --- | --- |
| `muse` (default) | Pinned `muse-spark-1.3-contributor`; usual substantive effort `xhigh`; supports managed worktrees and read-only isolation |
| `pi` | `--engine pi --provider opencode2api`; models selected explicitly or inherited; no managed worktree, `--web`, or model-step cap |
| `claude` | `--engine claude`; default `claude-haiku-5-5`, allowed models set by `engines.claude.models` in the claive config; context budget mode on by default (`[claive-handoff]` replies continue the turn in a new session, `budget_limit` exits 3); headless with parent settings/hooks excluded, MCP and skills loaded; no managed worktree or model-step cap; `--output-schema` returns schema-validated JSON; `--enable-steering` for live steering (budget mode turns it on) |
| `codex` | `--engine codex`; pinned `gpt-6-luna` at effort `max` (default) or `gpt-6.1-sol` at `low`, `medium`, `high` or `xhigh` (`medium` when no effort is passed; `max` and `ultra` are refused); writer runs without the Codex sandbox by default, read-only uses the read-only sandbox; no managed worktree, output schema, or model-step cap |

For Claude handoffs, exit-3 recovery, or an unanswered `ask` across sessions, follow
**Claude budget recovery** in `claive-orchestration`.

Claude/Codex workers are **available**, not automatically authorized as a
fallback or as critics for the Muse/Pi arbiter. Do not silently switch engines,
relax sandboxing, or assume another model's worker permissions carry over.
Codex skills load by default. `engines.codex.user_config: true` or
`CLAIVE_CODEX_USE_USER_CONFIG=1` opts into the user's `config.toml`, including MCP.
Exec turns retain `--ignore-rules`. App Server turns do not pass that flag.
The App Server path links user auth, sessions and skills and reads repo `AGENTS.md`.
A writing Codex worker runs without the Codex sandbox or approvals by default, so
scope it in the brief; `CLAIVE_CODEX_YOLO=0` restores the workspace-write sandbox. Neither setting installs
new MCP servers. Consult `docs/harness-integration.md` for the runtime protocol.

| Engine | Read-only | Web | Steering / fork | Step cap | `extra_args` |
|---|---|---|---|---|---|
| Claude | Plan mode, default tools | Always on | Yes / yes | Refused | Yes |
| Codex | Read-only sandbox | Live every turn | Yes / yes | Refused | Exec only |
| Muse | `:read-only` profile, shell on | Always on | No / no | Opt-in | Yes |
| Pi | Full tools plus an instruction, no enforcement | `--web` refused | Yes / yes | Refused | Yes |

Steering needs `--enable-steering`. Fork needs an idle reusable session.
`--web` has no effect on Claude, Codex and Muse.
Claude keeps `--setting-sources ""` and uses `--tools default`.
Claude loads repository CLAUDE.md files and `~/.config/claive/claude/CLAUDE.md`.
Write turns use `bypassPermissions`, read-only turns use `plan`.
Live check, 2026-10-15: `ExitPlanMode` is unavailable in `--print`.
Model refusals did not prove a harness-level write block.
Config reloads when a command needs policy. `enabled: false` disables an engine.
`extra_args` is opt-in. It refuses `--` and owned flags, including `--flag=value`.
Codex steering and fork turns refuse nonempty `extra_args`.
Exact options: `docs/command-reference.md`.

For routine work from the Codex parent, favor `claive-worker` in Codex's managed
shell so its session remains visible in `/ps`; use `claive start` or
`claive open --detach` only when genuinely detached execution is needed.
Keep the terminal session ID, Claive worker ID, and engine session UUID distinct.

## Model and reasoning

Use **only `gpt-6.1-sol`** for Codex workers. Do not substitute a different Codex model/provider or route through agy/OpenCode's historical defaults without explicit fallback authorization below. Their relay copies in `reference/` are archival. Native host subagents require an explicit request.

- **`xhigh`: reasoning, not implementation.** Planning, logic or design debates, and other important high-level discussion.
- **`medium` or `low`: mechanical implementation**, where the assignment states the exact steps. `gpt-6.1-sol` runs at `medium` when no effort is passed.
- **`high`: complex implementation**, where the assignment gives a goal and the worker decides what to do and how.
- **`max` and `ultra`: never.** The engine refuses both for `gpt-6.1-sol`.
- **Pi workers: always `max`** (the Pi engine default). They are free; Pi clamps `max` to each model's highest supported level.
- **Pi models:** prefer `mimo-v2.6-flash-free`, `big-pickle`, `space-bunny-free`; `longcat-2.5-preview-free` only rarely. Do not use `nemotron-*` or `ling-3.1-flash-free`.

The orchestrator can select effort per turn or change an existing worker's future default at any time. Changes apply to the next request; they cannot change an in-flight API call. Muse has no default step cap. It passes `--max-model-steps` only when a flag or role/config value sets it. Pi rejects this flag. A step cap is not a monetary budget or timeout.

## Prepare the assignment

Write one narrow task to an absolute scratch prompt-file path. Include:

`mission | inputs | authorized scope | write ownership | required output | stopping condition`

Supply the absolute repository path, relevant files and symbols, existing evidence, constraints, and acceptance criteria. Workers do not inherit the parent conversation. MCP access follows the engine configuration above. Pass relevant graph findings and coverage gaps when the assignment relies on them.

Tell the worker it is not alone in the codebase: preserve other edits and accommodate them. Restrict writes to its assigned files. Forbid recursive delegation, commits, pushes, and other external writes unless the assignment explicitly authorizes them. Workers may make local, reversible implementation choices; they must report a blocker rather than expand scope or make a consequential decision outside the assignment.

Request a concise report: outcome, files changed, checks with results, supporting evidence, local decisions, uncertainty, blockers, and actual worktree path when isolated. Use exact check commands when known and appropriate to the authorized task. Avoid raw context dumps.

## Launch and observe workers

Default to `/home/hamza/.local/bin/claive-worker` inside Codex's managed shell tool. The launcher opens a reusable worker and remains alive between related turns, so a yielded shell call is a managed background terminal and appears in the native running-terminal indicator and `/ps`. The installed manager is `/home/hamza/.local/bin/claive`; its generic supervisor owns state, follow-ups, cancellation, and logs, while each engine adapter builds its commands and interprets its events and usage. Private logs are retained under `${XDG_STATE_HOME:-$HOME/.local/state}/claive`. `CLAIVE_DIR` can select another absolute registry directory. The custom dashboard and tmux pane are optional additional views.

```bash
exec /home/hamza/.local/bin/claive-worker \
  --workspace "$task_workspace" \
  --prompt-file "$task_prompt" \
  --label "Implement assigned feature" \
  --reasoning-effort xhigh \
  --max-model-steps 100
```

For a Muse worker that needs isolation, add `--worktree`; `--worktree-base` selects a base commit (default `HEAD`; uncommitted parent edits are not included). claive creates the worktree itself (Muse 1.4.3's `-w create` fails under its sandbox) at `$CLAIVE_DIR/<id>/worktree`, detached for `--read-only`, on branch `claive/<id>` for writers, and it lasts across a reusable worker's follow-ups. When the worker ends, claive removes it unless a writer changed it; then the worktree and branch are kept, `show --json` lists `retained_worktree`, and you review and integrate from there. For a worktree you manage yourself, pass `--worktree-existing /absolute/worktree`. For Muse read-only workers add `--read-only`. It passes `--permission-profile :read-only --disable-write`, keeps the shell on, and omits `--disable-approval`. Web tools stay on. The Muse engine accepts only the pinned model. `--output-schema` is forwarded. Reusable workers require session logging; `--no-session-log` is rejected by this launcher. `--provider echo` is for local transport checks, not evidence that the live model works.

Use `exec_command` with a short initial `yield_time_ms` (for example 1000). A returned `session_id` means the shell command is still running: retain it and use `write_stdin` to collect output and final exit status. Independent workers can each have their own managed session; shell yielding lets the parent continue working without detaching the worker. After a turn, the reusable worker remains idle and available in the managed terminal until closed. An idle supervisor makes no model requests.

For Pi, use the same launcher with `--engine pi --provider opencode2api` and omit `--max-model-steps`. Omit `--model` to inherit the last selected Pi model from its global settings; the initial model is `muse-spark-1.3-contributor-free`. An explicit `--model ID` becomes the shared selection after launch-option validation, even if the subsequent model request fails; role models, batch stage models and timeout fallbacks do not. Existing workers and their follow-ups retain their launch model. This preference is separate from Muse's pinned model and never switches engines automatically. Pi supports only `opencode2api` here. Use `--workspace` for an externally retained worktree; Pi rejects worktree-management, `--web`, and `--output-schema`. Pi uses `--tools '*'` in both modes, including bash.
Read-only adds an instruction through `--append-system-prompt` and after the task.
This is a request to the model, not enforcement. Pi has no OS sandbox.
It passes `--offline --no-extensions --no-prompt-templates --no-themes --no-approve`.
Skills, MCP and context files load. See `/home/hamza/repo/claive/docs/pi-engine.md` for the verified runtime contract.

The shell session ID and the 12-character worker ID printed by `claive-worker` identify different objects; the retained worker `session_id` (a Codex thread ID for this engine) is a third identifier. Report both when a session is yielded. Keep ownership through the related task chain and final closure. Do not append `&`, use `nohup`/`disown`, or replace the default with `claive start`: those detach the worker from Codex's managed terminal. `start` is available for deliberately detached jobs and returns immediately; such jobs remain visible in the registry dashboard but not in Codex's terminal indicator.

Async options (host-neutral; full protocol in `/home/hamza/repo/claive/docs/harness-integration.md`). When several workers run at once, or a worker would outlive the shell session, use `claive open --detach` (reusable, returns at once) or `claive start`, then `claive wait ID1 ID2 --any --timeout S` (first worker to settle; exit 124 means still running), with `S` below the tool's time limit; add `--inbox codex` to also return on new inbox events. Give a reusable worker context with `claive send ID --message ... [--mode note|wake|now]` (`note` waits for its next turn); question it with `claive ask ID --message ... --timeout 120` (0 answered, 124 withdrawn/expired) and read its recent steps with `claive peek ID`. At the start of each turn, and after context compaction, run `claive inbox --consumer codex` to see finished turns since the last read; `ASK` lines carry a worker's question (answer with `claive answer ID --message ...`; exit 3 means the same). Parallel staged work: `claive batch start /abs/plan.json`, then `claive batch wait ID`. Multi-turn efforts: `claive mission new --title T --goal-file F`, `export CLAIVE_MISSION=<id>`, record decisions with `claive mission note`, and resume with `claive mission show ID` (its `Next:` line). Unattended server queue: `claive goal add` plus `claive serve`; answer parked goals with `claive goal answer ID --message ...`. Name a reusable worker with `claive alias set NAME ID` (`@NAME` then replaces the ID in worker commands). Workers can talk to each other: `claive conversation start @a @b --message-file /abs/topic.md --rounds 2 --timeout 300 --json` runs a bounded exchange between existing idle reusable workers and forwards each reply to the next (exit 3 = a question, 124/130 = inspect `pending_request.response_file` first; see `claive-orchestration`). Workers cannot launch workers or start conversations.

For cancellation use `claive cancel WORKER_ID`, then collect the managed session's final output. Forcibly killing a terminal can bypass supervisor cleanup; if interrupted, inspect the registry and stop any surviving worker. Use:

```text
claive watch                 # live view; Ctrl-C closes only the view
claive list                 # active jobs and recent results
claive show ID --json       # status, progress, exit code, log paths
claive logs ID              # recent JSONL events
claive logs ID --stderr     # diagnostic output
claive wait ID              # wait for assigned turns; idle means ready, not closed
claive followup ID --prompt-file /absolute/next.md --reasoning-effort xhigh
claive effort ID --reasoning-effort high # future default; current call unchanged
claive usage ID             # retained session provider token/cache counters
claive close ID             # drain queued turns, close supervisor, retain history
claive cancel ID            # stop the worker process group
claive status-line          # one-line counts for a shell/status bar
claive-codex [codex options] # Codex + live pane + tmux status bar
```

Report the task, worker ID, and managed shell session ID at launch and report its outcome at completion. Codex's terminal view tracks the managed command and its output; it does not read the registry's detailed engine lifecycle. The optional custom monitor shows jobs launched through `claive`; raw Muse, agy, native agents, and OpenCode invocations are not automatically discovered. A completed CLI run does not establish that the assigned work passed verification or parent acceptance. Use the ordinary-worker evidence controls in `claive-orchestration`: declare `--checks-file`/`--owned-path` before consequential work, run independent `verify`, then explicitly record parent `accept`. Blocked mandatory checks and ownership violations keep acceptance pending. Current/previous-turn warning counts are distinct from cumulative history; inspect structured turn evidence instead of treating a historical total as this turn's failure count. Unexpected supervisor exits are shown as interrupted, not successful. Closing the monitor does not cancel workers; end jobs explicitly when their purpose ends.

The manager keeps Codex's existing internal status-line configuration intact. Its tmux view adds an outer status bar and live pane in a separate session; it does not change other tmux sessions. No always-running monitor daemon is installed.

### Underlying Muse flags

Set these three options in both modes:

```text
--workspace <absolute-repo-path>
--trust-workspace
--json
```

Write turns also use `--disable-approval`. Read-only turns use
`--permission-profile :read-only --disable-write` instead.

Prefer `--prompt-file` over a positional prompt for more than a few lines. Write prompt contents with a file tool or a safely quoted literal heredoc; never interpolate untrusted text into shell code. Quote every path expansion. Capture stdout JSONL and stderr separately in scratch files.

Use the tracked launcher above for routine work. If a task requires a Muse option the manager does not expose, extend the manager within authorized scope or report the limitation; do not silently bypass monitoring. This underlying invocation documents the manager's contract. It assumes `task_workspace`, `task_prompt`, and `task_logs` already contain absolute paths and `task_session` is the retained UUID for this lane, the prompt exists, and the log directory exists:

```bash
if /home/hamza/.local/bin/muse exec \
  --workspace "$task_workspace" \
  --trust-workspace \
  --disable-approval \
  --json \
  --model muse-spark-1.3-contributor \
  --session-id "$task_session" \
  --reasoning-effort xhigh \
  --max-model-steps 100 \
  --user-input-auto-resolve \
  --prompt-file "$task_prompt" \
  > "$task_logs/events.jsonl" 2> "$task_logs/stderr.log"
then
  task_rc=0
else
  task_rc=$?
fi
```

- Pin Muse to `--model muse-spark-1.3-contributor`. Default effort is `xhigh`. Muse supports `medium`, `high`, `xhigh` and `max`.
- Set `--max-model-steps` only when the assignment needs a cap. Muse has no default cap.
- `--user-input-auto-resolve` cancels interactive questions automatically. A worker that needs an answer must report a blocker; cancellation is not authorization.
- For Muse read-only work use `--permission-profile :read-only --disable-write`.
  Omit `--disable-approval`, which conflicts with the profile. The shell remains on.
- Web tools stay on. Claive's `--web` has no effect on Muse.
- `--output-schema /absolute/schema.json` shapes the final answer for the meta provider and, on `--engine claude`, makes the result the schema-validated JSON object; stdout remains a JSONL event stream. Pi and Codex reject it.
- Preserve session logging for related follow-ups. `--no-session-log` disables durable history and local messaging, and is allowed only for deliberate single-turn runs.

## Reuse and prompt caching

Keep one worker and durable Codex session for a related task chain. Keep it idle when a specific related follow-up is expected in the active workflow. If no further use is planned, close it and collect the managed terminal output; retained history can be reopened later. Do not leave an idle worker running indefinitely for hypothetical future tasks. Read `show ID --json` for `session_id`; send a narrow `followup` instead of launching a new session. A per-turn effort override does not change the future default; `effort ID` does. Follow-ups can also override `--max-model-steps`. The worker appends JSONL/stderr logs and keeps each answer in `turn-000N.txt`. Queued follow-ups run sequentially and do not change an active request. For supported live input, a Pi, Codex or Claude worker must be launched with `--enable-steering`; `claive steer ID --message ... --wait 10 --json` records provider acknowledgement at the next model boundary, not proof of consumption. The current Muse transport rejects this operation. Cancellation/restart is a separate action; do not silently substitute it for steering. `close ID` finishes assigned turns before ending; `cancel ID` stops immediately. After closure, resuming with `--session-id UUID` restores retained history; preserve workspace and tool policy. A `--worktree` worktree is created by claive and lasts for all turns of the worker; it is removed when the worker ends unless a writer left changes or commits (then `retained_worktree` in `show --json` names it). To resume a closed session on the same files, pass that worktree with `--worktree-existing`. Do not retire solely after two or four tasks. Start fresh for unrelated work or an actual context/quality limit.

Each underlying `muse exec` child exits after its turn; the supervisor stays alive and the same UUID reloads history next turn. This preserves conversation context across process exits. It is not a persistent model connection or proof of a cache hit.

Meta [prompt caching](https://dev.meta.ai/docs/prompt-caching) automatically reuses matching token prefixes on the server. Terminal exit does not itself clear that cache; eviction and inactivity can still cause misses. Keeping an idle process open cannot keep a server entry warm. Preserve stable leading instructions, tools, and history; put changing task details last, as in the [cookbook](https://dev.meta.ai/docs/cookbook/prompt-caching). Changing rules, model, reasoning, or compaction may change the rendered prefix; preserve the session when adjusting effort and measure the result rather than promising a hit.

Use `claive usage ID` (or `--json`) for provider-reported usage from Muse's retained session export. It counts each `model_completed` once, excluding repeated attribution records; no exposed usage means unknown, not zero. `cached_tokens > 0` proves reuse, and its ratio to reported input measures the observed hit rate. Totals include all recorded model calls in the retained session, including internal calls. These are accounting counters, not current context occupancy or an invoice; see [token usage](https://dev.meta.ai/docs/token-counting#usage). Do not add cached tokens again to input totals.

The API optionally exposes `prompt_cache_key` and Responses retention hints; this installed Muse exec CLI exposes neither. Do not invent CLI flags, set a new per-session cache key, change endpoints, or bypass Muse authentication to enable them. Automatic caching needs no extra flag. The current cache price and contributor quota effects are not established by a session UUID or these counters.

## Isolation and limits

- Concurrent Muse writers can each use `--worktree`; read-only workers may share a workspace. A writer's changed worktree is kept on branch `claive/<id>` after the worker ends. File ownership must remain disjoint even when worktrees are isolated.
- Worktree creation defaults to `HEAD`. Uncommitted parent edits will not automatically be present. Choose a suitable `--worktree-base` or explicitly transfer the required patch before dependent work. A `--worktree` worktree survives across the worker's reusable turns; use `--worktree-existing` for one you manage yourself or to continue a retained one.
- Give other shell workers their own isolated worktrees before concurrent writes; their relay scripts do not create them.
- Run heavy builds or test suites one at a time because this WSL environment has limited shared RAM. Independent light workers may run concurrently.
- Write turns disable approval prompts. Read-only turns use the permission profile. Both keep Muse's filesystem/network sandbox enabled; network defaults to `proxy-only`. Diagnose access failures from stderr and terminal events. Do not silently add `--yolo` or change sandbox permissions. Any exception must fit the task's authorization and host policy.
- Use managed shell sessions for long runs, retain their IDs, and poll with short waits while keeping the user informed. A yielded command is still running. Close workers when their related task chain ends and collect the managed terminal output; retain isolated changes until integration is complete.

## Validate and integrate

For a reusable worker, use `wait ID` to obtain the assigned turns' outcome without ending its session. On final closure, collect the managed process exit status too. For a direct single-turn invocation, wait for process completion and inspect `task_rc`. A non-zero exit code is failure even if an answer appeared. Do not hide failures behind a pipeline that only reports the parser's exit code.

Parse terminal events, not the last physical line or a partial output delta. The observed Muse schema uses `payload_type` beginning with `run.terminal.`, with status in `payload.terminal` and final answer in `payload.text`:

```bash
jq -es '
  [.[] | select((.payload_type? // "") | startswith("run.terminal."))]
  | last
  | select(.payload.terminal == "completed")
  | .payload
' "$task_logs/events.jsonl"
```

Require both exit code zero and a completed terminal event. Missing, malformed, failed, or cancelled results are failures. If schema changes, inspect the local event shape before changing the parser. A completed run can still report a blocker or incomplete work; evaluate the answer against the acceptance criteria. Inspect tool/task failures and stderr even when the terminal event reports completion.

Verify consequential claims against source. Inspect tracked and untracked changes in the actual worker workspace, preserve unrelated edits, and perform checks appropriate to the changed behavior. For isolated workers, review and integrate only authorized changes into the parent workspace, then verify the integrated result. A worker's report alone is not proof of completion.

On failure, retry only with a changed hypothesis or clearer assignment. Report access or backend limitations honestly. Do not silently relax restrictions, change providers, or delegate recursively to get around a failure.

## Five-hour quota and fallback

When Muse explicitly reports `Subscription quota exhausted`, stop sending assignments against that quota. Hamza has pre-approved one fallback: Pi `muse-spark-1.3-contributor-free` (`--engine pi --provider opencode2api`, effort `max`) for the affected lane. Switch to it, report the reset time if present, and tell Hamza the fallback is in use. Any other fallback model or subagent still needs his explicit choice. Return to Muse after the reset. Do not run repeated same-quota retries or treat every generic 429 as the five-hour subscription limit.

The manager records `quota_exhausted`, `quota_reset_at` when provided, and `fallback_requires_user_approval`; `show`/`wait` print the fallback notice. It never launches an alternate provider itself. When switching to the fallback, preserve the original assignment's scope, write ownership, isolation, monitoring, and result checks with the chosen worker. The fallback does not alter Muse's pinned model or its xhigh normal default. Do not offer the broken OpenCode relay until repaired and verified. Close the Muse supervisor when no further use is planned; retained history can be resumed after reset.
