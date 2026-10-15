# Claive Claude budget mode: live test record and procedure

Spec: docs/design-budget-mode.md.
Budget mode is the claive feature that tells a Claude worker to write a `[claive-handoff]` reply
before its context fills. Claive then continues the work in a new session from that reply.
The feature copies the interactive handoff hooks in `~/repo/seamless-claude/handoff/`.

Recorded 2026-10-15. The fixes for issues 1-3 are in the commit that adds this file.

## How budget mode works (short)

1. The supervisor reads `usage` from each assistant event (input + cache tokens = context size).
2. At `warn` it sends a warn message, at `stop` a stop message, through live steering
   (stream-json user messages). Receipts: `<job>/steering/receipts/*.json`, `source: "budget"`.
3. The worker ends its session with a reply whose first line is `[claive-handoff]` and whose
   `## Owed to the parent` section has a nonblank body.
4. Claive saves it to `<job>/handoffs/turn-NNNN-K.md`, starts a new session with
   `<job>/prompt-NNNN-hK.md`, and appends an inbox event `budget_handoff`.
5. Claude's own compaction is the fallback. Claive passes `--autocompact <compaction point + 33k>`.
   A `compact_boundary` event means a missed handoff (`state.budget_missed += 1`).

The 2026-10-16 reliability revision also saves context manifests and retains exact external
messages independently of the model handoff. A matching provider replay confirms their entry
into a new session. Invalid or unavailable handoffs request recovery with exit 3.

For the reliability smoke, steer an additional question before the first handoff. Check its
receipt, exact text in the next prompt, correlated `continuation.phase == "entered"`, and its
answer in the final result. Fixture evidence alone does not establish live model compliance.

Code: `bin/claivelib/budget.py`, `cli.py` (`update_budget`, handoff branch near line 828,
`show` near line 1580, `answer` near line 2710), `config.py` (`claude_budget`,
`_validate_claude_budget`), `engines/claude.py` (`budget_margin`, `compaction_point`).

## Issues

### Issue 1: stop message arrives one tool result late (FIXED by the budget gate, verified live, one limitation)

Facts:
- Claive learns the context size from the model call that ALSO requests the next tool.
- Claude Code delivers the queued stop message only after that tool's result.
- So the context at the moment the model can read the stop message is
  `usage at detection + size of the next tool result`.
- If that sum reaches the compaction point first, Claude compacts and the handoff is missed.
- Claude Code compacted 0.1k to 1.7k BELOW the computed compaction point in the runs.

Evidence (Haiku, warn 50k, stop 57k):

| Run | Gap stop->compact | Context at detection | Next result | Compacted at | Result |
|---|---|---|---|---|---|
| 12cd85782ad3 (old code) | 10k (point 67k) | 58.3k | about 8.6k | 66.9k | missed |
| 431a1385ea33 (old code, 400-line reads) | 10k (point 67k) | 58.4k | about 7.4k | 65.8k | missed |
| d028e6320ef0 session 1 (new code) | 20k (point 77k) | 59.6k | about 12.8k | none (72.4k) | handoff OK |
| d028e6320ef0 session 2 (new code) | 20k (point 77k) | 58.2k | about 17k (1100 lines of orchestration.py) | 75.3k | missed |

Margin change: gap is now stop + 20k (Haiku) and stop + 25k (others), same as seamless-claude.
The margin only reduces the risk. Run d028e6320ef0 session 2 (17k result) already defeated it.

Note: a PostToolUse hook (the seamless-claude method) does NOT help. The hook reads the same
usage number and its message enters at the same point, after the tool result.

Fix built (commit 5613b74): the "budget gate". Claive passes a `PreToolUse` hook with `--settings`.
The hook runs the hidden `claive _budget_gate` command. The supervisor writes
`<job>/budget-gate.json` when the context reaches `stop`. The hook waits up to 2.0 s for that
file, then denies every tool call with a short factual reason. Claive still sends the stdin stop
message every time, so a gate timeout or a gate-file write failure does not lose the stop.
Experiments E1-E3 confirmed: the hook runs with `--setting-sources ""` and in plan mode. The
hook input has no usage. The stdout assistant event precedes the hook by 19-22 ms.

