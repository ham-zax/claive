---
name: subagent-routing
description: Delegate bounded coding, investigation, or review tasks from Codex to Muse or Pi workers through claive-worker. Use when delegation is warranted or the user requests subagents; supports reusable sessions and provider usage checks.
---

# Subagent routing

Muse Code is the default engine; Pi is available explicitly with `--engine pi` through `opencode2api`. Follow the global AGENTS.md delegation criteria; complete small or tightly coupled tasks directly when delegation would add overhead. The parent owns synthesis, integration, and final verification.

The maintained source is `/home/hamza/repo/claive`. Edit its `bin/` scripts or `skills/subagent-routing/SKILL.md`, then run `install.sh` to refresh the installed copies. Its `reference/global-AGENTS.md` is a snapshot, not a file to install over current global instructions.

For a task with an executable check, prefer the verifier-gated ladder in the `worker-orchestration` skill (`claive-orch`: Muse implements, a cross-family Pi critic reviews, and the arbiter accepts or reverts each round). This skill still governs the Codex launch mechanics. For the refine-vs-resample experiment itself, use `ttc-experiment`.

## Model and reasoning

Use **only `muse-spark-1.3-contributor`** for Muse workers. The launcher pins this model rather than inheriting another default. Do not substitute a different Muse model/provider or route through agy/OpenCode's historical defaults without explicit fallback authorization below. Their relay copies in `reference/` are archival. Native host subagents require an explicit request.

- **`xhigh`: default for normal implementation, investigation, review, and other substantive work** (the Muse engine default).
- **`max`: very complicated work**, difficult fixes, or consequential reasoning.
- **`high`/`medium`: rarely**, only for trivial mechanical work such as generating a commit message.
- **Pi workers: always `max`** (the Pi engine default). They are free; Pi clamps `max` to each model's highest supported level.
- **Pi models:** prefer `mimo-v2.6-flash-free`, `big-pickle`, `space-bunny-free`; `longcat-2.5-preview-free` only rarely. Do not use `nemotron-*` or `ling-3.1-flash-free`.

The orchestrator can select effort per turn or change an existing worker's future default at any time. Changes apply to the next request; they cannot change an in-flight API call. Muse defaults to 100 model steps; Pi rejects `--max-model-steps`. Set a cap suitable for the assignment, including a higher cap when needed; a step cap is not a monetary budget or timeout.

## Prepare the assignment

Write one narrow task to an absolute scratch prompt-file path. Include:

`mission | inputs | authorized scope | write ownership | required output | stopping condition`

Supply the absolute repository path, relevant files and symbols, existing evidence, constraints, and acceptance criteria. Workers do not automatically inherit the conversation or MCP access. Pass relevant graph findings and coverage gaps when the assignment relies on them.

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

