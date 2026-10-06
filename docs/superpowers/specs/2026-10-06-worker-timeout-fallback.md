# claive: per-turn timeout, fallback models, failure reason in list

Changes go in `bin/claivelib/cli.py` (and `bin/claivelib/engine*.py` only if a
helper is needed). Stdlib only; match the existing style. Acceptance tests:
`tests/test_worker_fallback.py`; `tests/test_workers.py`, `tests/test_pi_engine.py`
and every other existing test must keep passing. Do not edit any test or
fixture file (`tests/fixture_engine.py` and `tests/fixture_worker.py` already
support `--model` and `FIXTURE_MODEL_MODES`).

## 1. `--turn-timeout SECONDS`

- Accepted by `start`, `run` and `open` (all launch actions). Must be a
  positive number (int or float); otherwise ValueError before anything is
  created. Stored as `state["turn_timeout"]`; applies to every turn of the
  worker (follow-ups of a reusable worker included).
- In `supervise`, when a turn has run longer than the timeout, stop the child
  exactly like the cancel path (SIGTERM to the process group, SIGKILL after
  3 s), but the result is `status = "failed"`, `failure_kind = "timeout"`,
  `error = "turn timed out after <N>s"` (N as given, e.g. `1` or `1.5`), not
  `cancelled`. `wait` exits non-zero like any failure.
- `show` prints the failure kind and error as it does today.

## 2. `--fallback-models m1,m2,...`

- Accepted by `start` and `run` only (single-turn workers). With `open`, fail
  with ValueError mentioning `--fallback-models`. Comma-separated, blanks
  ignored; every model goes through `check_model_policy` at launch (so a
  disallowed model such as `nemotron-*` is refused before any worker is
  created). Stored as `state["fallback_models"]` (list, in order).
- When a turn ends with `status = "failed"` and a failure kind of `timeout`,
  `quota`, `worker` or `protocol` (not when cancelled, not for `supervisor` or
  `launch`), and a fallback model remains: relaunch in the same supervisor with
  the next model:
  - append `{"model": <model that failed>, "failure_kind": ..., "error": ...}`
    to `state["fallbacks"]`;
  - rebuild the launch and command for the new model with the same engine,
    provider, workspace, prompt file, effort, steps, read-only and other
    options, and a fresh session id (`engine.resolve_session_id(None, ...)`);
    update `state["model"]`, `state["launch"]`, `state["command"]`,
    `state["session_id"]`;
  - reset per-turn fields (terminal, error, failure_kind, quota fields, answer,
    report fields, malformed count) as `supervise` does at start, but keep
    `steps`/`task_failures` accumulating;
  - append to `events.jsonl` and `stderr.log` instead of truncating them;
  - the turn timeout restarts for each attempt.
- If every model fails, the worker ends failed with the last attempt's failure;
  `state["fallbacks"]` holds every attempt except the last.
- `show` prints one line per fallback:
  `Fallback from <model>: <failure_kind> (<error>)`; `--json` includes the
  fields as stored.
- `usage` / session usage keeps working for the final model's session.

## 3. Failure reason in `list`

The list label for a worker with task failures becomes
`[!N task failures: <last reason>]`, the last entry of
`state["task_failure_reasons"]` cut to 60 characters (with `...` when cut).
Without recorded reasons it stays `[!N task failures]`.

## Also

- `--help` text for both new flags.
- `claive list --json` and other JSON outputs keep all existing keys.
