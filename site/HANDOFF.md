# Handoff: claive landing page (`site/`)

Written 2026-10-07 (IST) by Claude, in a cloud session that reached this machine through the `hamza-wsl` bridge.
**Status (updated after the build session, 2026-10-07): the first version of the site is built and passed a machine-measured inspection. Nobody has looked at it by eye yet.** `site/index.html`, `site/PRODUCT.md` and a repo-root `LICENSE` now exist; nothing is committed. **Read section 0 first.** Original status before the build: research and decisions done, no site code written.
**Updated 13:58 IST the same day:** the user answered the five open questions. The answers are recorded in section 9 and folded into the decisions, ledger and do-not-claim list below.

Not to be confused with `.claude/HANDOFF.md`, which belongs to the earlier `stats`/`pick` feature work. I did not touch it.

---

## 0. Build session update (2026-10-07, after the build)

**Read this section first. Where it disagrees with sections 1 to 3 and 10 to 12 below (which were written before any code existed), this section is current.**

The build session was Claude in a cloud session, working on this machine only through the webharness tools (read, write, exec, `wh-browser`). Its own sandbox could not see the repo, so it could not open the screenshots it took.

### What exists now (all uncommitted)

*Staging note:* by the end of the session `git status` showed `LICENSE`, `site/PRODUCT.md` and `site/index.html` as staged, and `site/HANDOFF.md` as staged-then-modified. The build session ran no `git add` and no commit, so someone or something else staged them (probably the other agent working in this repo). Nothing is committed. Check `git status` before committing, so that only the intended files go in.

- `LICENSE` (repo root): MIT, "Copyright (c) 2026 Hamza". `git status` shows it staged, but the build session never ran `git add`, so something else staged it. **The holder name was not confirmed with the user.**
- `site/index.html`: about 16.6 KB, one file, no JavaScript. Order: hero with the real shakedown run drawn as a "run record" (seven test squares, 6 then 7); the problem; how it works (L0 to L2 ladder, five glossary terms); why it's different (six points); what it's built on; the experiment; cheaper agents; try it; what isn't built; footer.
- `site/PRODUCT.md`: the impeccable product record. It was written from section 9's answers instead of a fresh interview; inferred fields are labelled in the file. It sits in `site/`, not the repo root, because of the "only touch `site/`" rule. No personal paths in it.
- Not written: `DESIGN.md`. The impeccable documenter step did not run.

### Design as built

Instrument printout on warm paper (D8). Ink plus two data colours only: green for pass, vermilion for fail or reverted. Light and dark. Newsreader for text and display, IBM Plex Mono for code and data only, both from Google Fonts with fallback stacks. That font link is the page's only external dependency. One motion moment: the seventh test square lands (off under reduced motion). No cards, no eyebrows, no gradients, no section numbers.

### Verified (machine-measured only)

- **Browsers:** Windows Chrome (devtools tier) and Linux Clearcote, at 375 and 1280 px, light and dark. No horizontal overflow (the install code blocks scroll inside their own boxes). All four font files load. Lowest contrast 5.56:1 light, 6.87:1 dark. No console errors or warnings. Hero text and run record do not overlap. The last test square ends at opacity 1.
- **Defect 1, found and fixed:** footer links were 15 px tall; they are padded now. Inline links inside sentences are exempt.
- **Defect 2, reported by the user and missed by the checks above:** on phones the header nav was squeezed beside the wordmark, so every label wrapped to two lines ("How it / works", "The / experiment"; links 49 px tall at 375 px, header 91 px). A wrapped link still passes a minimum-height test, which is why it slipped through. Fixed in `index.html`: below 36rem the nav drops to its own full-width row (three links, one line each, 44 px tall, header 100 px); at 577 px and up it is one row (header 55 px). Confirmed in Clearcote only, at 320, 360, 375, 576, 577 and 1280 px, plus dark at 375 px: all labels on one line, nothing overflows, no console messages. Lesson for next time: measure label line counts and gaps, not just heights.
- **Unit tests:** `python3 -m unittest discover -s tests -p 'test_*.py'` gave 195 tests, OK, 2 skipped (115 s). This replaces the "no verified count" gap in section 10. The earlier grep of 202 `def test_` is higher than the 195 that ran; the difference was not investigated. The page shows no test count.
- **Arbiter rules** (ledger rows 3 and 4, and the same-family part of row 5) were checked in `bin/claivelib/orchestration.py`. `decide()` is pure and returns one action. MAX_ROUNDS is 3, STALL_AFTER 2, BREADTH_ROUNDS 2, MAX_LANES 2. A worse round runs `reset --hard` to the best commit and `clean -fdq`, and the rejected attempt is kept under `refs/claive-orch/<run>/<lane>/rejected-rN`. A same-family critic is refused unless `--allow-same-family` is passed, which is recorded as a warning.
- **Still `code?`:** the default `--rounds` of 2, the tie reviewer, and `stats`/`pick` (row 15). The page does not state `stats`/`pick` numbers.
- **Shakedown numbers** (run `bd886bb32632`: 6/7 then 7/7, 245 s, 668k input tokens, 544k cached) match `07-trial-runs.md`.
- **Paper:** title, first author and date confirmed from a mirror of the arXiv page (prismix.dev), not arXiv itself. *Refining Over Resampling: Test-Time Self-Correction for LLM Reasoning*, Ahsan Bilal and 6 other authors, submitted 6 Aug 2026. It evaluates math benchmarks (AIME24, AIME25, AMC, OlympiadBench, MATH500), not coding, and the page says claive adapts the idea. **The paper numbers in ledger row 12 are still unverified and are not on the page.**
- **Banned-string grep** of `index.html` (`/home/hamza`, email, fmge, sslip, sandbox, offline, windows, macos, benchmark, win rate, production-ready) found nothing.

