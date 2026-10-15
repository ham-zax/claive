# Design note: worker messaging, channels and roles

Status: revision 3. Features 1 to 4 (`send`, `wait --inbox`, `ask`/`peek`, refusals and
receipts) are built. The rest is a proposal.

Source: [Local Operator](https://github.com/damianvtran/local-operator) (`lop`). Revision 2
uses its source code, not only the README. The main files are
`local_operator/harness/comms.py` (the `hub` tool),
`local_operator/guides/peer-messaging/GUIDE.md` (the `send` tool) and
`local_operator/session/runtime/inbox.py`. This revision also applies three reviews:
first principles, pre-mortem and second-order effects.

Local Operator is a complete agent harness. claive stays a delegation layer under a
parent harness. This note copies ideas, not the runtime.

## What the source code shows

1. **lop does not let child agents talk to each other.** `comms.py` says: "a child
   addresses exactly one peer, its parent ... a child that needs something from a sibling
   says so to the parent rather than talking sideways to it." Only top-level sessions
   that a human started can send messages sideways. Channels that the parent opens
   (feature 3) are therefore new. lop has no design for them that claive can copy.
2. **lop has no guard against message loops.** It has only a mailbox limit
   (`MAX_INBOX_ROWS = 500`), a `no_reply` rule for the agent, and reuse of receipts in
   the same turn. claive must design its own guard (see the pre-mortem).
3. **Delivery happens at a safe point.** Notes and questions go into the child at a
   tool-batch boundary (an "aside"). Steering is a real user message. A running tool is
   never stopped.
4. **A question is withdrawn when it becomes stale.** When the asker stops waiting
   (timeout, or the child finished), the question does not arrive in the child's context
   later.
5. **An answer can come by two paths.** The child should answer with the tool, but an
   answer in plain text with no tool calls also counts. Without this, a model that forgets
   the tool makes the parent wait until the timeout.
6. **Results are written for the sender.** Examples: `delivered and woke the session`,
   `delivered to the mailbox`, `delivered mid-turn (steered)`. When the acknowledgement is
   lost, the result says that the message "may or may not" have arrived and tells the
   sender to check before a resend.
7. **Messages that need no answer are discouraged.** A peer that has nothing to answer
   ends with `no_reply`, which produces no output and wakes nobody.
8. **Clear refusals.** lop refuses ambiguous targets (it lists the pids), role words used
   as addresses, sessions that were never used, sends to itself and bodies that are too
   large. Each refusal tells the sender how to fix the request.
9. **Peek has limits.** At most 50 steps, with a character limit for each step.

## Goals

- The parent can send messages to workers, read them and question them during a turn.
- The parent can give a group of workers permission to send messages to each other.
- Workers get named roles that limit their tools.
- The parent stays in control. Every message has a receipt. Peer text is evidence, not
  authority.
- The cost of messages stays bounded and visible.

## Non-goals

- Messages between workers without a channel that the parent opened.
- Workers that open channels, spawn workers or add members.
- An HTTP server. Replacing the parent harness.

## Review 1: first principles

What a channel needs at minimum:

- **Delivery into a worker.** claive already has this. Steering puts text into a running
  turn (Pi, Codex, Claude), and a follow-up turn puts text into an idle worker.
- **Addressing.** claive already has aliases. Add the refusal of ambiguous targets.
- **A limit and an audit trail.** claive already has these for `conversation` (receipts,
  timeout, mission link).

Result: a channel is a `conversation` where the workers decide the order of speakers,
not the controller. Build it on the conversation code and the `send` code. Do not build a
second messaging system.

Assumptions to discard:

- "Every message must wake the target." This is a convention from lop. It is not a need,
  and it is expensive (see review 3).
- "Workers need a live socket." claive is file-based. A message file plus a delivery at the
  next safe point is enough.

Cheapest kill test: open two Pi workers in a channel on one real task. Measure the message
count, the tokens, and whether the two workers finish sooner than one worker with
`conversation`. If they do not finish sooner, stop after features 1, 2 and 4.

## Review 2: pre-mortem

The failure frame: "Three months later, we turned channels off. They cost too much and
the parent could not trust the results."

| # | Failure | Cause | Prevention (and its check) |
|---|---|---|---|
| 1 | Message loops spent the token budget | Each message woke the target, and the agents answered acknowledgements | Message budget for each channel and for each pair. `no_reply` rule in the worker prompt. Messages with no question never wake the target. Test: two scripted workers that always answer must stop at the budget. |
| 2 | A worker changed its task because of a peer message | Peer text was read as an order | Frame peer text as evidence. Assignment, permissions and file ownership never change. Test: a peer message that says "edit file X" is refused when X belongs to another worker. |
| 3 | The same message arrived twice | The acknowledgement was lost, and the sender sent again | Every message has an ID, and delivery ignores a duplicate ID. A lost acknowledgement returns `unconfirmed`, not `failed`. Test: replay one message ID. |
| 4 | Messages to a finished or closed worker were lost without notice | Delivery accepted the message and the target never read it | Refuse with the reason (`closed`, `finished`, `not in channel`). Test: send to a closed worker. |
| 5 | The parent lost track of the channel | Events did not reach the parent | A channel summary goes to `claive inbox`. `wait` returns on channel events (budget reached, question, close). |
| 6 | A question arrived after the asker gave up | The question stayed in the queue | Each question has a deadline. claive withdraws it when the deadline passes. (Copied from lop, point 4.) |

## Review 3: second-order effects

- **First order:** workers coordinate without the parent, so the parent does less relay
  work.
- **Second order:** every `wake` costs a full model turn. A broadcast to N workers in
  `wake` mode costs N turns, and the answers can cost N more. Cost grows with N², not N.
- **Third order:** workers start to spend their turns on discussion and not on the task.
  The parent then reads long transcripts to learn what happened.
- **Changes to the design:**
  - The default mode is `note`. `wake` is only for a direct message that asks a question.
  - A broadcast (`--all`) never wakes a worker.
  - Each channel has a maximum number of members (default 4).
  - When a channel closes, it writes a short summary for the parent: who sent what,
    the cost, and the decisions.

## Proposed features (priority order)

### 1. `claive send`: parent-to-worker delivery

```sh
claive send ID --mode note|wake|now --message-file /abs/msg.md [--id MSG_ID]
```

| Mode | Behaviour | Result string |
|---|---|---|
| `note` | Queue the message. The worker reads it at its next turn. | `queued for next turn` |
| `wake` | Queue the message. Start a turn if the worker is idle. | `delivered and started a turn` |
| `now` | Deliver at the next safe point in the running turn, through steering. If the worker has no steering, use `wake`. | `delivered mid-turn` |

Other rules:

- A lost acknowledgement returns `unconfirmed: check before resending`.
- A repeated `MSG_ID` returns the first receipt and does not deliver again.
- Channels and `ask` use this delivery code. Build it first.

Built. Limits as built:

- `wake` and `now` fall back to a note when the worker is busy. Muse has no steering, so
  `now` falls back to `wake` for Muse.
- `wake` does not check `needs_parent`. A wake can start a turn while the worker waits for
  an answer (`followup` does the same).
- A note to a worker that gets no more turns is never read.
- `--id` takes 32 lowercase hex characters. Without it, claive makes one.

### 2. Event-based `claive wait`

Built as `claive wait [IDs] --inbox CONSUMER [--json]`. It returns at the first event:

- a listed worker settles (`event: worker`)
- a new inbox event for the consumer (`event: inbox`; the cursor moves; exit 3 if an event
  needs the parent)
- the timeout (`event: timeout`, exit 124, the cursor does not move)

Channel events come with feature 5. With `--json`, the human report goes to stderr.

### 3. `claive ask` and `claive peek`

```sh
claive ask ID --message "..." --timeout 120   # question, wait for the answer
claive peek ID [--steps 5]                    # last steps, no interruption
```

- `ask` delivers with `now` (or `wake` when the worker is idle, or `note` when it is busy
  without steering). The timeout is 0 < S <= 3600 seconds (default 120).
- The worker answers with `claive reply MSG_ID --message ...`, or the final text of the
  delivery turn counts. Built: lop's "an assistant message with no tool calls is the answer"
  rule is not added, because in claive such a message ends the turn, so the final-text rule
  already covers it.
- When the timeout passes, claive withdraws a question that has not arrived (note, wake
  request or steering request). The deadline is stored with queued messages, wake requests,
  and steering requests. The steering pump marks an expired steering request `withdrawn`
  before sending it. A delivered question without an answer ends `expired`. Exit codes:
  0 answered, 124 withdrawn or expired, 3 the worker needs the parent, 1 refused or failed turn.
- `peek` reads the event log only, for the parent only. Steps have stable 1-based numbers
  and a total; `--steps` (1..50, default 5) and `--start N` page through them. Each step
  is cut to 600 characters in the middle (head and tail kept), as in lop.

### 4. Refusals and receipts

- **Ambiguous target:** worker commands accept a 4..11 character ID prefix. An ambiguous
  prefix lists the matches and exits with an error.
- **Refusals with reasons:** sends to itself and sends from workers are refused. `wake`
  (and the wake of `now` and `ask`) is refused while the worker waits for a parent
  decision; `note` stays allowed. Channel refusals come with feature 5.
- **Receipts in both directions:** each delivered message is a `<parent-message>` envelope
  (adapted from lop) with an instruction line and `[Message ID: <id> | from: <sender>]`.
  The sender is `parent ($CLAIVE_PARENT)` or `parent (unknown harness)`. The sender's
  receipt has the target, engine and model. The final text of each turn is kept in
  `results/turn-NNNN.txt`.

### 5. Channels opened by the parent

This is new. lop does not have it (see point 1 of "What the source code shows").

```sh
claive channel open @builder @critic @tester \
  --max-messages 20 --max-per-pair 6 --timeout 900   # prints CH_ID
claive channel show CH_ID [--json]
claive channel post CH_ID --message "focus on the auth bug"
claive channel close CH_ID
```

Inside a worker turn (claive sets the environment):

```sh
claive msg @critic --ask "Is the fix in auth.py correct?"   # may wake @critic
claive msg @critic "I pushed the fix to auth.py"           # note, no wake
claive msg --all "tests are green"                          # note to everyone, never wakes
claive msg --no-reply                                       # end a peer turn quietly
claive inbox --mine
```

Rules:

1. Only the parent opens or closes a channel or changes its members. The recursion guard
   blocks workers from these operations.
2. A worker can send messages only to members of its open channels. claive refuses and
   logs any other target.
3. Only a direct `--ask` can wake a worker. Notes and broadcasts wait for the next turn.
4. The limits are: total messages, messages for each pair, time, size (128 KiB) and
   members (4). When a limit is reached, the channel closes and the parent gets an inbox
   event with a summary.
5. A message cannot change a worker's assignment, permissions or file ownership.
6. The transcript is stored in `$CLAIVE_DIR/channels/<id>/`. Each message has an ID and a
   receipt.
7. Build channels on the `conversation` code. `conversation` stays as the fixed-order
   form.

### 6. Named roles

Example roles: `reviewer`, `coder`, `scout`. A role sets:

- the tool set (for example, `reviewer` gets no edit tools)
- the standing instructions
- the sandbox

Workers that a worker starts inherit its limits. This replaces the simple read-only/writer
switch. Engines that cannot limit tools must refuse a role that needs a limit.

## Further items from lop (H to M)

A second survey of lop found six more items. Each one adapts one lop module (MIT,
Copyright (c) 2024 Radient Inc.). Items A to G are the features above and the lop items
that make them better (envelopes in human output, redaction in `peek`, intent line,
`ask --schema`, scheduled wakes, channels, roles).

- H. Terminal control codes (lop `ansi.py`). Human output (`show`, `logs --stderr`, `peek`,
  `inbox`, wait reports, conversation output) removes escape sequences and control
  characters from worker and tool text. JSON output is already safe.
- I. Restart `serve` after an install (lop `buildwatch.py`). `serve` records the install
  fingerprint when it starts. Between goals, when no goal runs, it compares the fingerprint
  again and starts itself again with the same arguments when it changed.
- J. Failure note for the next turn (lop `incidents.py`). When a turn of a reusable worker
  fails, is cancelled or times out, the next prompt starts with one line that gives the
  failure kind and a short error. The note is given one time.
- K. Process names (lop `procname.py`). The supervisor sets a clear process name, so that
  `ps` and `pgrep` can find one worker without incorrect matches.
- L. `claive fork ID` (lop `fork.py`). A new reusable worker starts from a copy of the
  provider session of an idle worker. Engines that cannot fork refuse with a clear error.
- M. Monitors (lop `monitors/`). The parent adds a command that runs at an interval in the
  workspace of a worker. When its output changes, the worker gets a message with a short
  diff. The first run records the baseline only. The parent must give a read-only command.

## Later

- Scheduled wakes ("check this worker again in 30 minutes"). WSL has no supervisor, so a
  wake fires only while claive runs.
- Saved teams with layered briefs. lop uses three layers: the agent base prompt, a team
  "collaboration" brief and a "project" brief. You can then use one roster on many
  products.
- Local Operator as an engine (`lop exec --json`). Try only as an experiment.

## Build order

1. Done: `send` (feature 1) and event-based `wait` (feature 2)
2. Done: `ask` and `peek` (feature 3), and refusals and receipts (feature 4)
3. Run the kill test from review 1 with a minimal channel
4. If the kill test passes: full channels (feature 5)
5. Roles (feature 6)

## Open questions

1. Is `note` as the default channel mode acceptable? Review 3 recommends it. Only `--ask`
   wakes a worker.
2. Answered: Muse has no steering, so `now` falls back to `wake`.
3. Can channel members `peek` each other, or only send messages?
4. Do you accept the default limits: 20 messages, 6 for each pair, 4 members and 900 s?
