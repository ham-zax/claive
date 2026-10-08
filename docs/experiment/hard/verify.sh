#!/usr/bin/env bash
# verify.sh ID [--integration] [--visible]: the hard corpus verifier, run from a lane workspace (or task repo).
# Only the workspace's sqlglot/ package counts: it is copied into a pristine copy of the task's base
# (the fix's parent: tests, fixtures and config), the fix's hidden test changes (hidden/ID: files/ copied
# over, the paths in deleted removed) are applied, and the whole sqlglot suite runs there. Test or fixture
# edits in the workspace have no effect, and a fix that breaks another dialect or rule fails.
# --visible skips the hidden tests: the regression check workers may run themselves.
# --integration also runs the integration tests (the executor's TPC-H/TPC-DS runs against DuckDB).
# At most VERIFY_SLOTS suites (default 3) run at once on this machine; the rest wait for a slot.
# Process pools get PYTHON_CPU_COUNT workers (default 4, not one per CPU): the forked workers share pages with
# the parent, so per-process RSS summed over the session (claive-memcap) would count them many times over.
set -euo pipefail
usage="usage: verify.sh ID [--integration] [--visible]"
id=${1:?$usage}
shift
skip=1 hidden=1 label=
for a in "$@"; do
  case $a in
    --integration) skip=0 ;;
    --visible) hidden=0 label=" (visible tests only)" ;;
    *) echo "$usage" >&2; exit 2 ;;
  esac
done
root=${CORPUS_ROOT:-$HOME/corpus/hard}
base=$root/tasks/$id
ws=$PWD
[ -d "$base/.git" ] || { echo "verify: unknown task $id" >&2; exit 2; }
[ -f "$ws/sqlglot/__init__.py" ] || { echo "verify: no sqlglot/ package in $ws" >&2; exit 2; }

slots=${VERIFY_SLOTS:-3}
while :; do
  for ((i = 0; i < slots; i++)); do
    exec 9>"$root/.verify.$i.lock"
    flock -n 9 && break 2
  done
  sleep 3
done

t=$(mktemp -d "${TMPDIR:-/tmp}/sgverify.XXXXXX")
trap 'rm -rf "$t"' EXIT
trap 'exit 143' TERM INT
git -C "$base" archive HEAD | tar -x -C "$t"
if [ "$hidden" = 1 ]; then
  h=$root/hidden/$id
  [ -d "$h/files" ] || { echo "verify: no hidden tests for $id" >&2; exit 2; }
  cp -a "$h/files/." "$t/"
  if [ -s "$h/deleted" ]; then
    while IFS= read -r f; do rm -f "$t/$f"; done <"$h/deleted"
  fi
fi
rm -rf "$t/sqlglot"
tar -C "$ws" --exclude=__pycache__ --exclude='*.pyc' -cf - sqlglot | tar -C "$t" -xf -
rc=0
(cd "$t" && SKIP_INTEGRATION=$skip PYTHONDONTWRITEBYTECODE=1 PYTHON_CPU_COUNT=${PYTHON_CPU_COUNT:-4} PATH="$root/venv/bin:$PATH" \
  python -m unittest >"$t/.out" 2>&1) || rc=$?
fails=$(grep -cE '^(FAIL|ERROR): ' "$t/.out" || true)
if [ "$rc" -ne 0 ]; then
  echo "verify $id$label: FAILED ($fails failing tests; first 50 below, then the end of the log)"
  grep -E '^(FAIL|ERROR): ' "$t/.out" | head -50
  echo "..."
  tail -n 60 "$t/.out"
else
  echo "verify $id$label: OK"
  tail -n 3 "$t/.out"
fi
exit "$rc"
