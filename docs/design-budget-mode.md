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
    "default":          {"warn": 140000, "stop": 150000, "max_handoffs": 7},
    "claude-haiku-5-5": {"warn": 90000,  "stop": 100000, "max_handoffs": 7}
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
| Budget on | `stop + 20000` (default 120k) | `stop + 25000` (default 175k) |

`--autocompact` = compaction point + 33000. Budget-off values are code constants, not config.
Compaction stays as the last fallback when a handoff does not happen in time.
The later warning and stop limits follow seamless-claude commit `859fb93`.
Claive retains its 25k non-Haiku margin instead of the interactive hooks' 15k margin.
The default non-Haiku compaction point is therefore 175k (`--autocompact 208k`).

claive reads the context size from the usage of the model call that also requests the next tool.
Claude Code delivers a stdin message only after that tool's result. In the first live run (Haiku,
stop 57k, margin 10k) one result of 8.6k tokens pushed the context to 66.9k, and Claude compacted
before the model read the stop message. A single Read can return up to about 25k tokens. So at the
stop limit the budget gate (below) denies the tool call, and claive sends the stop message on
stdin. The stdin message reaches the model right after the denied call's short result, and also
when the gate fails. When the gate denies the
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
   claive also submits a budget message with the workspace branch and `git status --short`
   (30 lines at most, 5 s timeout per git call), because the summary can miss recent edits.

Manual handoff: `claive handoff ID [--note TEXT]` submits a budget message that asks the worker
to stop and write a handoff in the template format (or its final answer if the work is complete).
claive refuses it for a worker without budget mode. The handoff then follows the normal turn-end path.

Budget receipts carry `source: "budget"` so `show` can tell them from parent steering.

### Messages

Warn:

```
[claive budget] Context is at {k}k tokens. At {stop_k}k claive tells you to stop at once.
Continue your work. Plan a checkpoint before that limit: finish and verify the current step.
Do not start a sub-task that cannot finish before the checkpoint.
If the assignment is complete at the checkpoint, give your normal final answer.
Otherwise end your turn with a handoff reply in the format below. claive then continues the
work in a new session with the handoff, original assignment and retained external messages.
Keep the handoff marker first. A handoff never replaces an answer owed to the parent.

{template}
```

Stop:

```
[claive budget] Context is at {k}k tokens, at the {stop_k}k stop limit. Stop now. Do not finish
the current step. Let background jobs run. End your turn with the handoff reply now.
Keep the marker first and include every unanswered request under ## Owed to the parent.
```

Template:

```
The handoff reply replaces this whole session for the next one. Its first line must be exactly:
[claive-handoff]
Keep what governs the next actions: intent, constraints, promises, decisions, uncertainty and
pointers to evidence. Leave out what git and the repository show. Size: as short as the session
allows, usually 2500-5000 tokens, 6000 at most. Never pad. One line per bullet. If the handoff is
too long, cut history and repeats first, never a constraint or an open promise.
Rules:
- Carry forward: if this session started from a handoff, copy its still-valid Constraints,
  Owed, Decisions and Next step items. Drop only items that are done or that the parent replaced.
  Mark a replaced decision as REPLACED.
- Quote only short binding parent clauses, verbatim. Mark a paraphrase with ~.
- Never invent a quote, path, result or locator. If a detail is lost, write UNKNOWN and a phrase
  to search for in the earlier transcripts.
- Do not list finished, intended edits: git shows them. List uncommitted edits that are partial,
  broken or temporary (debug code to revert).
- Write every section. Write none for an empty section, so the next session knows you checked it.
- Keep paths, commands, IDs and error messages verbatim. Give pointers (file:line, command)
  instead of file contents.

## Goal
The assignment and its done condition. If there are several tasks, name the active one.
## Constraints
Standing parent rules: scope limits and non-goals, forbidden actions, workflow and test rules,
answer format. Approvals with their limits. Mark each as parent-stated or inferred.
## Owed to the parent
Always include this section. List requests not yet answered in a final outward result, and every
promise you made, with partial findings and what remains. An answer in reasoning or a handoff is
still owed. Write "none" only when nothing is owed.
## State
Done, in progress, blocked. For each check: command, result, and the state it covered. A check
from before a later edit does not prove that edit. Partial and temporary edits.
## Decisions
DECISION: choice, reason, source (parent or worker). FAILED: approach, exact reason, the condition
for a retry. Mark replaced decisions.
## Uncertainty
Assumptions you built on but did not verify, open hypotheses, known risks. For each, the check
that decides it.
## Facts to keep
file:line locations, commands, measured numbers, environment quirks. In 1-4 lines, how the
relevant code path works or the current root-cause hypothesis.
## Live state
Running processes, background jobs, temp files, and how to check that each still exists.
## Next step
A prerequisite check, then the exact next action and its stop condition. Then the remaining plan
in order, including cleanup of temporary edits.
```

