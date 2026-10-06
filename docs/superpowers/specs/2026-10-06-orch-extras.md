# claive-orch: post-pass critic, integrate, branch cleanup, lane setup

All changes go in `bin/claivelib/orchestration.py` (stdlib only; match its style).
Acceptance tests: `tests/test_orch_extras.py`. `tests/test_orch.py` must keep
passing. Do not edit either test file.

## 1. Post-pass critic (`init --post-pass-critic`)

- New flag stored as `config["post_pass_critic"]` (bool). Allowed only with arms
  `B` and `D`; for any other arm `init` fails (ValueError mentioning
  `--post-pass-critic`) before creating anything.
- `decide`: when a lane's status is `passed` and the flag is set:
  - no critique with `post_pass: true` recorded for that lane yet →
    `action("critique", ..., lane=name, round=lane_rounds(lane) + 1, post_pass=True)`;
  - the post-pass critique named defects, the correction has not been verified
    yet (`lane_rounds(lane) < critique["round"]`) and the round limit is not
    used up (`lane_rounds(lane) < limit`) →
    `action("correct", ..., lane=name, round=critique["round"], post_pass=True)`;
  - otherwise finish as today. Only one post-pass critique per lane, ever.
  Without the flag behaviour is unchanged. Note `lane_status` returns `passed`
  before looking at critiques; a post-pass correction that makes things worse is
  reverted by `verify` as today, so the lane stays passed.
- `command_critique` records `post_pass: bool(expected.get("post_pass"))` in the
  `critique.completed` event. Non-post-pass critiques keep working as today.
- The critique prompt, when the arbiter's next action is a post-pass critique,
  starts with `# Post-pass review` and says the verifier passes and the critic
  must look for behaviour changes beyond the task (regressions, changed public
  behaviour, swallowed errors, wrong counts on error paths, untested paths). It
  includes the task, the diff and the verifier output, and the same
  `CRITIC_SCHEMA`. The correct prompt is unchanged (it already lists the
  defects of the critique for that round).
- `GUIDE`/`next` need no new action names.

## 2. `claive-orch integrate RUN [--no-verify]`

- Refuse (ValueError) if the run is not finished (`not finished`), has no
  winning commit, or its outcome is not `verified` (`not verified`), or if it
  was already integrated (`already integrated`).
- Apply `git diff <base> <commit>` to the repo checkout **without staging**:
  try `git apply` (working tree only) first. If that fails, try
  `git apply --3way`; afterwards unstage only the paths in the diff that applied
  cleanly (`git reset -q -- <paths>`) so the user's own staged changes stay
  staged. If paths conflict, print `Conflict: <path>` lines, record nothing and
  exit 1. Never commit.
- Then (unless `--no-verify`) run the configured verifier in the repo checkout
  with `run_verifier` (output file `verify/integrate.txt` in the run dir) and
  print `Verifier in <repo>: <describe(result)>`.
- Append event `run.integrated` (`commit`, `result` or null). `summarize`
  (and so `report --json`) gets `"integrated": true|false`.
- Close the run's reusable workers that are idle: for every registered worker
  whose claive state has `reusable: true` and status `idle`, run
  `claive close ID` (the `claive` next to `claive-orch` in the same bin
  directory). Print what was closed; failures are warnings, never errors.
- Exit 0 when applied and the verifier passed (or was skipped), 1 when the
  verifier fails in the checkout (the applied change stays in place).

## 3. Branch cleanup

- `claive-orch cleanup RUN --branches [--force]`: after removing worktrees as
  today, delete the run's lane branches with `git branch -D`, printing
  `Deleted branch <name>`. The winning lane's branch is kept unless the run was
  integrated or `--force` is given: print `Kept <branch> (winner not integrated;
  use --force)`. Non-winning branches are always deleted.
- `claive-orch prune --repo REPO [--apply]`: for every local branch
  `orch/<run>/<lane>` in REPO:
  - run unknown in this state root → `Kept <branch> (unknown run)`;
  - run not finished → `Kept <branch> (run not finished)`;
  - lane worktree still exists → `Kept <branch> (worktree exists; run cleanup)`;
  - winner of a non-integrated run → `Kept <branch> (winner not integrated)`;
  - otherwise deletable: `Would delete <branch>` (default, dry run) or, with
    `--apply`, delete it and print `Deleted branch <branch>`.

## 4. Lane setup (`init --setup CMD`)

- Stored as `config["setup"]`. Run with `bash -c` (cwd = the worktree,
  stdin DEVNULL, timeout `verify_timeout`) and env `CLAIVE_ORCH_REPO=<repo>`
  `CLAIVE_ORCH_LANE=<worktree>`:
  - in the temporary base worktree during `init`, before the base
    verification; a failure raises ValueError containing `setup` and the tail
    of its output (worktree removed as today);
  - in each new lane worktree in `command_lane`, after `worktree add` and
    before `lane.added` is appended. On failure remove the worktree and delete
    the branch, then raise ValueError containing `setup` and the output tail
    (so the lane can be added again later).
- Typical use: `--setup 'ln -s "$CLAIVE_ORCH_REPO/node_modules" node_modules'`.
- Setup files are part of the worktree; tell users to keep them gitignored
  (document in the `--setup` help text).

## Also

- Update `--help` texts and `GUIDE["finish"]` to mention
  `claive-orch integrate {run}` and `claive-orch cleanup {run} --branches`.