### Not verified

- **Nobody has looked at the page by eye.** Screenshots exist in WSL at `/tmp/claive-shots/{desktop,mobile}-{light,dark}.png` (they may be gone after a reboot). The first job of the next session or the user is to look at the hero and run record.
- **impeccable did not fully run.** Its skill is not installed on WSL (it was only in the cloud session at `/mnt/skills/plugins/impeccable/`, with no network). So no `context` launcher, no `detect` pass, no finish reviewer, no documenter.
- **The one-line descriptions** of Foreman, Claudexor, JevRouter, JevLoop and pi-subagents come from the "Project lesson" notes in `03-research-references.md`. Those projects' own READMEs were not read.
- **No real reader has tested** whether a first-time visitor can say what claive is.

### Open items and decisions

1. Look at the page and send back anything wrong.
2. Confirm the copyright holder name on `LICENSE`.
3. **Public-readiness audit of the repo** (last bullet of section 10) is still not done, on purpose. Nothing was edited outside `site/`.
4. **`site/HANDOFF.md` itself names `/home/hamza/...` paths, the sslip.io gateway, the FMGE/LMS site and the private calibration project.** Delete it or scrub it before the repo goes public. `index.html` and `PRODUCT.md` are clean.
5. Nothing is committed. `docs/experiment/*` is another agent's work in progress; do not stage it together with the site.
6. When the gates settle, "The experiment" can gain thresholds and counts. The page currently says the bars are not published yet; change that sentence then. The footer's "Last updated 2026-10-07" is hard-coded.
7. Ledger row 20 (free-tier models can rate-limit or disappear) is not on the page. It is an honesty point worth adding to "Cheaper agents, same recipe". Rows 16 and 19 are also unused.
8. Optional: publish the file as an Artifact for a private preview link, install impeccable under `~/.claude/skills/` and run `document` and the finish review, or let impeccable's `detect` run once.

### Definition of done (section 12), as of now

| Item | Status |
|---|---|
| Opens on its own, meets the section 6 must-haves | Yes, by measurement (not by eye) |
| First-time visitor can say what it is, how it differs, what it is based on, what the experiment is, why it exists | Written to do so; untested on a real reader |
| Every claim matches a ledger row; nothing from the do-not-claim list | Checked by the build session, with the caveats above |
| MIT `LICENSE` exists before the page says MIT; author credited; repo linked | Yes (holder name unconfirmed) |
| Nothing committed unless the user asks | Yes |

### Browser tooling notes