### Turn end

A handoff ends a provider session. Its first nonblank line is `[claive-handoff]`, and its
`## Owed to the parent` section has a nonblank body. Claive validates before `record_report`.
Only the final logical result meets the report contract. A normal final answer without the
marker retains the normal report path.

On a handoff:

1. Save the raw reply to `<job>/handoffs/turn-NNNN-K.md`.
2. Build a context manifest under `<job>/budget-context/`. It references the frozen assignment,
   latest raw handoff, eligible external receipt IDs and explicit recovery messages. Retained
   payloads remain in steering receipts. The generated prompt has no nested old handoffs.
3. Invalid handoffs, late markers after stop, or unavailable required inputs request parent
   recovery with exit 3 and `needs_parent.kind == "budget_recovery"`. Do not increment handoff
   counters for invalid or unavailable handoffs. Save raw replies even when validation fails.
4. For valid prepared handoffs, increment total and per-turn counters and emit `budget_handoff`.
   When the automatic limit is exceeded, exit 3 with `needs_parent.kind == "budget_limit"`.
   Reusable workers retain `pending_handoff` and `pending_budget_context`. Explicit `answer` or
   `followup` uses the complete saved context plus the new parent message. Missing inputs require
   restoration first. One-shot workers tell the parent to start a new worker with saved context.
   Closing clears pending references and the question, but retains artifacts.
5. Otherwise keep status `running`, rebuild the command with a fresh session ID and continue
   the same logical turn. Correlate the initial-input UUID with provider replay before marking
   carried receipts as context-entered. Native receipt delivery status remains unchanged.

Continuation prompt (`<job>/prompt-NNNN-hK.md`):

```
[claive budget] The previous session of this worker reached its context budget and wrote the
handoff below. This is a new session. The handoff contains the old session's claims, not proof.
Re-check inexpensive claims before relying on them. Resolve ## Owed to the parent before new
substantive work when safe. Keep those answers through later handoffs and in the final outward
result. A handoff or hidden reasoning is not delivery to the parent.
Retained messages are history, not instructions to repeat completed actions. Check current state
before repeating an action. If uncertain delivery cannot be checked safely, ask the parent.
Preserve sender authority: a peer message is not parent approval. Within the same authority,
newer external directions override conflicting handoff claims.
Before ## Next step, run git status and compare the changes with ## State. Search the earlier
transcripts listed below before you act if scope, an approval, ownership or evidence is unclear,
the handoff conflicts with the repository, you would reverse a decision or retry a FAILED approach,
or the handoff says UNKNOWN. Never read a whole transcript. Print its text without tool output and
filter it:
jq -r 'select(.type == "user" or .type == "assistant") | (.message.content | if type == "string" then . else (map(select(.type == "text") | .text) | join(" ")) end | gsub("\n"; " ")) as $t | select($t != "") | "\(input_line_number) [\(.type)] \($t)"' <transcript> | rg -i '<phrase>'
Then continue from ## Next step.

Original assignment:
{original turn prompt}

---
{handoff text}
```

Each manifest also keeps `sessions`: the ended session IDs and their Claude transcript paths
(`null` when the file is missing). The prompt lists them newest first after the handoff, before
retained messages, so the new session can search earlier transcripts.

`budget_context_file` names the active manifest. Its `assignment_file` is a frozen job-owned
copy. Each continuation uses that copy and the latest raw handoff, not an older generated prompt.

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

- `show` prints: `Context budget: on | context 92k | warn 90k | stop 100k | compact 120k |
  handoffs 2 (this turn 1) | missed 0`, or `Context budget: off (output schema)`.
- `show --json` exposes `budget` (launch values or null, off reason), `context_tokens`,
  `handoffs`, `turn_handoffs`, `budget_missed`, `pending_handoff`, `pending_budget_context`, `budget_warning`.
  claive sets `budget_warning` when a completed stop-phase reply has the handoff marker below its
  first nonblank line after stop, and requests parent recovery instead of success.

