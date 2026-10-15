# Future improvements

Status: proposals, 2026-10-15. A1 to A4 are implemented. Nothing else is approved for implementation. Each item needs the
user's approval before work starts. Sources: the retro of the worker-freedom session
(2026-10-15) and a review of Sno Station (`sno-ai/sno-station`, `sno-ai/sno-station-skills`).

## A. Fixes from the retro

### A1. A detached launch must fail when the supervisor does not start

Implemented 2026-10-15.

- **Seen:** `claive open --detach` printed `Worker b08d9bb64dd7 | ...` and exited 0. The
  supervisor never wrote `supervisor_pid`. Four detached opens ran in the same minute. Later,
  `load()` marked the worker `interrupted` ("worker supervisor did not start"). The parent found
  the problem only through `claive list`.
- **Code:** `spawn_supervisor()` in `bin/claivelib/cli.py` starts the process and returns at
  once. `load()` sets the 5 s "did not start" rule.
- **Proposal:** after `Popen`, wait up to 5 s until `state.json` has `supervisor_pid` or the
  child exits. If neither starts the worker, exit 1 and print the last lines of
  `supervisor.log`.
- **Test:** a fake launcher that exits before it writes the PID gives exit 1 and the log text.

### A2. A worker can run a different claive than its supervisor

Implemented 2026-10-15.

- **Seen:** Codex runs commands with `bash -lc`. The login profile puts `~/.local/bin` first, so
  the channel worker ran the installed (older) claive, not the test copy, and got "workers
  cannot send messages to workers".
- **Proposal:** export `CLAIVE_BIN` (the absolute launcher of the supervisor) to each worker.
  Peer-send hints and worker-side commands use `$CLAIVE_BIN`.
- **Test:** a worker environment has `CLAIVE_BIN` equal to `launcher_path()`.

### A3. Fake engines must refuse the flags that the real CLI refuses

Implemented 2026-10-15.

- **Seen:** Muse refuses `--permission-profile` with `--disable-approval`. Every test passed.
  Only the live check found the conflict.
- **Proposal:** the fake Muse engine refuses the same flag pairs as the real CLI
  (`--permission-profile` with `--disable-approval` or `--approval-mode`). Add a pair to the
  fake when a live check finds one.

### A4. Pyflakes rules in lint and CI

Implemented 2026-10-15.

- **Seen:** during the merge, a local variable `summary` in the channel handler hid the function
  `summary()`. `compileall` and `shellcheck` cannot find this. The suite found it later.
- **Proposal:** add `ruff check --select F bin/claivelib tests` to `just lint` and to
  `.github/workflows/test.yml`. Today it reports only 4 unused imports (`steering.py:6`,
  `tests/test_peek.py:19`, `tests/test_steering.py:16`, `tests/test_workers.py:12`).

### A5. Working rules for the parent (no code)

- Do not edit files while the full suite runs. One run tested a mix of old and new code.
- Give `cat` a file, always. A bare `cat` waits on stdin until the command hangs.
- Make glob patterns for cleanup exact. `claive-wt-*` also matched `claive-wt-archive`.

### A6. Open question: Claude plan mode

The live check (2026-10-15) showed that `ExitPlanMode` is not available in `--print`. The model
refused every write itself, so nobody has proved that the harness blocks a write in plan mode.
A future check needs a write that the model tries and the harness denies
(`permission_denials` in the result event).

## B. Ideas from Sno Station

Sno Station is a skill set for Claude Code and Codex: cross-vendor review, quota handover,
work orders with proof, and a nightly retro. Most of it overlaps with claive (critic,
`conversation`, checks and `accept`, `mission`, `inbox`). We do not install the full set:

- Two skills can match one request ("review this" starts Sno's peer-review or a claive critic).
- Each skill description adds context to every turn (31 skills).
- The installer runs `curl ... | sh`, and `sno` updates itself. Skills here install through
  Skillshare.

### Licence

Both repositories use **Apache-2.0**, not MIT. claive is MIT. We can copy Apache-2.0 code or
text into claive on these conditions:

- Keep the copied files under Apache-2.0. Add the Apache-2.0 licence text (for example
  `LICENSES/Apache-2.0.txt`).
- Add a `NOTICE` entry with the Sno copyright line ("Copyright Sno Station Skills
  contributors" or "Copyright SNO contributors").
- Put a note in each copied file that we changed it, if we change it.

Recommendation: copy the review prompt (B2) with attribution. Write the quota read (B1) again in
Python from the protocol facts below. The Sno script is 1160 lines of Bash with Cursor, Hermes
and OpenClaw support that claive does not need.

### B1. Read the subscription quota without spending it

claive now finds an exhausted quota only when a turn fails. Sno's
`apps/subscription-quota-check/bin/subscription-quota-check` reads it with no model call:

- **Codex:** start `codex -s read-only app-server`. Send `initialize`, `initialized`, then
  `{"id":2,"method":"account/rateLimits/read"}`. Read the line with `"id":2`, then close stdin.
  claive already speaks this protocol in `engines/codex_appserver.py`. `codex login status`
  must say `Logged in using ChatGPT`, otherwise the state is "needs login".
- **Claude:** run `claude --safe-mode --no-session-persistence -p --input-format stream-json
  --output-format stream-json --verbose`. Send
  `{"type":"control_request","request_id":"q","request":{"subtype":"get_usage"}}`. Read the
  `control_response`. `rate_limits_available: false` means an API key, not a subscription.
- **Staleness:** `~/.claude.json` `cachedUsageUtilization.fetchedAtMs` older than 1 hour is
  unknown, not a reading.
- **Rule from Sno:** read only on request, or after a vendor refused a turn for quota. Never
  poll on a timer: the Claude usage endpoint has its own rate limit, and a timer gets 429s and
  then stale numbers.
- **Proposal:** `claive engines --quota` (or `claive quota`) prints the 5-hour and weekly
  windows for Claude and Codex, with Sno's exit codes: 0 usable, 1 blocked, 3 unreadable,
  4 needs login. The parent reads it before it chooses an engine. Muse and Pi stay unread.
- **First step:** a design note, then a live check of both reads on this machine.

### B2. A stronger `reviewer` role preamble

Sno's review prompts (`peer-review/skill/assets/adversarial-review.prompt.md`, also a plan and a
test variant) have rules that the claive `reviewer` role does not have:

1. **Cut first.** Before the defect search, list what the change adds that no observed failure
   or stated requirement needs (a lock, cap, retry, flag, gate, queue). This agrees with the
   worker-freedom rule.
2. **Ordinary use first.** Walk the normal call end to end before any rare input.
3. **Three gates for each finding:** the trigger (a real sequence that reaches it), the
   consequence (what breaks, for whom), and a refutation (try to disprove it; report only what
   stays).
4. **Order for process code** (supervisors, messaging, skills): a failure with no signal, a
   stall or a wait with no timeout, a wrong flow, two writers on one resource. "Wrong but loud
   is acceptable. Wrong and silent is the bug." Retro item A1 is this type of bug.

**Proposal:** add these four rules to the built-in `reviewer` preamble in `roles.py`, in our own
words. Or copy the prompt text with the Apache-2.0 attribution above.

### B3. Rule for checks

From Sno's `charter` skill: "The expected result comes from the requirement or from independent
data, never from the program's own output. For a defect, the check reproduces the defect
first." claive-orch already needs a verifier that fails at base. **Proposal:** add the first
sentence to the check-manifest section of `docs/harness-integration.md` and to the
`claive-orchestration` skill.

### B4. Count what the critics find

Sno's `catch-report` counts the problems that the second model found, apart from the problems
that agents found in their own work. claive has `stats` and `pick`, which rank critics by past
results. **Proposal (low priority):** `claive stats` shows, for each critic family, the number of
findings that led to a kept change. Do this only if the ladder runs often enough to give data.

### B5. Quota handover (after B1)

Sno's `rotate-agent` and `handoff` move a long run to another vendor when the quota is low:
the working agent writes a brief with the exact commit, and the receiver checks the brief before
it changes a file. claive has `mission` and a Muse-to-Pi quota fallback. **Proposal:** none now.
Consider it only after B1 works. It needs a reader that runs during the night, so it conflicts
with the "never poll" rule unless the user starts it on purpose.

## C. From the budget gate live tests (2026-10-15)

### C1. Warn when a stop reply holds the marker below the first line

In live run 37c07187a9fc, the worker wrote a preface and then `[claive-handoff]` on line 22.
claive counted no handoff and gave no warning. **Proposal:** in phase `stopping`, if the reply
holds the marker below the first nonblank line, record a `budget_missed` reason that names the
line. Accepting the marker anywhere is a separate decision.

### C2. `claive list` shows ASK for a closed worker

After `claive close` on a worker with `needs_parent: budget_limit`, `claive show` reports
`completed | session closed`, but `claive list` still shows `ASK`. **Proposal:** clear or
ignore `needs_parent` when the session is closed, and add a test for the list state.

### C3. Live budget tests use a scratch config

The live tests edited `~/.config/claive/config.json` and needed a manual restore.
`CLAIVE_CONFIG` already exists. **Done:** docs/budget-mode-live-test.md now sets CLAIVE_CONFIG to a scratch config.

## Order

1. A1, A4 (small, they catch real bugs).
2. A2, A3.
3. B2, B3 (text only).
4. B1 design note and live check.
5. A6, B4, B5 when there is a need.
