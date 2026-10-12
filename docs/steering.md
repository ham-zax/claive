# Steering a running worker

Pi and Codex support native input at a model-call boundary through distinct transports.
Pi uses RPC and Codex uses App Server (v2 `turn/steer`). Opt in when launching a worker:

```sh
claive open --engine pi --enable-steering \
  --workspace /abs/repo --prompt-file /abs/task.md
claive steer ID --message 'Use the updated requirement' --wait 10 --json
```

For Codex, use the same commands with `--engine codex` when launching:

```sh
claive open --engine codex --enable-steering \
  --workspace /abs/repo --prompt-file /abs/task.md
claive steer ID --message 'Use the updated requirement' --wait 10 --json
```

Steerable Codex launches use a turn-scoped App Server subprocess, initialized with
`thread/start` or `thread/resume` followed by `turn/start`. Every `turn/steer`
request includes the current thread ID and the **expected active turn ID**;
Codex rejects stale or non-steerable turns. The normal `codex exec` transport
is unchanged when steering is not enabled. The bridge preserves the chosen
model, `max` effort, approval policy, read-only/write sandbox, web selection,
and retained thread sessions. Nonopted-in runs use an isolated Codex home with
only shared authentication and retained sessions; opting into user config shares
its config and skills, but not personal rules. An App Server acknowledgement
still does not prove the model used the new instruction. The ARM integration includes deterministic same-turn/rejection/resumption cases.
A separate real Codex 0.161.0 smoke on 2026-10-12 also confirmed same-turn
consumption: a `turn/steer` acknowledgement preceded a completed turn whose
response included the steered `CLAIVE_STEER_VERIFIED` marker. The ARM host
reported a bubblewrap/user-namespace warning; this no-tool smoke does not
establish that sandboxed shell tools work on the host.

`--message-file` can read an instruction from a file. This sends a correlated
`steer` command into the running agent process. Pi queues the instruction after
current tool calls finish and before the next model request. It does not change
the API request already in flight or stop a command. Provider acknowledgements
are reported as `acknowledged` with their disposition; acknowledgement does not
prove the model consumed or complied with the instruction. `show --json` exposes
turn-bound receipts and counts separately from queued follow-ups.

Receipt states are `pending` (local submission), `sent` (written to provider),
`acknowledged` or `rejected` (provider response), `not_delivered` (turn ended
before delivery), and `unconfirmed` (sent but no response before turn ended).
Pending requests cannot spill into another reusable turn. Instructions must be
nonblank and at most 1 MiB. `--wait` times out with exit 124; it does not cancel
or withdraw a sent instruction. Rejected/unconfirmed input exits nonzero.

Existing Pi launches retain JSON print mode. They require a new launch with
`--enable-steering` to use RPC. `followup` still queues a separate turn; `cancel`
still stops the worker process group. Steering and single-turn model fallbacks
cannot be combined. All other launch policy (model, tools, read-only settings,
extensions, provider overlays, session retention) is preserved.

Current Muse `exec` and Claude print adapters explicitly reject
steering. Muse's installed headless mailbox returns
`external_agent_ingress_closed`; its MSP `turn/steer` surface requires a persistent
`muse serve` host that this adapter does not launch. These are current transport
limitations, not claims that the providers lack every possible interactive API.

Protocol evidence: installed Pi 1.1.0 `dist/modes/rpc/rpc-mode.js` accepts JSON
`prompt` and `steer` commands, emits correlated `response` events, and shuts down
on stdin EOF. `dist/core/agent-session.js` implements `steer` by queuing input for
the next model call. Fixture regressions cover acknowledgement, rejection,
missing acknowledgements, and reusable turns. An installed-Pi smoke run against
a local deterministic OpenAI-compatible endpoint verifies that a steering token
appears in the second model request and changes the result within the same turn.
