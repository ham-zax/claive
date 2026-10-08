---
name: subagent-routing
description: Claude Code rules for delegating work. Workers are Muse and Pi, driven through the worker-orchestration skill (claive / claive-orch). Sonnet 5.5 only when the user asks for it. Hamza grants standing, unrestricted permission for Muse and Pi workers; load before delegating.
---

# Subagent routing (Claude Code)

As the parent, I plan, write worker task text, verify, and synthesize.
I keep every judgement call: diagnosis, design, acceptance criteria, and
interpreting results. Workers execute. A worker's report is evidence to check,
never proof.

## Allowed workers

1. **Muse and Pi through this repository's tooling**. This is the default and the
   only preferred route. Load the `worker-orchestration` skill and follow it:
   - Muse (`muse-spark-1.3-contributor`) implements at effort `xhigh`, or `max`
     for very complicated tasks. `high`/`medium` only rarely, for trivial chores.
   - Pi (`--engine pi --provider opencode2api --model <id>`, effort always
     `max`) critiques, makes the diverse second attempt, or reviews. Prefer
     `mimo-v2.6-flash-free`, `big-pickle`, `space-bunny-free`; rarely
     `longcat-2.5-preview-free`. Never `nemotron-*` or `ling-3.1-flash-free`, and
     never a muse-spark model against Muse.
   - A task with an executable check goes through the `claive-orch` ladder
     (default arm D). Independent chores without a check go to plain
     `claive` workers.
   - Launch only through `claive` / `claive-orch`. Never call `muse` or
     `pi` directly.
2. **`sonnet-5-5` agent**: allowed, **not preferred**. Use it only when the user
   asks for Sonnet for this task. Then use effort `high`, never `xhigh`. Never use
   Sonnet 5 or the Agent tool's generic `sonnet` option. If `sonnet-5-5` is
   unavailable, ask; do not substitute.

Not allowed: `agy` / `agy-relay`, `opencode` / `opencode-relay`, standalone Muse
(outside `claive`), the `codex` review agent, and any other model or
agent, unless the user names it for the task.

## Async workers

Launch with `claive start`, `claive open --detach` or `claive batch start`
(plain Bash; they return at once). Wait with `claive wait ID... --any --timeout 540`
(exit 124 means still running). `--any` is required for several IDs: without it the
wait exits 1 at once and waits for nothing, so re-arm after each return until every
ID has settled. Without `--timeout`, use `run_in_background: true` to get a
completion notification. After compaction,
run `claive inbox --consumer claude` and `claive mission show ID`. Use a
mission (`CLAIVE_MISSION`) for multi-turn efforts. `goal ID ASK` inbox lines
come from the `claive serve` queue; answer them with `claive goal answer ID
--message ...`. The protocol is identical for
Codex and Pi parents: `/home/hamza/repo/claive/docs/harness-integration.md`.

## Workers can talk to each other

When two or more workers should react to each other (proposer/critic/reviewer, a
cross-model design debate), do not relay text by hand. Open reusable workers with their
own roles, name them with `claive alias set NAME ID` (then `@NAME` works in place of an
ID in any worker command), and run
`claive conversation start @a @b @c --message-file /abs/topic.md --rounds 2 --timeout 300 --json`
in a managed shell. Exit 3 means a worker asked a question (`claive answer`), 124/130 mean
inspect `pending_request.response_file` before resending. Details and limits: the
`worker-orchestration` skill, section "Let workers talk to each other".

## Rules

- Escalation threshold, stated in every worker prompt. A worker decides only
  local, reversible calls where the evidence is clear, and lists them in its
  report. It stops and reports instead when a call would change a root cause, a
  design, public behaviour, a data format, scope, or the acceptance criteria.
- Give exact files, symbols, and the planned change so workers skip exploration.
  Ask for raw evidence (verbatim output, file:line), not conclusions. Check every path
  and symbol against the repo before launch: a brief once named a file that did not
  exist. Treat handoff notes as hypotheses until `git log` or the code confirms them.
- No cap on concurrent Muse/Pi workers, but heavy runs (tests, builds, verifiers) share a
  9 GB budget (the user's memory note): the caps of heavy runs active at once sum to at most
  9G. CLAUDE.md says avoid parallel heavy jobs, so run one at a time, capped with
  `claive-memcap 9G` or `systemd-run` (see `worker-orchestration`), until the user settles it.
- Verify every change myself (`git diff --stat` plus a focused check) before
  reporting it done.
- Measure before you fan out. For performance or debugging, get the baseline or the
  reproduced mismatch first, then launch workers at that measured hot spot.
- Commit or stage only when the user asks. Workers never do, and `claive-orch integrate`
  applies its diff unstaged.
- Read bounded output: `claive logs ID --lines 40 | cut -c1-300` for a log, and
  `claive show ID | head -n 80 | cut -c1-300` for a report. Never dump a whole log.
- Muse quota exhausted: switch the affected lane to the pre-approved fallback,
  Pi `muse-spark-1.3-contributor-free` at `max`, and tell the user (with the
  reset time). Any other fallback needs the user's approval; never substitute
  Sonnet. Return to Muse after the reset.
- Retire workers when their task chain ends (`claive close ID`), and close a finished
  mission with `claive mission close ID`. Start fresh
  ones with a compact handoff rather than growing one worker's context forever.