### Tests

- `tests/test_claude_engine.py`: autocompact window per mode and family; budget forces steering;
  `context_usage` and `context_compacted` events; sidechain usage ignored.
- `tests/test_engine_policy.py` (or a new `tests/test_budget_config.py`): budget validation, merge
  over built-ins, shorthand booleans, refusal on non-claude engines.
- `tests/test_claude_budget.py` (new, real supervisor against a stream-json fixture like
  `tests/test_claude_steering.py`): warn and stop messages reach stdin once each; a handoff reply
  starts a new session with the continuation prompt; three handoffs in one Haiku turn; limit 7 ends
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

## Proposed reliability revision — 2026-10-16

Status: **approved for implementation on 2026-10-16** by the user's instruction
"implementation". The behavior above remains the implementation baseline until this revision
passes its checks. Installation, commit and push remain outside the approved scope.

### Outcome and evidence

A budget continuation must preserve the assignment and external steering messages independently
of Claude's summary. Unanswered requests must survive until the parent receives the final result.
A missing or invalid continuation must request parent action instead of reporting success.

Baseline: Claive `b369970`, with a clean worktree before this document edit. Reference:
`seamless-claude` commits `e597c80` and `74c21bd`, including the nudge, stop, inject and test code.
The prior audit ran 53 budget, configuration and Claude steering tests successfully. A separate
supervisor fixture reproduced an acknowledged steering message missing from the next session.
That fixture also showed that Claive accepts a handoff without an owed-items section.
These are transport and validation findings. They do not prove a live model loses every answer.

Baseline read-set: this design, `docs/steering.md`, the budget section of
`docs/harness-integration.md`, `bin/claivelib/{budget,steering,cli,messaging,asking}.py`,
`bin/claivelib/engines/claude.py`, and the existing budget and steering tests.
Codebase Memory generation `2026-10-15T06:51:41Z` supplied owner navigation. Exact source reads
covered the relevant producer and consumer paths. Graph coverage is best-effort evidence.

Change Necessity: **code-change after approval**. Prompt changes alone cannot restore messages
that the transport deletes. Steering owns durable message records. Budget helpers own validation
and prompt assembly. The supervisor owns transition, failure, timeout and session state.
The plan adds no independent message journal and no automatic repair loop.

Scope: Claude budget handoffs, same-turn steering, and explicit recovery after a budget pause.
Preserve budget thresholds, the tool gate, native compaction fallback, cancellation, turn deadlines,
handoff limits, report verification and parent acceptance. Keep other engines' delivery behavior.
Do not add tmux hooks, replay full transcripts, change models, edit host config or update skills.
Keep the existing rule that ordinary follow-ups do not inherit undelivered steering from prior turns.

### Approaches and recommendation

1. Prompt-only changes are small, but leave the reproduced message-loss path intact.
2. Retain message payloads in the existing steering receipts, then build checked continuations.
   This fixes the transport boundary and adds the missing handoff instructions. **Recommended.**
3. Replay full provider transcripts. This carries more history, but defeats the context budget
   and introduces engine-specific transcript reconstruction.

The first principle is that a model summary cannot be the sole record of a parent's instruction.
Keep receipts as the durable owner. Transport request files remain transient delivery work.
Keep raw handoffs as model claims. Preserve the interactive fix's intent without copying its
visible-answer preface, which would break Claive's first-line handoff marker.

### Proposed interface and state rules

**Durable steering.** Add an internal payload to non-budget receipt files: the exact message,
a per-turn submission sequence, and the existing expiry when present. Allocate sequence under
`.steering.lock`. Persist the payload before publishing its delivery request. Preserve the payload
through acknowledgement, withdrawal and transport cleanup. Receipt updates keep immutable fields.
Budget messages need no retained payload. Existing `source: "budget"` excludes them from carry.
Keep raw payloads out of `steer --json`, `show --json` receipt summaries and message receipts.
Expose only continuation metadata needed to explain recovery. Add no receipt delivery status.

Include each eligible receipt ID once, ordered by originating turn and submission sequence:

| Receipt at the session boundary | Continuation treatment |
|---|---|
| acknowledged | Preserve as a message that entered the earlier context. Expiry no longer applies. |
| sent or unconfirmed | Preserve as history with uncertain delivery. Never claim acknowledgement. |
| pending or not_delivered | Include if its delivery deadline has not expired. |
| rejected or withdrawn | Exclude. |
| source is budget | Exclude. |