For a Muse worker that needs isolation, add `--worktree`; `--worktree-base` selects a base commit (default `HEAD`; uncommitted parent edits are not included). claive creates the worktree itself (Muse 1.4.3's `-w create` fails under its sandbox) at `$CLAIVE_DIR/<id>/worktree`, detached for `--read-only`, on branch `claive/<id>` for writers, and it lasts across a reusable worker's follow-ups. When the worker ends, claive removes it unless a writer changed it; then the worktree and branch are kept, `show --json` lists `retained_worktree`, and you review and integrate from there. For a worktree you manage yourself, pass `--worktree-existing /absolute/worktree`. For enforced read-only workers add `--read-only` (both write and shell are disabled). Web tools are disabled unless `--web` is supplied. The Muse engine accepts only the pinned model. `--output-schema` is forwarded. Reusable workers require session logging; `--no-session-log` is rejected by this launcher. `--provider echo` is for local transport checks, not evidence that the live model works.

Use `exec_command` with a short initial `yield_time_ms` (for example 1000). A returned `session_id` means the shell command is still running: retain it and use `write_stdin` to collect output and final exit status. Independent workers can each have their own managed session; shell yielding lets the parent continue working without detaching the worker. After a turn, the reusable worker remains idle and available in the managed terminal until closed. An idle supervisor makes no model requests.

For Pi, use the same launcher with `--engine pi --provider opencode2api` and omit `--max-model-steps`. Omit `--model` to inherit the last selected Pi model from its global settings; the initial model is `muse-spark-1.3-contributor-free`. An explicit `--model ID` becomes the shared selection after launch-option validation, even if the subsequent model request fails. Existing workers and their follow-ups retain their launch model. This preference is separate from Muse's pinned model and never switches engines automatically. Pi supports only `opencode2api` here. Use `--workspace` for an externally retained worktree; Pi rejects worktree-management, `--web`, and `--output-schema`. Pi read-only tools are `read,grep,find,ls`, with extensions disabled; Pi has no Muse OS sandbox. See `/home/hamza/repo/claive/docs/pi-engine.md` for the verified runtime contract.

The shell session ID and the 12-character worker ID printed by `claive-worker` identify different objects; the retained worker `session_id` (a Muse session UUID for this engine) is a third identifier. Report both when a session is yielded. Keep ownership through the related task chain and final closure. Do not append `&`, use `nohup`/`disown`, or replace the default with `claive start`: those detach the worker from Codex's managed terminal. `start` is available for deliberately detached jobs and returns immediately; such jobs remain visible in the registry dashboard but not in Codex's terminal indicator.

Async options (host-neutral; full protocol in `/home/hamza/repo/claive/docs/harness-integration.md`). When several workers run at once, or a worker would outlive the shell session, use `claive open --detach` (reusable, returns at once) or `claive start`, then `claive wait ID1 ID2 --any --timeout S` (first worker to settle; exit 124 means still running), with `S` below the tool's time limit. At the start of each turn, and after context compaction, run `claive inbox --consumer codex` to see finished turns since the last read; `ASK` lines carry a worker's question (answer with `claive answer ID --message ...`; exit 3 means the same). Parallel staged work: `claive batch start /abs/plan.json`, then `claive batch wait ID`. Multi-turn efforts: `claive mission new --title T --goal-file F`, `export CLAIVE_MISSION=<id>`, record decisions with `claive mission note`, and resume with `claive mission show ID` (its `Next:` line). Unattended server queue: `claive goal add` plus `claive serve`; answer parked goals with `claive goal answer ID --message ...`. Workers cannot launch workers.

For cancellation use `claive cancel WORKER_ID`, then collect the managed session's final output. Forcibly killing a terminal can bypass supervisor cleanup; if interrupted, inspect the registry and stop any surviving worker. Use:

```text
claive watch                 # live view; Ctrl-C closes only the view
claive list                 # active jobs and recent results
claive show ID --json       # status, progress, exit code, log paths
claive logs ID              # recent JSONL events
claive logs ID --stderr     # diagnostic output
claive wait ID              # wait for assigned turns; idle means ready, not closed
claive followup ID --prompt-file /absolute/next.md --reasoning-effort xhigh
claive effort ID --reasoning-effort max # future default; current call unchanged
claive usage ID             # retained session provider token/cache counters
claive close ID             # drain queued turns, close supervisor, retain history
claive cancel ID            # stop the worker process group
claive status-line          # one-line counts for a shell/status bar
claive-codex [codex options] # Codex + live pane + tmux status bar
```

Report the task, worker ID, and managed shell session ID at launch and report its outcome at completion. Codex's terminal view tracks the managed command and its output; it does not read the registry's detailed engine lifecycle. The optional custom monitor shows jobs launched through `claive`; raw Muse, agy, native agents, and OpenCode invocations are not automatically discovered. A completed CLI run does not establish that the assigned work passed verification. Task failures remain visible as warnings even when the run completes. Unexpected supervisor exits are shown as interrupted, not successful. Closing the monitor does not cancel workers; end jobs explicitly when their purpose ends.

The manager keeps Codex's existing internal status-line configuration intact. Its tmux view adds an outer status bar and live pane in a separate session; it does not change other tmux sessions. No always-running monitor daemon is installed.

### Underlying Muse flags

Always set all four options:

```text
--workspace <absolute-repo-path>
--trust-workspace
--disable-approval
--json
```

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

- Pin `--model muse-spark-1.3-contributor`. Use `xhigh` for normal tasks and `max` for very complicated work; `high`/`medium` only rarely for trivial chores. The orchestrator chooses per turn.
- Set `--max-model-steps` to fit the assignment; the default 100 is not a required ceiling.
- `--user-input-auto-resolve` cancels interactive questions automatically. A worker that needs an answer must report a blocker; cancellation is not authorization.
- For enforced read-only work add **both** `--disable-write --disable-shell`. The first disables only non-shell filesystem writes; leaving shell enabled permits writes through commands. Run any necessary verification commands in the parent or an isolated workspace.
- Add `--disable-web-tools` when web access is unnecessary.
- `--output-schema /absolute/schema.json` shapes the final answer for the meta provider; stdout remains a JSONL event stream.
- Preserve session logging for related follow-ups. `--no-session-log` disables durable history and local messaging, and is allowed only for deliberate single-turn runs.

## Reuse and prompt caching

Keep one worker and durable Muse session for a related task chain. Keep it idle when a specific related follow-up is expected in the active workflow. If no further use is planned, close it and collect the managed terminal output; retained history can be reopened later. Do not leave an idle worker running indefinitely for hypothetical future tasks. Read `show ID --json` for `session_id`; send a narrow `followup` instead of launching a new session. A per-turn effort override does not change the future default; `effort ID` does. Follow-ups can also override `--max-model-steps`. The worker appends JSONL/stderr logs and keeps each answer in `turn-000N.txt`. Queued follow-ups run sequentially. `close ID` finishes assigned turns before ending; `cancel ID` stops immediately. After closure, resuming with `--session-id UUID` restores retained history; preserve workspace and tool policy. Muse-created `--worktree` isolation is per-exec and is cleaned up on exit. If a lane must stay isolated across turns, use an externally retained worktree and pass it with `--worktree-existing`. Do not retire solely after two or four tasks. Start fresh for unrelated work or an actual context/quality limit.

Each underlying `muse exec` child exits after its turn; the supervisor stays alive and the same UUID reloads history next turn. This preserves conversation context across process exits. It is not a persistent model connection or proof of a cache hit.

Meta [prompt caching](https://dev.meta.ai/docs/prompt-caching) automatically reuses matching token prefixes on the server. Terminal exit does not itself clear that cache; eviction and inactivity can still cause misses. Keeping an idle process open cannot keep a server entry warm. Preserve stable leading instructions, tools, and history; put changing task details last, as in the [cookbook](https://dev.meta.ai/docs/cookbook/prompt-caching). Changing rules, model, reasoning, or compaction may change the rendered prefix; preserve the session when adjusting effort and measure the result rather than promising a hit.

Use `claive usage ID` (or `--json`) for provider-reported usage from Muse's retained session export. It counts each `model_completed` once, excluding repeated attribution records; no exposed usage means unknown, not zero. `cached_tokens > 0` proves reuse, and its ratio to reported input measures the observed hit rate. Totals include all recorded model calls in the retained session, including internal calls. These are accounting counters, not current context occupancy or an invoice; see [token usage](https://dev.meta.ai/docs/token-counting#usage). Do not add cached tokens again to input totals.

The API optionally exposes `prompt_cache_key` and Responses retention hints; this installed Muse exec CLI exposes neither. Do not invent CLI flags, set a new per-session cache key, change endpoints, or bypass Muse authentication to enable them. Automatic caching needs no extra flag. The current cache price and contributor quota effects are not established by a session UUID or these counters.

## Isolation and limits

- Concurrent Muse writers can each use `--worktree`; read-only workers may share a workspace. A writer's changed worktree is kept on branch `claive/<id>` after the worker ends. File ownership must remain disjoint even when worktrees are isolated.
- Worktree creation defaults to `HEAD`. Uncommitted parent edits will not automatically be present. Choose a suitable `--worktree-base` or explicitly transfer the required patch before dependent work. Use an externally retained worktree with `--worktree-existing` when isolation must survive across reusable turns.
- Give other shell workers their own isolated worktrees before concurrent writes; their relay scripts do not create them.
- Run heavy builds or test suites one at a time because this WSL environment has limited shared RAM. Independent light workers may run concurrently.
- The standard flags disable approval prompts but keep Muse's filesystem/network sandbox enabled; network defaults to `proxy-only`. Diagnose access failures from stderr and terminal events. Do not silently add `--yolo` or change sandbox permissions. Any exception must fit the task's authorization and host policy.
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
