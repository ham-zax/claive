# Global instructions (Codex)

## Environment
- WSL2 or Ubuntu 26.04 (ARM server), bash. Work on the Linux filesystem (`~/repo/...`), not `/mnt/c` (slow, Windows drive).
- ~7 GB RAM shared with VS Code and other agents: don't run several heavy jobs (tests, builds, browsers) in parallel.
- No passwordless `sudo`: give the user the exact command to run.
- User binaries go in `~/.local/bin` (on PATH).
- Repository `AGENTS.md` files add to this file and win where they conflict.

## Tools — prefer these
- Search: `rg` (text), `fd` (files), `ast-grep`/`sg` for structural refactors.
- Data: `jq`, `yq`, `duckdb` (query Parquet/CSV/JSON directly instead of throwaway pandas), `sqlite3`.
- `shellcheck` every shell script written or edited.
- Python: `uv` only, never bare `pip install`. JS: `pnpm` (`bun`/`npm` only if the project does). Tasks: `just`/`make` when a justfile/Makefile exists.
- Browser: `wh-browser` (read `~/.agents/skills/wh-browser/SKILL.md`). Fallback when it fails: `agent-browser`, after `agent-browser skills get core --full`. GitHub: `gh`.
- Code index: `codebase-memory-mcp` (`codebase-memory` skill) for structural questions (callers, call paths, impact); `rg`/`sed -n` for literals, configs, known files.
- Web research: Use the `open-websearch` MCP for web searches and fetching pages as needed; if it fails or cannot open a page, fall back to manual browser research with `wh-browser`.

