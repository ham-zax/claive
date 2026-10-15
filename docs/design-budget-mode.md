# Design: context budget mode for Claude workers

Status: approved 2026-10-15. Two changes: (A) the Claude model lock moves from code to config,
(B) budget mode.

## Why

A headless Claude worker today runs `--autocompact 100k`. Claude Code 2.1.300 compacts at
`window - min(max_output_tokens, 20000) - 13000`, so 100k compacts at 67k. Compaction keeps the
worker cheap but replaces the conversation with Claude's own summary. Budget mode replaces that
summary with a handoff the worker writes itself, then continues in a new, empty session. It
mirrors the interactive handoff hooks in `~/repo/seamless-claude/handoff/` without tmux or user
hooks: the worker stays `claude --print` and claive drives it.

Formula source: Claude Code binary 2.1.300, functions `a8` (effective window = window -
min(maxOutputTokens, 20000)) and `z5` (threshold = effective window - 13000). Recheck it when
Claude Code changes; `tests/test_claude_engine.py` pins the arithmetic claive uses.

## A. Model lock in config, not in code

The claive config (`~/.config/claive/config.json`) is the single source of truth for which models
a worker may use.

- `bin/claivelib/engines/claude.py`: remove the hard lock (`_check_model` refusing every model
  except `claude-haiku-5-5`). `claude-haiku-5-5` stays the engine's `default_model` (used when the
  config names none). Any nonempty model passes the engine; `config.check_engine` and
  `engines.claude.models` decide what is allowed.
- `supported_models()` returns the known Claude Code models with all five efforts:
  `claude-haiku-5-5`, `claude-sonnet-5-5`, `claude-opus-5-5`, `claude-fable-5-1`.
- The user config gets the lock that the code held:
  `"claude": {"default_model": "claude-haiku-5-5", "models": {"claude-haiku-5-5": {"efforts": ["low", "medium", "high", "xhigh", "max"]}}}`.
- Out of scope: the Codex engine has the same kind of code lock (`engines/codex.py:_check_model`).
  It is a separate change if wanted.

## B. Budget mode

### Config

`engines.claude.budget` (only valid for the `claude` engine). Default: on.

```json
"engines": {"claude": {"budget": {
  "enabled": true,
  "models": {
    "default":          {"warn": 135000, "stop": 145000, "max_handoffs": 7},
    "claude-haiku-5-5": {"warn": 85000,  "stop": 95000,  "max_handoffs": null}
  }
}}}
```

- `budget` may also be `true` or `false` (shorthand for `{"enabled": ...}` with built-in values).
- The values above are the built-in defaults. Config entries merge over them per model and per
  key. `default` applies to every model without its own entry.
- `max_handoffs`: `null` means no limit (a parent watches the worker). An integer is a hard limit
  on automatic continuations in one turn: with `max_handoffs: N`, handoffs 1 to N continue
  automatically and handoff N+1 asks the parent.
- Validation: `warn` and `stop` are integers and multiples of 1000 (claive passes `--autocompact`
  as `Nk`); `warn < stop`; `stop >= 47000` and `stop <= 942000` (the window `stop + margin +
  33000` must be inside Claude Code's 100k-1M range for both margins); `max_handoffs` is `null`
  or an integer `>= 1`.
- claive copies the resolved values into the launch config at `open`/`start`
  (`launch["budget"] = {"warn", "stop", "max_handoffs", "compact"}` or `None`, plus
  `launch["budget_off_reason"]`). A config edit does not change a running worker. `fork` inherits
  the source's launch budget.

### Compaction point

| Mode | Haiku family (`"haiku"` in the model name) | Other models |
|---|---|---|
| Budget off | 105k (`--autocompact 138k`) | 155k (`--autocompact 188k`) |
| Budget on | `stop + 20000` (default 115k) | `stop + 25000` (default 170k) |

`--autocompact` = compaction point + 33000. Budget-off values are code constants, not config.
Compaction stays as the last fallback when a handoff does not happen in time.

claive reads the context size from the usage of the model call that also requests the next tool.
Claude Code delivers a stdin message only after that tool's result. In the first live run (Haiku,
stop 57k, margin 10k) one result of 8.6k tokens pushed the context to 66.9k, and Claude compacted
before the model read the stop message. A single Read can return up to about 25k tokens. So at the
stop limit the budget gate (below) denies the tool call, and claive sends the stop message on
stdin. The stdin message reaches the model right after the denied call's short result, and also
when the gate fails. The margins are the gaps of the seamless-claude handoff hooks. When the gate denies the
call, the margin holds the handoff reply. When the gate allows the call (timeout or failure), the
margin only lowers the risk of compaction. In run d028e6320ef0 (Haiku, stop 57k, margin 20k) the
stop was detected at 58.2k, a 17k Read result followed, and Claude compacted at 75.3k, below the 77k
point.