Live evidence (Haiku, warn 50k, stop 57k):
- 37c07187a9fc: gate denied at 59.1k, no compaction. The deny reason held the stop
  instructions. Haiku treated the tool-result text as untrusted, ignored it and wrote a
  preface before `[claive-handoff]`, so claive detected no handoff. Fix: the deny reason
  now states only the fact. The instructions arrive only through stdin.
- 07bfedabe07c: PASS. Session 1: 46.5k to 59.2k, deny + stdin stop, handoff 1, automatic
  continuation. Session 2: 42.6k to 66.7k (one 24k Read), deny + stdin stop, handoff 2,
  then `budget_limit` (max_handoffs 1). budget_missed 0. The parent answer started session 3
  from handoff 2.

Remaining limitation: if the gate allows a call (gate file not yet written, or the context is
below `stop`), and that call's result crosses the compaction point, Claude can still compact first.

### Issue 2: `max_handoffs: N` gave N-1 automatic continuations (FIXED, verified live)

Old: claive counted the handoff, then stopped when `turn_handoffs >= max_handoffs`.
`max_handoffs: 1` gave zero automatic continuations.
New: stop when `turn_handoffs > max_handoffs`. `max_handoffs: 1` gives one automatic
continuation, and the second handoff asks the parent (`needs_parent.kind == "budget_limit"`, exit 3).
Live check: worker d028e6320ef0 continued once, then stopped at handoff 2.

### Issue 3: one-shot workers were told to "Answer" (FIXED, unit-tested, not live-tested)

Old: a one-shot worker (`claive start`/`run`) at the limit had the question
"Answer to continue ...", `pending_handoff` was set and `show` said
"answer or followup continues from it". But `claive answer` refuses one-shot workers
("worker is not waiting for a parent decision").
New: for one-shot workers the question ends "Start a new worker from that handoff to continue.",
no `pending_handoff` is set, and `show` prints no "Pending handoff" line.
Reusable workers (`claive open`) keep the old text and `pending_handoff`. Verified live for the
reusable path only:
`claive answer` started turn 2 from the handoff, `prompt-0002.md` = preamble + handoff +
original assignment + parent message, `pending_handoff` cleared.

### Issue 4: Claude compacts slightly before the computed point (OPEN, observation)

Compaction fired at 66.9k, 65.8k and 75.3k for computed points 67k, 67k and 77k.
Cause unknown (estimate of the next request, or a different buffer). Any margin math must
allow about 2k of slack.

## Live test procedure (Haiku)

Rules: run one worker at a time (WSL, about 7 GB RAM). Run `claive wait` in the background.
Give the worker a label with your own session prefix.

1. Copy the config to a scratch file:
   `cp ~/.config/claive/config.json <scratch>/config.json` (if the file is missing, start from `{}`).
2. Set low limits in the scratch file (the lowest valid `stop` is now 47000; values are multiples of 1000):
   ```
   jq '.engines.claude.budget = {"models": {"claude-haiku-5-5": {"warn": 50000, "stop": 57000, "max_handoffs": 1}}}' \
     <scratch>/config.json > <scratch>/c && cp <scratch>/c <scratch>/config.json
   ```
   Then `export CLAIVE_CONFIG=<scratch>/config.json` in the shell that runs every later claive command of the test (open, show, wait, answer, close).
   A running worker keeps its launch values. A config edit affects only new workers.
3. Task prompt that fills context fast (save as a file):
   ```
   Read-only review task in <repo>.
   Read each file below IN FULL, in this order, one at a time, with the Read tool (page through
   each file until its last line; do not skip ranges, do not use grep instead of reading).
   After each file, write a 3-line summary of it: purpose, main functions or sections, one risk.
   1. docs/command-reference.md  2. docs/harness-integration.md  3. bin/claivelib/serve.py
   4. bin/claivelib/batch.py  5. bin/claivelib/messaging.py  6. bin/claivelib/orchestration.py
   7. bin/claivelib/cli.py  8. tests/test_workers.py  9. tests/test_ask.py
   Final reply: a numbered list with the 3-line summary for every file 1-9. Do not edit any file.
   ```
   Do not mention budget messages in the prompt.