- Skill: `~/.agents/skills/browser/SKILL.md`. CLI: `wh-browser <fast|jev|devtools> <tool> '<json>'`.
- The default target is Windows Chrome. It reaches WSL servers at `http://localhost:<port>` but not WSL `file://` paths, and its screenshots must be saved inside the Windows temp directory, which could not be found from WSL (`cmd.exe` was not callable).
- Use the Linux target instead. Add `"browser_target":"linux","browser_backend":"clearcote","browser_profile":"x-main"` to **every** call. Run a `fast observe` first so the browser launches; devtools attaches afterwards. Screenshots to `/tmp` work there. `browser_backend:"chrome"` on Linux is broken.
- Serve the site with `python3 -m http.server 8765 --bind 127.0.0.1 --directory /home/hamza/repo/claive/site` (detached) and stop it afterwards.
- Devtools `emulate` takes a viewport such as `375x812x2,mobile,touch` plus `colorScheme`.
- **Closing your tab is fiddly.** `fast execute` never switches tabs: its `tab` must equal the tab the session is currently on, and the browser keeps spawning and dropping `about:blank` tabs, so "current" changes between calls (`TAB_CONTEXT_MISMATCH`). What worked, in one script and gated on the check: `observe` to get `active_tab`, then `execute` with that `tab` and `{"op":"tab_switch","tab":"<my id>"}`, confirm `final_state.active_tab` is your id, then `execute` with `tab` = your id and `{"op":"tab_close"}`. The switch argument is named `tab`, not `value`. Never close a tab you did not open.
- **Stopping the local server:** the job tool prints the wrapper's pid, not Python's, so `kill <that pid>` leaves `http.server` running. Use `pkill -f '[h]ttp.server 8765'` (the brackets stop the pattern matching its own shell) and confirm with `curl` that the port is dead. One stray server from the first session ran until the end of the nav fix.

---

## 1. TL;DR for the next session (written before the build; see section 0 for the current state)

1. Read sections 4 to 6 (what claive is, what was decided, the page plan). Everything is sourced from this repo.
2. The five open questions are answered (section 9): **public and MIT, named author with GitHub link, developers first and juniors second, `site/` as the location, and no gate numbers on the page.**
3. Add the MIT `LICENSE` file first, because the page will say MIT (section 9, item 1). Then check the "unverified" items in section 10. The paper numbers matter most.
4. Run the impeccable skill's workflow (section 11), then build one self-contained `site/index.html`.
5. Do one batched desktop + mobile inspection, one fix batch, stop.

Starter prompt for the new session, started from `/home/hamza/repo/claive`:

> Read `site/HANDOFF.md`. The questions in section 9 are answered. Verify section 10, then build the claive landing page with the impeccable skill. Only touch `site/` (and add the MIT `LICENSE`); another agent is working in this repo.

---

## 2. What this session did and did not do

**Read in full:** all of `docs/experiment/` (README, 00 to 07, `rules.md`, `original-README.md`, `paper-notes.md`, `corpus.example.json`, `calibration/README.md`, `calibration/plan.py`; `calibration/tasks.json` only the first ~3.5 KB), `README.md`, `docs/harness-integration.md`, `docs/pi-engine.md`, `skills/worker-orchestration`, `skills/ttc-experiment`, `skills/claude-subagent-routing`, `.claude/HANDOFF.md`, `systemd/claive-serve.service`. Read only the opening of the 5 specs and 1 plan under `docs/superpowers/`.

**Not read:** `bin/` source (I saw only file sizes and the output of `claive-orch arms`), `tests/`, `install.sh`, `skills/subagent-routing` (the Codex policy, 21 KB), `reference/*`, the rest of `tasks.json`, the spec bodies.

**Ran:** `claive-orch arms` (read-only, output matches the docs). I tried the full test suite through the bridge; the tool call errored and returned nothing, so **I have no verified passing-test count.**

**Wrote:** this file and the empty `site/` folder. No commits, no edits to existing files.

## 3. State of the repo at handoff

- Remote: `https://github.com/ham-zax/claive.git`. 34 commits, 2026-10-03 to 2026-10-07 (started as "Codex Muse workers", renamed claive in `8ad0d0c`). Latest `fbefdab`.
- **Another agent is actively working in this repo** (the user confirmed, 13:58 IST). `docs/experiment/05-experiment-protocol.md` is modified (gate thresholds filled in, dated 2026-10-07) and `docs/experiment/calibration/` is staged. Those numbers are still being tuned. `calibration/README.md` still says the gates "need the user's decision". **Write only inside `site/` (plus the new `LICENSE`), and re-run `git status` before relying on anything in `docs/experiment/`.**
- About 6,700 lines of Python under `bin/` (stdlib only), about 4,300 lines of tests; a grep finds 202 `def test_` (`.claude/HANDOFF.md` recorded 195 passing at an earlier point).
- **LICENSE added in the build session** (MIT, holder "Hamza", unconfirmed). See section 0.
- `~/repo/sites/` exists and is empty. No site convention to follow there.
- Requirements per README: Linux with Python 3. The default engine needs the Muse binary. The dashboard pane needs tmux and Codex.