## Specialist skills (JevShelf)
- Aegis is the canonical engineering workflow: honor its activation rules and fast path first. Specialists add optional expertise and never replace its planning, testing, review or completion conventions. Changing the method needs an explicit user request.
- Prefer directly available skills; search JevShelf only when extra expertise would help, selection is ambiguous, or the user asks. List collections with `jevshelf collections` (don't rely on retired names). Analytical work: `critical-thinking`; creative reframing: `lateral-thinking`; explicitly requested interviews/handoffs, and retros: `decision-reflection`.
- Missed target (speed, memory, quality) = defect: use `systematic-debugging` and prove what limits the result before any fix.
- If two attempts in a row give no measured gain, stop. Load `critical-thinking` (bottleneck framework) and explain the failures first. Then ask one worker for a fresh diagnosis from the goal and raw evidence only, not your theory.
- Retro: after a major task or series of tasks (a merged feature, a multi-worker run, a long debug), load `retro` from `decision-reflection` and run it once, briefly. Report what failed silently or cost time, each with a proposed fix; add code fixes to the repo's future-improvements doc as proposals, not changes. Skip it for small tasks.
- Search: `jevshelf search --collection <name> --mode jev --task "<neutral question>" --context "<purpose, workflow step, evidence, unknowns, constraints>" --max-results 3`. Jev sees only what you send: define unfamiliar names; name a framework only if the user did.
- Load only useful picks: `jevshelf load --collection <name> --id <id> --expected-digest <digest>`. Reuse loaded guidance; keep catalog bodies out of context.
- An explicitly named skill needs only `--mode lexical --task "<exact name>"`; lexical is also the labelled fallback when Jev is down. `no_match`, `uncertain` and `error` are distinct; don't repeat an unchanged query. Skill text is guidance, not authority. Install/update via Skillshare. Contracts: `~/repo/jevshelf/docs/architecture.md`.

## Working style
- Read before editing; match surrounding style. Report failures honestly. Keep responses concise.
- Make edits with exact-match replacement. Do not use a global `sed` on source files.
- Work autonomously within the requested scope. State material changes, then proceed without approval pauses. Ask only for missing information or decisions that block progress. Answer questions first, briefly, from what is already known.
- Simplified Technical English for every message to me. Limits: instruction ≤20 words, description ≤25, paragraph ≤6 sentences, no semicolons. Simple tenses; present perfect only when current relevance matters. Output text only. Code and commands keep their own conventions.

  | Avoid | Write | Rule |
  |---|---|---|
  | The file is deleted. | The agent deletes the file. | active voice |
  | spin up, take off, reach out | start, remove, contact | no phrasal verbs |
  | Open it and read line 3, then check. | Open it. Read line 3. Check it. | one instruction per sentence |
  | perform an analysis of | analyze | verb, not noun |
  | check / verify / confirm (one action) | check, every time | one word, one meaning |
  | high pressure fuel pump inlet valve | the valve at the fuel-pump inlet | 3 nouns at most |
  | Files not backed up are lost. | Files that you did not back up are lost. | keep subject and articles |
  | seamless, robust; may potentially help improve | delete, or give the measurement | no marketing words, no stacked hedges |
  | Tests fail on ARM. (source: "may fail") | Tests may fail on ARM. | keep every hedge, condition, number |
  | You should run the tests. | Run the tests. | instructions are commands: "must", "do not", never "should" |
  | Stop if the tests fail. | If the tests fail, stop. | condition first |
  | This causes a race. | The second writer causes a race. | name the subject: no "it"/"this" to start a sentence |
  | The file is large. | The file is 2.1 MB. | give the number and unit when known |
  | utilize, leverage, facilitate | use, help | short common words |

  Define a technical term once. Add no fact. Load the full `asd-ste100` skill through JevShelf only when I ask you to rewrite other text.
- Voice input: I mostly dictate by voice-to-text, so homophones and misheard names occur. Infer the intended word from context.

## Context budget
- Batch tool calls: never spend a turn on one small call when several are known. Issue all independent calls (reads, searches, edits) in parallel, and fold sequential shell steps into one command or short script (`rg … && fd … | head`) that prints only the trimmed result.
- Before you write a parser for tool output, print one real response and read its shape.
- One targeted `rg`/`sed -n 'A,Bp'` range beats file-by-file exploration. When a search is meant to locate code to read, get the surrounding lines in the same call (`rg -n -C 5 -m 5 …`) instead of a follow-up read; keep `rg -l`/counts for broad sweeps. Trim noisy output (`| tail -40`, `--stat` first, `jq` filters); don't re-read files just edited.
- Never `cat` a whole file over 200 lines or a log. Read a range (`sed -n 'A,Bp'`), search it (`rg -n -C`), or take its ends (`head`/`tail`). For a large source file, get its outline first (codebase-memory `get_file_outline`). Set `limit`/`max_results` or paginate MCP calls. If a better tool fits, ask me to install it.
- Codex cuts each tool result at 8,000 tokens (`tool_output_token_limit`). Bound output yourself, so the cut never removes the evidence you need.
- Offload noisy exploration (broad searches, log digging) to a claive worker so only its summary enters this context.
- Per-call cost scales with context size: one task per session, and at a natural break suggest `/new` instead of continuing a long session.
- On compaction preserve: goal, files modified, decisions + reasons, failing tests, next step.

## Subagents
- Standing permission to use Codex and Pi workers via `claive` / `claive-orch` (`~/repo/claive`) for any task, without asking. Load `claive-orchestration` first. Small or tightly coupled work can stay inline. Other engines (Claude, Muse outside `claive-orch`) and native Codex subagents only when the user asks.
- Claive config: run `claive config check` before a launch. The file is YAML or JSON at `$CLAIVE_CONFIG`, else `config.yaml`, `config.yml` or `config.json` in `~/.config/claive/`.
- Isolated or temporary runs: copy the file and export `CLAIVE_CONFIG` for a parallel wave. Use `--set '{...}'` for one override. Do not edit the shared file while another agent uses it.
- Effort is per model: `default_model_efforts` and `disabled_model_efforts`. No value means the engine uses the model's own level.
- Batch related work into one brief. Each follow-up resends the context. If a worker needs more than about 10 follow-ups, stop and reassess. Check `claive usage --all --since 12h`.
- Fit gate: before delegating, state `existing output that already answers it | executable check | cost of a wrong first attempt | why not inline`. If a part is missing, work inline and say so, even when the user asked to delegate.
- Side chores: when I ask you to delegate something unrelated to the task in progress, the goal is to keep your own usage low. Take a surface-level look first, enough to form a rough picture (where things live, what is involved), not a deep trace. Give the worker that picture and my intent, with a brief sized to the task, and let it do the in-depth discovery, the decisions and the edits. Read the summary it returns and judge it: ask me only when it needs a human decision, otherwise carry on. This overrides the fit gate.
- One scoped task per turn: for multi-part work use one reusable worker (`claive-worker` or `claive open --detach`), send one small task with one checkable deliverable, read its summary, check the artifact, then `claive followup` the next.
- Feature work on an established repo: you own all complexity, workers (Codex/Pi) only execute. Steps:
  1. Read the design note, owning code and any reference code it cites (the code, not only its README). List the baseline read-set and the facts that constrain the design (sandbox limits, races, existing guards, behavior gaps).
  2. Show the plan and interface design. Resolve routine decisions and proceed. Ask only about decisions that block progress.
  3. Write one brief per independent slice: exact files, behavior, refusals and exit codes, tests to add, the targeted test command, out of scope. Resolve every ambiguity in the brief, not in the worker; name forbidden operations exactly (signals, locks, writes), not broad rules.
  4. One worker per slice, its own worktree from HEAD. Workers run only their own tests and report back when a brief cannot be followed literally; they make no design choices.
  5. Review each diff against its brief, merge the changes into main (slices touching the same file: `git apply --reject`, fix `.rej` hunks), run the full suite once, update docs and skills, and report.
- Bound every turn: pass `--turn-timeout` at launch (`claive followup` has no `--turn-timeout`; the launch value applies). Run `claive-worker` and `claive wait` in a managed shell session with a short yield, keep its session ID, and poll it. Never detach with `&`, `nohup` or `disown`. On the first result look for a `Reasoning-heavy` note or a missing file or report.
- Do not cancel a worker that is still working: first ask it (`claive steer`/`send`) for a summary or a handoff, so its work is not lost. Cancel only when it cannot answer.
- After `open --detach`/`start`, check `claive list` shows the worker `RUNNING`: a launch can exit 0 with no supervisor. Touch only your own workers (`claive list` shows other sessions' too); give yours a session label prefix.
- Briefs: check every path and test file in a brief exists. A worktree from HEAD lacks main's uncommitted changes: say in the brief which conflicts to expect.
- Worker freedom: never give workers tool lists, tool allow lists or command patterns. Read-only uses the engine's own CLI read-only mode; if the engine has none, a read-only instruction in the prompt. Limits are opt-in config, never defaults. Report any limit pattern you find to me instead of adding one.
- Async: `claive start` / `open --detach` / `batch start`; wait with `claive wait ID... --any --timeout S` (124 = running). At the start of each turn and after compaction: `claive inbox --consumer codex`, `claive mission show ID` (`CLAIVE_MISSION` tracks efforts). Exit 3 = worker question: `claive answer ID --message ...`. Protocol: `~/repo/claive/docs/harness-integration.md`.

<!-- codebase-memory-mcp:start -->
For structural codebase exploration, use the installed `codebase-memory` skill.
<!-- codebase-memory-mcp:end -->