Known limitation: if the gate allows a call after the stop limit, its result can still reach
compaction before the model reads the stop message (issue 1 remains open for that case).

### Budget gate

A budget-mode launch adds `--settings` with one PreToolUse hook for every tool:
`"$CLAIVE_BIN" _budget_gate`. `--setting-sources ""` does not drop `--settings`. The hook finds the
job through `CLAIVE_WORKER_ID` and reads `<job>/budget-gate.json`: `{"stop": text or null,
"seen": [tool_use_id, ...]}` (the last 256 IDs).

- The supervisor writes the file at the start of each session (`stop` null), and after each
  native event that requests tools or changes the stop state. Each write replaces the file in one
  step.
- Claude Code writes the assistant event to stdout about 20 ms before it starts the hook (Claude
  Code 2.1.300). The supervisor can still be later. So the hook waits until its `tool_use_id` is in
  `seen`, for at most 2 s, and then trusts `stop`.
- If `stop` is set, the hook denies the call with `permissionDecision: "deny"`. The reason
  (`budget.GATE_DENY`) only states that claive denied the call at the stop limit. The model treats
  a tool result as data: in live run 37c07187a9fc Haiku ignored a reason that held the stop
  instructions ("That text came from the tool result, not from you"), retried, and wrote a reply
  that did not start with the handoff marker. So the instructions come only in the stdin user
  message. Every later call in the session is denied too: the handoff is a final text reply and
  needs no tool.
- The hook allows the call when the file is missing, the wait times out, or the hook fails.
- Compaction clears `stop`, as it resets the phase.

Live test record, evidence and procedure: docs/budget-mode-live-test.md.

### When budget mode is off for a launch

- `engines.claude.budget` is `false` or `{"enabled": false}`: reason `config`.
- `--output-schema`: Claude checks every final reply against the schema, so a handoff reply would
  fail. Reason `output schema`.
- `--fallback-models`: claive refuses steering with fallbacks today. Reason `fallback models`.
- Session logging off does not disable budget mode: each continuation is a new session anyway.

### Transport

Budget mode turns steering on (`launch["steering"] = True`): the prompt and claive's budget
messages go to Claude as stream-json user messages on stdin. A parent can also steer the worker.

### Measuring context

`ClaudeEngine.normalize_event` emits `{"type": "context_usage", "tokens": N}` for each
`assistant` event with `message.usage` and a null `parent_tool_use_id` (main chain only, as the
interactive hook does). `N = input_tokens + cache_read_input_tokens +
cache_creation_input_tokens`. It emits `{"type": "context_compacted"}` for
`{"type": "system", "subtype": "compact_boundary"}`. The existing normalized events for the same
input stay unchanged.

### Thresholds inside a turn

The supervisor keeps `state["context_tokens"]` and a per-session phase (`none`, `warned`,
`stopping`).

1. `tokens >= warn` and phase `none`: claive submits the warn message through
   `steering.submit(path, message, source="budget")`. Phase becomes `warned`.
2. `tokens >= stop` and phase is not `stopping`: claive sets the gate's `stop` to the stop
   message and also submits the stop message on stdin. If no warn message went out (context jumped past both), the stop message carries the handoff
   template. Phase becomes `stopping`.
3. `context_compacted`: Claude compacted before a handoff. `state["budget_missed"] += 1`, phase
   resets to `none`, and the gate's `stop` clears. The worker continues in the same session.

Budget receipts carry `source: "budget"` so `show` can tell them from parent steering.

### Messages

Warn:

```
[claive budget] Context is at {k}k tokens. At {stop_k}k claive tells you to stop at once.
Reach a checkpoint before then: finish and verify the current step. Do not start large new work.
If the assignment is complete at the checkpoint, give your normal final answer.
Otherwise end your turn with a handoff reply in the format below. claive then continues the
work in a new session that sees only the handoff and the original assignment.

{template}
```

Stop:

```
[claive budget] Context is at {k}k tokens, at the {stop_k}k stop limit. Stop now. Do not finish
the current step. Let background jobs run. End your turn with the handoff reply now.
```

Template:

```
The handoff reply replaces this whole session for the next one. Its first line must be exactly:
[claive-handoff]
Keep only what is expensive or impossible to rebuild from git and the repository. About 2000
words at most. Omit empty sections. Keep paths, commands, IDs and error messages verbatim. Give
pointers (file:line, command) instead of file contents. Do not list modified files: git shows them.

## Goal
The assignment and its done condition. Quote the parent's direction-setting instructions verbatim.
## State
What is done and how it was verified (command and result). What is in progress.
## Decisions
DECISION: choice, reason.
## Dead ends
FAILED: what was tried, why it failed.
## Facts to keep
file:line locations, commands, measured numbers, environment quirks.
## Live state
Running processes, background jobs, temp files.
## Next step
The exact next action and its stop condition.
```