---

## 4. What claive is, in plain words

**One line:** a referee for AI coding agents.

A "parent" agent (Claude Code, Codex, a Pi agent, or just a shell script) hands a coding task to cheaper "worker" agents. A second worker from a **different model family** critiques the result. The same worker then fixes what the critic found. Throughout, **your tests decide** whether a change is kept. A change that makes the test score worse is thrown away automatically.

Two programs do the work:

- `claive`: manages the workers (open, wait, follow up, cancel, logs, token usage, inbox, batches, missions, an unattended goal queue, a doctor check). Engines: Muse Code (default) and Pi (second, through the `opencode2api` gateway only).
- `claive-orch`: the **arbiter**. It is plain code, not a model. The parent asks `claive-orch next RUN` and does exactly the one action it names. It refuses out-of-sequence commands.

**The ladder** (from `docs/experiment/00-rationale.md` and `01-design.md`):

- **L0 direct:** one worker tries, tests run, pass means done.
- **L1 refine:** critic names concrete defects, the same worker session corrects them, tests run, keep the result only if the score is no worse. Default 2 rounds, hard max 3.
- **L2 breadth:** only if refining stalls, one deliberately different second attempt in an isolated git worktree. The tests pick the winner.

**And it is an experiment.** The question, verbatim from the repo: *for frontier coding agents, does verifier-gated refinement, with selective breadth only when refinement stalls, beat a single run and compute-matched resampling?* Arms A, R, R', B, B0, D are implemented. Arms C and E (which need a "Jev" decision model) are **not**. **There are no experiment results yet.**

---

## 5. Decisions made, and why