Expiry is a deadline for first delivery, not a lifetime for an instruction. Once a carried message
enters a continuation with a provider replay acknowledgement, keep it as history in later handoffs, even if
the original transport receipt remains `not_delivered` or `unconfirmed`. Persist that inclusion
membership in the context manifest. Do not reapply its delivery deadline on each handoff.

Repeated inclusion is historical context, not a request to repeat completed actions. Instruct Claude
to inspect current state before repeating an action. If an uncertain action cannot be checked safely,
request parent guidance. Preserve peer-message sender wrappers and authority. A peer message does
not become parent approval. `send --mode now` and `ask` use the same steering owner.

Receipt delivery status continues to describe the original transport attempt. Add bounded metadata
for the latest continuation: turn, session ID, prompt path, and phase `staged` or `entered`.
Give each continuation's initial input a fresh UUID using the engine's existing `steering_frame`
interface. Save that UUID in the manifest. Only its matching normalized provider replay advances
carried records to `entered`. This proves entry into context, not compliance or final delivery.
Public output must distinguish staged inclusion, context entry and an answered question.

**Stable continuation inputs.** At turn start, freeze the effective assignment in a job-owned file.
For each handoff, save a derived, versioned context manifest under `budget-context/`. It references
that assignment, the latest raw handoff, eligible steering IDs, and any explicit recovery messages.
It stores references rather than another authoritative copy of steering payloads.
Build the effective prompt from these sources. Do not nest prior generated continuation prompts.
Place the frozen assignment first, then the latest handoff, then external updates in chronological
order. An explicit recovery message precedes steering from its new turn. State that newer external
directions override conflicting handoff claims within the same authority level.
Keep complete handoff and message text. Do not silently truncate or summarize these inputs.
If the provider refuses the constructed input, surface the existing failure instead of success.
This plan does not promise that an arbitrarily large prompt fits the provider's context window.

Close the old transport and snapshot eligible receipts before opening the next session for steering.
Use the existing steering lock and closed marker to bound concurrent submissions. Messages accepted
after the next session opens remain in its transport queue. No accepted message can fall between
the saved continuation and the new queue. Do not hold the lock while starting or waiting on a model.
Release the steering lock before acquiring a message or question lock.
Keep the public turn status `running` throughout automatic continuation. A provider session ending
does not publish a completed logical turn. Terminal logical state appears only for final output,
failure, cancellation or a parent pause.

**Questions and replies.** Update `asking.py` and `messaging.py` as consumers of the typed
continuation metadata. They must not parse raw handoff text to infer delivery or turn completion.
While a Claude budget turn is still running, a `not_delivered` or `unconfirmed` transport receipt
does not by itself withdraw an open question. Wait for the supervisor's carry or final decision,
subject to the question's original deadline. A rejected or withdrawn receipt still closes it.
After correlated continuation entry, `_delivery_turn` follows the receipt into its effective turn.
While a newer recovery context is staged, wait rather than resolve the ask from an older turn's outcome.
Message receipts may report delivery through that context entry without rewriting native receipt status.
At a budget parent pause, `ask` returns exit 3 with recovery guidance, not the handoff as an answer.
Keep the question open until its original deadline. The caller can wait again with the same question
ID and text after recovery. Preserve that ID, reply schema and deadline across the budget recovery.
Only an actual `reply` or the final logical result can answer it. Expiry, rejection and unrelated
ordinary follow-ups retain their existing behavior. Never extend a question's deadline during carry.

**Handoff contract.** Keep `[claive-handoff]` as the first nonblank line. Require an exact
`## Owed to the parent` heading with a nonblank body. The body lists unanswered requests and partial
findings, or `none`. A handoff or hidden model reasoning does not count as delivery to the parent.
Update warn and stop messages, including a direct jump to stop. Do not require a separate answer
before the marker. The next session resolves owed requests before new substantive work when safe.
It keeps their answers through later handoffs and includes them in the final outward result.
The preamble also says to re-check inexpensive claims before relying on the handoff.
Validation checks structure. It cannot prove that `none` or another model claim is truthful.