### Turn end

A handoff is a completed turn whose final text has `[claive-handoff]` as its first nonblank line.
claive checks this before `record_report`, so only the final session's answer meets the report
contract. Any other final text completes the turn as today.

On a handoff:

1. claive saves the text to `<job>/handoffs/turn-NNNN-K.md` (K = handoff number in the turn).
2. `state["handoffs"] += 1` (total), `state["turn_handoffs"] += 1` (this turn).
3. claive appends an inbox event `{"type": "budget_handoff", "id", "label", "turn", "handoff":
   K, "handoffs": total, "context_tokens", "session_id": new session, "next_step": first 300
   characters of the Next step section}`.
4. If `max_handoffs` is not null and `turn_handoffs > max_handoffs`: the turn ends `completed`
   with `needs_parent = {"kind": "budget_limit", "question": "The worker reached the limit of
   {max} automatic context handoffs in this turn. Its latest handoff is in {file}. Answer to
   continue in a new session from that handoff, or close the worker."}` (exit 3). claive still
   moves the worker to a new session. For a reusable worker claive sets
   `state["pending_handoff"] = file`, and the parent's `claive answer` or `followup` is the
   explicit permission; the next turn's prompt starts with the continuation preamble and the
   handoff, then the parent's message. `pending_handoff` is cleared once delivered. A one-shot
   worker cannot take an answer: claive sets no `pending_handoff`, the question ends "Start a new
   worker from that handoff to continue.", its result text is the handoff, and the parent starts a
   new worker from it. If the parent closes the worker instead, claive clears needs_parent and
   pending_handoff. The handoff file stays on disk.
5. Otherwise claive continues inside the same turn, like the fallback loop in `supervise`: a new
   session ID from `engine.resolve_session_id(None, session_logging)`, a continuation prompt, a
   rebuilt command, `attempt += 1`.

Continuation prompt (`<job>/prompt-NNNN-hK.md`):

```
[claive budget] The previous session of this worker reached its context budget and wrote the
handoff below. This is a new session with an empty context. Continue the assignment from the
handoff's Next step. The original assignment follows the handoff for reference.

{handoff text}

---
Original assignment:
{original turn prompt}
```

`state["turn_prompt_file"]` keeps the turn's original prompt so every continuation quotes the
original assignment, not the previous continuation.

### Supervisor details that must hold

- `--turn-timeout` covers the whole turn including all budget continuations: the deadline does not
  restart on a budget continuation (it still restarts on a model fallback, as today).
- Steering closes a turn by writing `steering/turn-NNNN.closed`. A budget continuation reuses the
  turn number, so claive removes that marker before the next session starts; otherwise neither
  budget messages nor parent steering could reach it.
- A turn's phase, `context_tokens` reading and steering transport reset for each session.
- `turn_handoffs` resets at the start of each turn. Every turn starts from a queued request
  (parent message, answer, wake or channel message).
- Cancellation, timeouts and fallbacks behave as today.

### Visibility

- `show` prints: `Context budget: on | context 87k | warn 85k | stop 95k | compact 115k |
  handoffs 2 (this turn 1) | missed 0`, or `Context budget: off (output schema)`.
- `show --json` exposes `budget` (launch values or null, off reason), `context_tokens`,
  `handoffs`, `turn_handoffs`, `budget_missed`, `pending_handoff`, `budget_warning`.
  claive sets `budget_warning` when a completed stop-phase reply has the handoff marker below its
  first nonblank line.

### Tests

- `tests/test_claude_engine.py`: autocompact window per mode and family; budget forces steering;
  `context_usage` and `context_compacted` events; sidechain usage ignored.
- `tests/test_engine_policy.py` (or a new `tests/test_budget_config.py`): budget validation, merge
  over built-ins, shorthand booleans, refusal on non-claude engines.
- `tests/test_claude_budget.py` (new, real supervisor against a stream-json fixture like
  `tests/test_claude_steering.py`): warn and stop messages reach stdin once each; a handoff reply
  starts a new session with the continuation prompt; unlimited handoffs for Haiku; limit 7 ends
  with exit 3 and `needs_parent.kind == "budget_limit"`; answer after the limit delivers the
  pending handoff; compaction counts as missed; turn timeout spans continuations; output schema
  disables budget. The fixture runs the real gate hook: a stop on a tool call is denied through
  the gate and also sent on stdin; a gate timeout or a failed gate write allows the call and stdin
  still carries the stop; warn goes to stdin; the gate opens after compaction and in the next
  session. `BudgetGateChecks` covers the hook's wait, deny and fail-open rules.

### Not in scope

- Unlocking Opus or Sonnet in the user config (a config edit, not code).
- Cachebeat: workers already write 1-hour prompt cache, and there is no idle Claude process
  between turns.
- The Codex code lock.