| # | Decision | Why |
|---|---|---|
| D1 | **Audience:** developers first, juniors second (the user's answer: "dev for now, but target junior too"). Plain words first, no jargon without a gloss, real commands lower down. Gloss five terms once: parent, worker, critic, verifier, arbiter. Include a short "new to this? start here" path in the Try-it section. | The user asked for the purpose to be clear to a "normal user", then fixed the audience as dev-first with junior reach. |
| D2 | **Impeccable mode: Persuade.** The first viewport states the purpose in one plain sentence. | Landing page. The skill picks mode from the surface, not the product. |
| D3 | **One self-contained `site/index.html`.** Inline CSS and JS, inline SVG, no build step, no framework. | Opens by double-click, publishable as an Artifact for preview, deployable anywhere static. Also keeps the site as small as the project it describes. |
| D4 | **Location: `claive/site/`** (this folder; confirmed by the user). | Next to the sources every claim cites; the next session starts in the repo. |
| D5 | **Every number and rule on the page traces to a file** (ledger, section 7). | The user asked that the content come from the repository. It also stops the page from drifting from the docs. |
| D6 | **Honesty rules** (section 8): no benchmark wins, no "better than X", the experiment has no results yet. | The repo says a null result is acceptable and the shakedown "is not experimental evidence". A page that oversold it would contradict its own project. |
| D7 | **Frame "cheaper agents" as the bet being tested, not a result.** | The user's idea (free, flash, mini agents can do this too, even better, with the prompt and the loop) is a good hook, and the repo gives real support, but not proof. See 5a. |
| D8 | **Visual concept (proposal only):** an instrument printout or lab notebook, built around the project's real artifact, the append-only `events.jsonl`. Signature graphic: the score trajectory of the one real shakedown run (6/7, critic names the defect, 7/7). | It shows the mechanism with real data instead of a generic "AI" look. Impeccable's new-work step makes the final call. |
| D9 | **Public, MIT, named author, GitHub link** (the user's answers). | The page can say MIT and link `github.com/ham-zax/claive`, but only once the repo actually carries a `LICENSE` file and has been checked for things that should not go public (section 10). |
| D10 | **No numeric gate thresholds, and no hard-coded calibration counts, on the page.** Describe the gates in words. | An agent is tuning them right now (the user's answer to question 5). Numbers published today could be wrong by tomorrow. |

### 5a. The "cheaper agents" angle: what the repo supports and what it does not

**Supports (cite these):**
- It is a recipe, not a platform: markdown skills, a stdlib Python CLI, and written specs. Any agent that can run shell commands can be the parent.
- Critics and second attempts run on **free-tier gateway models** through Pi. The preferred critic list is `mimo-v2.6-flash-free`, `big-pickle`, `space-bunny-free`.
- Dogfooding: `stats` and `pick` were built by a Muse worker through claive's own ladder, with the spec and acceptance tests written by the parent (`.claude/HANDOFF.md`: run `66c510f15600` verified 194/194). The specs under `docs/superpowers/specs/` are written exactly this way.
- One live run: a free-tier critic (`nemotron-3-ultra-free`) named the exact defect and the implementer fixed it (`07-trial-runs.md`). One task, no repeats.
- The source paper reportedly found **larger gains for smaller models** and shrinking gains for stronger ones (`paper-notes.md`). Unverified, see section 10.

**Does not support (do not claim):**
- That a cheap agent beats a frontier agent. Nothing measured says so.
- **Local models.** The repo has a loopback override for a locally run `opencode2api` *gateway*, but Pi accepts only that provider, so running local weights is not a documented feature. Say "free-tier and flash-class models", never "local models".
- Cost savings. There is no dollar cost anywhere; compute is measured in tokens and wall-clock only.

Suggested honest hook: *"Give a cheap agent the recipe and a failing test. The loop does the checking."* Then label the claim: "This is the bet the experiment tests."

---

## 6. Page plan and draft copy

Draft copy is a starting point. Adjust the voice in impeccable's init step.

1. **Hero.** H1: "A referee for AI coding agents." Sub: "claive lets one AI hand a coding job to cheaper ones, has a different AI critique the result, and keeps each change only if your tests say it got better." Status chip: "Experimental. No results yet." Two actions: "How it works" (anchor) and "Try it".
2. **The problem.** An agent normally gets one shot. Easy work gets over-served with expensive reasoning; hard work gets one attempt that can lock in a bad approach with nobody to check it. (`00-rationale.md`, "Why this experiment exists".)
3. **How it works.** The ladder as a diagram (inline SVG), L0 to L2, with the revert on a worse score. Real data strip: the shakedown run. Gloss the five terms here.
4. **Why it's different.** Six points, no two alike in shape:
   1. Tests are the referee, not another AI's opinion.
   2. Fix before re-roll: refine an attempt before paying for a second one.
   3. The critic is a different AI family. The arbiter refuses a same-family critic.
   4. Code, not a model, runs the process: one legal next step at a time, regressions reverted with git.
   5. Works with any agent that can run a shell command. Plain CLI, exit codes, file state.
   6. It measures itself: compute-matched baselines, thresholds fixed before data, a null result is an acceptable outcome.
   Bonus row (smaller): unattended goal queue, and self-tuning picks (`stats`, `pick`).
5. **What it's built on.** The paper (*Refining Over Resampling*, Bilal et al., arXiv 2608.05643) and the projects it borrows ideas from: Foreman, Claudexor, JevRouter, JevLoop, plus pi-subagents for the async design. Ideas, not code ("references, not dependencies"). Plain version of each in one line (`03-research-references.md`).
6. **The experiment.** The question; arms in plain words (A one try, R two identical tries and the tests pick, R' two different setups, B fix loop with critic, B0 fix loop without, D fix loop then a different second try only if stuck); the gates **in words only** (a pass bar for each decision, fixed before the first data run), **with no numeric thresholds because they are still being tuned**; the status: a small calibration corpus built from real fix commits, first batch not run, shakedown only. State that a corpus this small supports exploratory conclusions only.
7. **Cheaper agents, same recipe.** Section 5a, with the label "the bet".
8. **Try it.** `./install.sh`, then the README's quick-start prompt, then the `init` / `next` loop in four lines. Requirements stated up front (Linux, Python 3, Muse binary for the default engine). Written so a junior dev can follow it: say what each command does in one line and show the expected first output. Link the repo and state MIT.
9. **What isn't built.** Jev arms C and E, stuck detection inside a turn, a test-author worker for hidden checks, enforced token budgets, Codex / OpenCode / Claude Code as *workers* (they are parents here). From `06-skill-driven-implementation.md`, "Not implemented".
10. **Footer.** Source link (`github.com/ham-zax/claive`), author credit, "MIT licensed", last-updated date.

**Design must-haves:** works at phone width with 16 px gutters and no sideways scroll; light and dark; respects reduced motion; the diagram is readable without JS; real text, not images of text.

---

## 7. Claims ledger

Status: **docs** = stated in the repo docs I read; **code?** = confirm against `bin/claivelib/orchestration.py` first; **verified** = I checked it live; **paper?** = unverified, see section 10.

| # | Claim | Source | Status |
|---|---|---|---|
| 1 | Harness-neutral CLI; any parent that can run shell commands can drive it | `README.md`, `docs/harness-integration.md` | docs |
| 2 | Muse Code default engine; Pi second engine, `opencode2api` only | `README.md`, `docs/pi-engine.md` | docs |
| 3 | `claive-orch next` gives one action; out-of-sequence commands are refused | `06-skill-driven-implementation.md` | docs, code? |
| 4 | Rounds default 2, max 3; lane b max 2; at most 2 candidates; worse round reverted (`reset --hard` + `clean -fd`); equal = no progress; 2 no-progress rounds = stall; tests pick the winner; reviewer only on a tie, once; failing best is reported `stalled`/`unresolved`, never success | `skills/worker-orchestration` ("Rules the arbiter enforces"), `01-design.md` | docs, code? |
| 5 | Critic is read-only, a different family; arbiter refuses same-family | `skills/worker-orchestration`, `04` | docs |
| 6 | Arms A, R, R', B, B0, D implemented; C, E not | `claive-orch arms`, `06` | **verified** |
| 7 | Hypotheses H1 to H7 | `00-rationale.md` | docs |
| 8 | Gates exist and are fixed before the first data run. **Qualitative only: do not publish the numbers.** An agent is tuning them now. | `05-experiment-protocol.md` | docs; numbers withheld by decision (D10) |
| 9 | A small calibration corpus built from real fix commits of the author's own project; base fails and reference passes; first batch `calib1` (arms A and R, 3 repeats) **not run** | `calibration/README.md`, `.claude/HANDOFF.md` | docs (being tuned: say "about ten tasks" or re-read at build time; my difficulty counts of 4 easy, 4 medium, 2 hard are from the README table, recount before use) |
| 10 | Shakedown run `bd886bb32632`: tests 6/7, critic named the defect, same session fixed it, 7/7, 245 s, 668k input tokens (544k cached). One task, no repeats, not evidence | `07-trial-runs.md` | docs |
| 11 | Paper: Bilal et al., *Refining Over Resampling*, arXiv 2608.05643 | `paper-notes.md` | **paper?** |
| 12 | Paper numbers (resampling saturates; critique ablation; gains shrink for stronger models, about 17 points per 10^3 TFLOPs for a 1.5B model vs about 1.16 for 7B) | `paper-notes.md` | **paper?** |
| 13 | Lineage: Foreman, Claudexor, JevRouter, JevLoop, Jev/TypeSafe, pi-subagents; original plan repo `ham-zax/jev-ttc-orchestrator-experiment` rev 2 | `experiment/README.md`, `03`, `specs/2026-10-06-async-workers.md` | docs |
| 14 | Jev not available here, so arms C and E are not built | `04`, `06` | docs |
| 15 | `stats` / `pick`: critic gets 3 scored uses, then best smoothed helpful rate, 20% seeded exploration; refused in experiment runs | `skills/worker-orchestration`, `06` | docs, code? |
| 16 | Goal queue: `goal add`, `serve`, systemd unit; one goal at a time by default; default 2 h budget; backoff 30 s doubling to 30 min | `README.md` | docs |
| 17 | `stats`/`pick` built by a Muse lane through the ladder, 194/194 | `.claude/HANDOFF.md` | docs |
| 18 | Compute measured in tokens and wall-clock; no dollar cost | `04`, `05` | docs |
| 19 | Pi has no OS sandbox; Muse runs in a sandbox | `04`, `docs/pi-engine.md` | docs |
| 20 | Free-tier gateway models can rate-limit or disappear | `04` | docs |

## 8. Do-not-claim list

- Any result, win rate or benchmark. There are none.
- "Better than <named product>". The repo compares nothing to a competitor, only to its own baselines.
- Local models, offline use, Windows or macOS support, cost savings, "production ready", "sandboxed".
- A license other than MIT. **Do not say "MIT" on the page until the `LICENSE` file is in the repo.**
- That Jev-based control exists. It does not.
- Numeric gate thresholds or exact calibration-task counts (D10).
- Personal paths (`/home/hamza/...`), the FMGE/LMS site, the author's email address, or the name of the private calibration project. Say "the author's own project".
- Quote the paper's numbers only after they are verified. Until then, attribute them to "the repo's paper notes".

---

## 9. Questions and the user's answers (answered 2026-10-07, 13:58 IST)

1. **Public or private?** **Answer: public, MIT.** The site and the repo are public under the MIT license. **To do first:** add a `LICENSE` file with the MIT text to the repo root before the page says "MIT". The copyright holder is not yet decided: use the name the user wants shown (probably "Hamza"); ask if unsure. Also run the public-readiness check in section 10.
2. **Author and branding.** **Answer: yes to both.** Credit the author and link `https://github.com/ham-zax/claive`. The repo says "Hamza" and the GitHub handle is `ham-zax`; use those, do not invent a surname, and keep the email address off the page.
3. **Who is the "normal user"?** **Answer: developers for now, but aim to reach juniors too.** See D1.
4. **Location.** **Answer: yes, `claive/site/`.**
5. **Are the experiment gates final?** **Answer: no.** An agent is actively working on and tuning them, so they are for later. Do not show threshold numbers and do not hard-code calibration counts. Describe the gates in words only (D10). A later pass can add numbers once the experiment settles.

## 10. Unverified items and gotchas

- **The paper.** A fetch of `arxiv.org/abs/2608.05643` returned no readable content. I could not confirm the title, authors or numbers. `paper-notes.md` itself says to check the PDF. Do a web search or fetch the PDF before the site states any number.
- **Test count.** The suite run through the bridge failed with a tool error. Run `python3 -m unittest discover -s tests -p 'test_*.py'` directly on the machine before putting a number on the page, or leave the number out.
- **Rules in code.** Ledger rows marked `code?` come from docs, not from reading `orchestration.py`. Skim `decide()` and `lane_status()` first.
- **This session's reach.** The cloud workspace has no copy of the repo; everything went through the `hamza-wsl` bridge. Its read tool pages at 16 KiB (use `offset`), and `list_repos` failed because no GitHub account is linked here. The remote is `ham-zax/claive`, so `add_repo` could attach it if needed.
- **Screenshots.** The cloud workspace has Node 22 and the Python `playwright` module, but I found no Chromium binary and did not try installing one. The user's plan does not include browser control. Check what is available on the machine you are on before promising screenshots.
- **Public-readiness of the repo (new, because it is going public).** I have not audited this. From what I read, check these before publishing: the gateway URL (an IP-based `sslip.io` address) recorded in `docs/pi-engine.md`; `reference/global-AGENTS.md` (a snapshot of the user's global instructions, which I did not read); absolute `/home/hamza/...` paths in the skills and README; the production site named in `skills/ttc-experiment` and `05`; and the private calibration project named in `calibration/tasks.json`. Flag these to the user; do not edit them, because another agent is working there. The site itself must still keep personal paths and that project name out.

## 11. Impeccable workflow for the build

From the skill's own setup:

1. Run its `scripts/impeccable context` once, with the project directory as the working directory. A missing `PRODUCT.md` routes a new surface through **init**, then **new-work**.
2. Init should record: audience (D1), voice (plain, dry, honest about being an experiment), and anti-references (generic AI-tool look: purple-blue gradients, glass cards, emoji icons, fake typing terminals, identical feature-card grids).
3. Read `reference/new-work.md`, choose the visual world (D8 is a proposal), then read `reference/craft-floor.md` immediately before the first edit.
4. Build `site/index.html` from the page plan and ledger.
5. **Verify in bounded passes:** one batched inspection (desktop and mobile together), fix everything in one batch, at most one confirming round, then stop.
6. Before delivering, re-check every number on the page against the ledger.

## 12. Definition of done

- `site/index.html` opens on its own and meets the must-haves in section 6.
- A first-time visitor can say what claive is, how it differs, what it is based on, what the experiment is, and why it exists, without reading anything else.
- Every claim matches a ledger row; nothing from the do-not-claim list appears.
- The repo has an MIT `LICENSE` file before the page says MIT, and the page credits the author and links the repo.
- Optional: publish the same file as an Artifact for a private preview link.
- Nothing is committed unless the user asks.