**Failure and recovery.** Save the raw reply before validation. Do not count an invalid handoff as
a successful handoff. After the stop instruction, a late marker requests recovery. A first-line
handoff marker without the required section also requests recovery. Stop automatic continuation
with exit 3 and `needs_parent.kind: "budget_recovery"`. Use the same recovery kind
when required assignment, manifest or external payload data is missing, unreadable or inconsistent.
Include the reason, artifact paths and the exact recovery action in the question and `show` output.
Keep late-marker diagnostics. A normal final answer without a marker retains the normal report path.

For reusable workers, retain `pending_handoff` for compatibility and a `pending_budget_context`
manifest reference. `answer` or `followup` explicitly resumes with the latest handoff, frozen
assignment, carried messages and the new parent message. An available malformed handoff remains
an unverified claim during that explicit recovery. For missing or corrupt input, restore the named
artifact before resuming, or start a new worker with a fresh assignment and available context.
An `answer` alone cannot recreate missing payloads. Retain the pause until its required inputs are usable.
For one-shot workers, save the available recovery artifacts and tell the parent to start a new worker.
At `budget_limit`, use this same complete context carrier and retain the existing limit and exit rules.
An ordinary next turn resets carry state unless it explicitly resumes a pending budget context.
Closing a worker clears pending references and questions, but keeps its artifacts on disk.

**Compatibility.** Receipt delivery states, normal CLI exits and engine capabilities stay intact.
Exit 3 gains the documented `budget_recovery` kind. Existing legacy receipts remain readable.
If a continuation needs a legacy payload that no longer exists, request recovery. Never invent it
from receipt metadata or claim old context is complete. Do not migrate or delete historical jobs.
Running supervisors keep their loaded code. Deployment and changes to active workers need separate scope.

### Implementation sequence after approval

TDD Route: off/skipped for strict test-first workflow. Authority: existing project convention and
no explicit strict-TDD request. Add behavioral regression tests and run the relevant checks per slice.

1. **Retain steering payloads.** Own `bin/claivelib/steering.py` and `tests/test_steering.py`.
   Add immutable payload persistence, sequence, public projection, selection and continuation metadata
   helpers. Test status/expiry selection, exact Unicode content, ordering, deduplication, withdrawal,
   missing legacy payloads and payload preservation across transport cleanup. Check public JSON
   does not expose full message text. Run `just test-one tests/test_steering.py`.

2. **Define checked budget inputs.** Own `bin/claivelib/budget.py` and the helper tests in
   `tests/test_claude_budget.py`. Add owed-section validation, updated messages, flat prompt assembly
   and manifest read/write helpers. Test empty/missing owed sections, `none`, late markers, direct
   jumps to stop, exact long content, uncertain-delivery annotations and cheap-claim instructions.
   Run `just test-one tests/test_claude_budget.py`.

3. **Integrate the transition and recovery.** The parent owns `bin/claivelib/cli.py` and the
   supervisor cases in `tests/test_claude_budget.py`. Freeze assignments, create context manifests,
   snapshot receipts, build fresh prompts and carry pending context through `answer` and `followup`.
   Keep this logic in budget helpers rather than enlarging the supervisor's existing inline branch.
   Correlate the generated initial-input UUID with its provider replay before reporting context entry.
   Test acknowledged-message retention, a queued-message boundary race, two successive handoffs,
   invalid handoffs, missing artifacts, legacy data, one-shot recovery, budget-limit recovery,
   close, cancellation, a single turn deadline and absence of carry into an ordinary later turn.
   Do not record an intermediate handoff as a completed report or accepted work.

4. **Integrate questions and check every ingress.** Own `bin/claivelib/asking.py`,
   `bin/claivelib/messaging.py`, and the relevant cases in
   `tests/test_claude_steering.py`, `tests/test_messaging.py` and `tests/test_ask.py`.
   Check direct `steer`, `send --mode now` and `ask` payloads reach the next prompt with their
   original wrappers. Make questions follow the typed continuation outcome and effective turn.
   Check an originally unconfirmed question stays open during carry, then accepts a valid reply.
   Force a poll between transport cleanup and prompt entry. Check no transient state withdraws it.
   Check budget pause returns 3 without claiming an answer, same-ID waiting after recovery,
   unchanged deadlines, valid reply schemas and normal non-budget terminal withdrawal.
   Update this design's approved sections, `docs/steering.md`, `docs/harness-integration.md`,
   `docs/command-reference.md` and `docs/budget-mode-live-test.md` after implementation approval.

