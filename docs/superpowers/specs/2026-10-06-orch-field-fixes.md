# claive-orch: fixes from field use (setup leaks, rescore, reject, integrate paths, tokens, cache)

All changes go in `bin/claivelib/orchestration.py` (stdlib only; match its style).
Acceptance tests: `tests/test_orch_more.py`. `tests/test_orch.py` and
`tests/test_orch_extras.py` must keep passing. Do not edit any test file.

## 1. Setup files never enter checkpoints

`--setup` often creates untracked files (for example a `node_modules` symlink).
Today `verify` runs `git add -A`, so they get committed into checkpoints, and a
revert's `git clean -fdq` deletes them.

- In `command_lane`, list untracked files before and after the setup command
  with `git ls-files --others --exclude-standard --directory --no-empty-directory`
  (in the lane worktree). The new entries are the lane's **local paths**. Store
  them in the `lane.added` event as `local_paths` (list of repo-relative strings;
  a trailing `/` from `--directory` may be stripped).
- Add the configured cache paths (section 6) to the local paths too.
- Everywhere a lane worktree is staged, cleaned or checked for dirt, skip the
  local paths:
  - `command_verify` checkpoint: stage with
    `git add -A -- . ':(exclude,top)<p>' ...` (one exclude per local path), so
    they are never committed. A file a worker changes inside a local path is
    also not committed (documented limitation).
  - revert: `git clean -fdq -e /<p> ...` so local paths survive.
  - `command_cleanup` dirty check and the clean-worktree check in `rescore`
    (section 2): ignore porcelain lines whose path is a local path or inside one.
- Do **not** write to `info/exclude` (it is shared with the user's checkout).

## 2. `claive-orch rescore RUN --reason TEXT`

Re-verify without using a round, after the parent fixed the verifier or a
held-out check (the acceptance dir is read live, so editing it is enough).

- `--reason` is required (non-empty). Refuse if the run is finished.
- For every lane that has at least one verification: refuse (ValueError
  mentioning `uncommitted`) if the lane worktree has changes outside local
  paths, or if an implementer of the lane is still running. Then run
  `run_verifier` on the worktree (it is at the lane's best checkpoint, because
  verify either checkpointed or reverted) with output
  `verify/<lane>-rescore-<n>.txt`.
- Append `verification.rescored` with `lane`, `reason`, `result` (with the
  `round` of the replaced verification). `fold`: replace the lane's last
  verification with the new result, set `lane["best"] = result`, and drop
  critiques whose round is greater than `lane_rounds(lane)` (critiques of a
  correction that has not happened yet were made against the old verifier).
  `no_progress` and round counts do not change.
- Print `Lane <x> rescored: <old describe> -> <new describe>` per lane.
- Afterwards `next` decides from the new scores (a lane that now passes
  finishes; a still-failing lane asks for a new critique).

## 3. `claive-orch reject RUN LANE --defect N [--defect N ...] --reason TEXT`

The parent overrides a critic suggestion before the correction prompt.

- Allowed only while the lane has a recorded critique for round
  `lane_rounds(lane) + 1` (i.e. the correction is pending; post-pass critiques
  included). Otherwise ValueError mentioning `no pending critique`.
- `N` is 1-based in the critique's original defect list; an out-of-range
  number or an empty `--reason` is a ValueError. Rejecting an already rejected
  defect is harmless.
- Append `critique.rejected` (`lane`, `round`, `defects`, `reason`). `fold`
  (applied to the latest critique of that lane and round): remove the rejected
  defects from `defects`, keep them in `critique["rejected"]` as
  `{"index", "description", "reason"}`. If no defects remain, set
  `no_concrete_defect = True` (the arbiter then treats it like a clean critique:
  a failing lane stalls, a post-pass review finishes).
- The correction prompt lists only the remaining defects and adds a section
  `## Rejected suggestions` naming each rejected one with the reason and "Do not
  apply this."
- Both critique prompts (normal and post-pass) add: "Treat the task's
  constraints as binding: never propose relaxing, bypassing or weakening a
  check, gate, validation or test to make verification pass."

## 4. `integrate --paths P ... / --exclude P ...`

- `--paths` limits the applied diff to those pathspecs; `--exclude` drops them
  (`:(exclude)P`). Both may be given. They apply to every `git diff` in
  integrate (the patch, the `--name-only` lists). Paths are repo-relative.
- Print `Applied: <path>` for each applied file. If the filtered diff is empty,
  fail with ValueError `nothing to integrate`.
- Record `paths`/`exclude` in the `run.integrated` event.

## 5. Report tokens

- `report` (text and `--json`): when the run is finished and no
  `usage.collected` event exists yet, collect usage first (same as
  `claive-orch usage`; refactor into a helper), then summarize.
- `summarize`: `tokens` sums the totals of every worker that has them; it is
  `None` only when no worker has totals. New key `tokens_unknown_workers`: the
  number of workers with an error or no totals. Text report:
  `tokens: in X, out Y, cached Z (N worker(s) unknown)`; when `tokens` is None:
  `tokens: unknown (N worker(s) without usage)` or `tokens: not collected`.

## 6. Verifier build cache

- `init --cache-key CMD --cache-path P [--cache-path P ...]` (both or neither;
  paths repo-relative, no `..`). Stored as `config["cache_key"]`,
  `config["cache_paths"]`.
- Cache root: `runs_root().parent / "orch-cache" / <sha256 of the repo path>[:16]`.
  Exported to setup, verifier and cache-key commands as `CLAIVE_ORCH_CACHE`
  (also when no cache key is configured, so scripts can cache by themselves);
  setup and verifier also get `CLAIVE_ORCH_REPO`.
- Around every `run_verifier` call in a lane or the base check (not integrate):
  run CMD (`bash -c`, cwd = worktree, timeout 120 s); key = sha256 of its
  stripped stdout. If CMD fails, skip caching for this verify and append a
  warning event. If `<root>/<key>` exists, copy each cached path into the
  worktree (replace what is there; keep symlinks as symlinks) before the
  verifier. After a verifier that did not time out, if `<root>/<key>` did not
  exist, copy the existing cache paths into `<root>/<key>.tmp-<pid>` and rename
  it to `<root>/<key>` (atomic; ignore a lost race).
- Print `Cache hit <key[:12]>` / `Cache stored <key[:12]>` in verify output.
- Help text: the verifier should skip its build when the cached outputs exist.

## 7. Critic fallback models (worker manager side is a separate change)

`claive start` gets `--fallback-models a,b` and records
`state["fallback_models"]` (list) and, after switching, `state["model"]` = the
model that actually ran plus `state["fallbacks"]` = list of
`{"model", "failure_kind", "error"}`.

- `command_worker` for critic/reviewer roles: also check the family of every
  model in `record.get("fallback_models", [])`; a fallback in the lane's family
  is refused like the primary model (same `--allow-same-family` override).
- `command_critique` already reads the model from the record; also record
  `fallbacks` from the record in the critique event when present.

## Also

- `--help` texts for every new flag and command; `GUIDE` unchanged unless a
  message names a command that changed.
