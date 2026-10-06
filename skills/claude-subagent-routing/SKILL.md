---
name: subagent-routing
description: Claude Code rules for delegating work. Workers are Muse and Pi, driven through the worker-orchestration skill (codex-workers / codex-orch). Sonnet 5.5 only when the user asks for it. Hamza grants standing, unrestricted permission for Muse and Pi workers; load before delegating.
---

# Subagent routing (Claude Code)

Opus (me) orchestrates: I plan, write worker task text, verify, and synthesize.
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
   - A task with an executable check goes through the `codex-orch` ladder
     (default arm D). Independent chores without a check go to plain
     `codex-workers` workers.
   - Launch only through `codex-workers` / `codex-orch`. Never call `muse` or
     `pi` directly.
2. **`sonnet-5-5` agent**: allowed, **not preferred**. Use it only when the user
   asks for Sonnet for this task. Then use effort `high`, never `xhigh`. Never use
   Sonnet 5 or the Agent tool's generic `sonnet` option. If `sonnet-5-5` is
   unavailable, ask; do not substitute.

Not allowed: `agy` / `agy-relay`, `opencode` / `opencode-relay`, standalone Muse
(outside `codex-workers`), the `codex` review agent, and any other model or
agent, unless the user names it for the task.

## Rules

- Escalation threshold, stated in every worker prompt. A worker decides only
  local, reversible calls where the evidence is clear, and lists them in its
  report. It stops and reports instead when a call would change a root cause, a
  design, public behaviour, a data format, scope, or the acceptance criteria.
- Give exact files, symbols, and the planned change so workers skip exploration.
  Ask for raw evidence (verbatim output, file:line), not conclusions.
- No cap on concurrent Muse/Pi workers, but RAM is about 7 GB: only one test or
  build run at a time, and back off if memory gets tight.
- Verify every change myself (`git diff --stat` plus a focused check) before
  reporting it done.
- Muse quota exhausted: switch the affected lane to the pre-approved fallback,
  Pi `muse-spark-1.3-contributor-free` at `max`, and tell the user (with the
  reset time). Any other fallback needs the user's approval; never substitute
  Sonnet. Return to Muse after the reset.
- Retire workers when their task chain ends (`codex-workers close`). Start fresh
  ones with a compact handoff rather than growing one worker's context forever.