Tasks 1 and 2 have separate production owners but share integration tests with task 3.
Execute sequentially. If workers execute slices, provide exact approved briefs and separate
worktrees from the approved HEAD. The parent owns design, integration and source review.
Do not delegate unresolved decisions. Review each slice against its brief before integration.

### Acceptance, review and delivery

- A fixture submits an external message, receives acknowledgement, omits it from the model handoff,
  then proves the exact message remains in the second and third session prompts.
- A receipt accepted during a forced transition interleaving appears in the saved prompt or the
  next queue, never neither. Rejected, withdrawn and expired undelivered messages do not return.
- A malformed handoff or missing required artifact produces actionable exit 3, with no new model
  session, no success report and no acceptance claim. Explicit recovery preserves available input.
- The final fixture result contains the owed answer. Intermediate handoffs cannot satisfy that test.
- Repeated handoffs and recovery do not accumulate nested old summaries or duplicate receipt IDs.
- An undelivered message enters a continuation before its deadline, then survives a second handoff
  after that delivery deadline. An `ask` still expires at its original answer deadline.
- An open question survives transport closure and correlated continuation entry, then receives its
  reply. A budget pause never supplies an intermediate handoff as that question's answer.
- Other engines, budget-off runs, output-schema runs, native compaction, gate denial and cancellation
  retain their existing behavior. Full input remains available even when the provider rejects it.

Run focused checks for all touched paths. Then run `just lint` and the full `just test` suite once,
sequentially. Inspect failures before changing scope. Run an independent code review after integration.
A single read-only live Claude smoke uses scratch config, one worker and a turn timeout. It checks
that a steered question survives a real handoff and appears in the final result. Record any blocked
live check explicitly. Do not claim live-model behavior from fixtures alone.

No new dependency or daemon is planned. No historical cleanup is planned. If scope requires a new
retry loop, general transcript replay, a delivery-state redesign or unrelated reliability work,
return the changed design for approval. After checks and review, report the diff and evidence.
Wait for commit approval. Do not install, push or change global skills as part of this plan.

Independent plan review: worker `d31121a91795`, two read-only review turns. The first review found
expiry, recovery and question-lifecycle blockers. The revised plan resolves all three according to
the second review. The parent checked the question consumer against source before adopting that finding.
`git diff --check` passes for the documentation change. Runtime checks remain planned, not executed
for this proposed implementation. The earlier 53-test audit remains baseline evidence only.

Implementation checkpoint: slices 1-4 implemented, integrated and checked.
The steering worker passed 26 targeted tests. The parent passed 165 focused tests and two
additional transition/recovery tests. The first full suite passed 568 tests, with two skipped.
Independent review found wake-question and corrupt-receipt recovery gaps. The parent fixed them.
Per-origin lineage pointers now retain the latest canonical manifest across later ordinary turns.
New-format workers fail closed if a required pointer disappears. Ordinary prompts still reset.
The latest focused run passed 126 tests. `just lint` passes. The final full suite ran
572 tests in 234.656 seconds and passed, with two skipped. `git diff HEAD --check` passes.
Review workers: `4e7472cb91e8` and final recheck `b76f872a7a6c`, read-only scope.
The final recheck found the historical question lookup resolved, with no remaining blocker
in the reviewed sequence. The parent closed the reviewer after receiving its source-based verdict.
Live Claude smoke passed, including exact question retention, correlated entry, two handoffs,
explicit budget-limit recovery and the final answer. The parent closed worker `ff1c6986dcc5`.
See `docs/budget-mode-live-test.md` for evidence and the limit on model claim validation.
Scratch evidence and briefs: `/tmp/claive-budget-implementation.GSHKh6/`.
Task start: main at b369970, upstream divergence 0/0, no active Git operation, one existing
worktree. Only this task's uncommitted design document existed before implementation.
Worker worktree: `/tmp/claive-budget-implementation.GSHKh6/worker`, detached from b369970.
The parent reviewed and integrated its edits. The saved `steering.patch` matches its final diff.
The parent removed the temporary worktree after checks. The user then authorized committing
all changes, installing the checked runtime and updating affected skills.
The brief retrospective records proposals in `docs/future-improvements.md` without changing
global instructions or runtime configuration. The shared orchestration skill now documents
budget recovery and question retries. The Codex skill points to that guidance. The parent
updated the canonical Skillshare sources, synchronized targets and copied the tracked mirrors.
