# Steering a running worker

Pi supports native input at a model-call boundary using its RPC transport. Opt in
when launching a worker:

```sh
claive open --engine pi --enable-steering \
  --workspace /abs/repo --prompt-file /abs/task.md
claive steer ID --message 'Use the updated requirement' --wait 10 --json
```

`--message-file` can read an instruction from a file. This sends a correlated
`steer` command into the running Pi process. Pi queues the instruction after
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

Current Muse `exec`, Claude print, and Codex `exec` adapters explicitly reject
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