4. Launch a reusable worker (tests issues 1, 2 and the answer path):
   ```
   claive open --detach --engine claude --model claude-haiku-5-5 --reasoning-effort xhigh \
     --workspace <repo> --read-only --turn-timeout 2400 \
     --label <prefix>-budget --prompt-file <prompt file>
   ```
   For issue 3 live, launch the same with `claive start` instead of `open --detach`.
5. Check the launch: `jq .launch.budget ~/.local/state/claive/<ID>/state.json` must show
   `compact: 77000`. `ps -ef | grep -o -- '--autocompact [0-9]*k'` must show `110k`.
6. Watch: `jq -c '{status, context_tokens, handoffs, turn_handoffs, budget_phase, budget_missed, needs_parent}' ~/.local/state/claive/<ID>/state.json`
7. Timeline per session (context per model call, budget messages, compactions):
   ```
   jq -r 'if .type=="assistant" then "A ctx=\((.message.usage.input_tokens//0)+(.message.usage.cache_read_input_tokens//0)+(.message.usage.cache_creation_input_tokens//0))"
     elif .subtype=="compact_boundary" then "COMPACT pre=\(.compact_metadata.pre_tokens)"
     elif .type=="user" then (.message.content|tostring|if test("claive budget") then "U-BUDGET" else "U \(length)ch" end)
     else empty end' ~/.local/state/claive/<ID>/events.jsonl | uniq
   ```
8. At `needs_parent.kind == "budget_limit"`: `claive show <ID>`, then
   `claive answer <ID> --message "Continue from the handoff."`, then check `prompt-0002.md`.
9. Close the worker (`claive close <ID>`).
10. Unset CLAIVE_CONFIG after the test. The user config is unchanged.

## Expected results and pass criteria

- Receipts with `source: "budget"`. The warn message is skipped when one result jumps past both
  limits. Then the stop message carries the handoff template.
- Each `handoffs/turn-0001-K.md` starts with `[claive-handoff]`. `session_id` changes after each.
- Inbox (`claive inbox --consumer <you>`) has one `budget_handoff` event per handoff.
- `claive show <ID>` has `Context budget: on | ... | compact 77k | handoffs N (this turn N) | missed M`.
- Issue 1 passes only if `budget_missed` stays 0. Record, for each miss: context at detection,
  the size of the next tool result, and `pre_tokens` of the compaction.
- Issue 2 passes if handoff 1 continues automatically and handoff 2 stops with `budget_limit`.
- Issue 3 passes (one-shot) if the question ends "Start a new worker from that handoff to
  continue." and `show` has no "Pending handoff" line.

## Source-runtime check, 2026-10-16

Worker `ff1c6986dcc5` used Claude Haiku 5.5 and a scratch registry and config.
The source launcher ran a read-only task with a 300-second turn timeout.
Scratch limits were warn 50,000, stop 57,000 and maximum automatic handoffs 1.
The task read `bin/claivelib/cli.py` and `tests/test_workers.py`.

The parent steered an additional arithmetic question requiring `BUDGET_LIVE_ANSWER=42`.
The exact question remained in the generated continuation prompt. The matching input nonce
confirmed continuation entry. Two handoffs reached `budget_limit`, with zero missed budgets
and zero malformed provider events. One explicit parent answer resumed the saved context.
Turn 2 returned the requested summaries and `BUDGET_LIVE_ANSWER=42` in its final result.
The parent closed the idle worker. The worker ended with exit 0.

One handoff incorrectly claimed no answer remained owed. Structural checks cannot prove model
claims. The retained question and explicit recovery preserved the final answer in this run.
The host config and installed launcher were unchanged.
Local evidence: `/tmp/claive-budget-implementation.GSHKh6/live/evidence.json`.
