# Hard corpus (20 sqlglot tasks)

The second corpus for the experiment in `../05-experiment-protocol.md`, built because the calibration
corpus was too easy (Gate 1 failed: arm A passed 83% and then 100%). Twenty real upstream changes to
[sqlglot](https://github.com/tobymao/sqlglot), a SQL parser, transpiler, optimizer and engine with about
1,400 tests. All changes are from April to October 2026, each changes at least 70 lines of source, and
together they cover optimizer rules, scoping, type annotation, lineage, the Python executor, parsing
and dialect semantics.

| Task | Area | Category |
|---|---|---|
| sg-canonical-names | optimizer: new rule | feature |
| sg-pivot-alias-star | optimizer: star expansion, types | bug-fix |
| sg-data-identifiers | optimizer: identifier normalization | bug-fix |
| sg-groupby-literal-merge | optimizer: merge_subqueries | bug-fix |
| sg-decorrelate-nonequal | optimizer: unnest_subqueries | bug-fix |
| sg-dml-scope | optimizer: scope | bug-fix |
| sg-multi-pivot | optimizer: pivot chains | bug-fix |
| sg-annotate-rounding | optimizer: annotate_types | bug-fix |
| sg-merge-blowup | optimizer: merge_subqueries | bug-fix |
| sg-lineage-all-columns | lineage | feature |
| sg-lineage-pivot-chain | lineage | bug-fix |
| sg-exec-subqueries | executor (integration tests) | feature |
| sg-exec-outer-joins | executor (integration tests) | bug-fix |
| sg-snowflake-positional | optimizer: qualify, Snowflake | bug-fix |
| sg-presto-datediff | dialects: BigQuery, Snowflake, MySQL to Presto | bug-fix |
| sg-setop-pagination | parser and generator: set operations | bug-fix |
| sg-bigquery-week-units | dialects: BigQuery date parts | bug-fix |
| sg-bigquery-escapes | dialects: BigQuery literals | bug-fix |
| sg-int-division | dialects and executor (integration tests) | bug-fix |
| sg-anonymizer | new module | feature |

This is corpus v2, with hidden tests. In v1 the fix's tests were applied to the worker's checkout and listed
in the statement, and Haiku 5.5 passed 5 of the first 5 runs: a failing test that names the expected output
is most of the solution. In v2 the worker gets the fix's parent and an issue-style statement in `tasks.json`.
Each statement fully specifies the change (API names, rules, and one or more exact input and output
examples, all checked against the reference), so that a correct implementation of the statement passes the
hidden tests. `build.sh` appends a note: change `sqlglot/` only, acceptance is by hidden tests plus the whole
suite, failures are shown in the next round, and the visible check command. Never show a worker the commit,
the reference, `_ref/` or `hidden/`.

## Build

```bash
git clone https://github.com/tobymao/sqlglot ~/corpus/sqlglot          # mining clone (full history)
uv venv --python 3.13 ~/corpus/hard/venv
uv pip install --python ~/corpus/hard/venv/bin/python duckdb pandas python-dateutil pytz typing_extensions
JOBS=3 ./build.sh     # about 25 minutes: 60 whole-suite runs
```

The venv has no sqlglot: tests import the checkout's own `sqlglot/` from the working directory.

- `~/corpus/hard/tasks/<id>` is a one-commit repo: the fix's parent, as is. There is no fix history.
- `~/corpus/hard/hidden/<id>` holds the fix's changes under `tests/`: `files/` (added and modified files)
  and `deleted` (removed paths). Only the verifier applies them.
- `~/corpus/hard/_ref/<id>` has the same base commit plus the reference tree. It is used only for validation.
- Hashes are deterministic (fixed author and dates). Re-run `build.sh` after changing `tasks.json`.

## Verifier

`bin/verify.sh ID [--integration] [--visible]` runs from the lane workspace. It copies the task's pristine
base, applies the hidden tests (unless `--visible`), replaces its `sqlglot/` with the workspace's, and runs
the whole suite (`python -m unittest`, with
`SKIP_INTEGRATION=1` unless `--integration` is given). So:

- the manifest's `verify` is the hidden check that decides acceptance; `worker_verify` (the same with
  `--visible`) is the only command shown to workers, as a regression check, and it passes at base;
- editing tests or fixtures has no effect, without needing `acceptance_dir` (which cannot handle a fix
  whose tests modify existing files);
- a fix that breaks another dialect or rule fails;
- integration tests run for the three tasks whose fix touches the executor (TPC-H and TPC-DS against
  DuckDB, about 35 s more).

Validation runs the verifier three times per task: the hidden suite must fail at base and pass at the
reference, and the visible suite (`--visible`) must pass at base.

One run takes about 40 s, or 75 s with integration, and peaks near 1.9 GB. At most `VERIFY_SLOTS`
runs (default 3) run at once machine-wide; the others wait for a slot. The suite's process pools
(`ProcessPoolExecutor()` in the optimizer and executor tests) get `PYTHON_CPU_COUNT` workers, default 4,
rather than one per CPU. Forked workers share the parent's pages, so with 16 of them `claive-memcap`'s
per-process RSS sum read 5.3 GB for a run whose real footprint (PSS) was 1.8 GB, and the 3G cap
killed every run.

## Contamination

The fixes and their tests are public and exist on this machine: the mining clone, `_ref/`, `hidden/`, and any
newer sqlglot installed elsewhere. A worker that runs the verifier without `--visible` also sees hidden
test failures. For example, `~/repo/AVO/frappe-bench` has sqlglot 30.16.0, which already contains
10 of the 20 fixes. Workers are not sandboxed from them. Experiment x3 therefore audits every worker's
tool calls after the run (`~/corpus/x3/audit.sh`), including reads of `hidden/` and verifier runs without
`--visible`, and reports flagged runs in `FINAL.md`.
