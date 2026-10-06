---
name: subagent-routing
description: Claude Code rules for delegating work. Workers are Muse and Pi, driven through the worker-orchestration skill (codex-workers / codex-orch). Sonnet 5.5 only when the user asks for it. Load only after the user explicitly permits subagents for the current task.
---

# Subagent routing (Claude Code)

Opus (me) orchestrates: I plan, write worker task text, verify, and synthesize.
I keep every judgement call: diagnosis, design, acceptance criteria, and
interpreting results. Workers execute. A worker's report is evidence to check,
never proof.

## Allowed workers

1. **Muse and Pi through this repository's tooling**. This is the default and the
   only preferred route. Load the `worker-orchestration` skill and follow it:
   - Muse (`muse-spark-1.3-contributor`) implements.
   - Pi (`--engine pi --provider opencode2api --model <id>`) critiques, makes the
     diverse second attempt, or reviews. It never critiques Muse with a
     muse-spark model.
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
- RAM is about 7 GB: at most 2–3 concurrent workers, and only one test or build
  run at a time.
- Verify every change myself (`git diff --stat` plus a focused check) before
  reporting it done.
- Muse quota exhausted: stop giving it work, report the reset time, and ask the
  user. Never fall back silently, and never substitute Sonnet.
- Retire workers when their task chain ends (`codex-workers close`). Start fresh
  ones with a compact handoff rather than growing one worker's context forever.
