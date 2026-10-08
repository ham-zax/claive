#!/usr/bin/env bash
# Build the hard corpus from tasks.json: real sqlglot fixes (needs a sqlglot clone and the venv: see README.md).
#   tasks/<id>   single-commit repo: the fix's parent, as is. No fix history, no new tests.
#   hidden/<id>  the fix's test changes, applied only by the verifier: files/ (added and modified files)
#                and deleted (removed paths). Never shown to workers.
#   _ref/<id>    same base commit plus the reference commit (validation only; never shown to workers)
#   bin/verify.sh   the verifier (whole suite over a pristine base + hidden tests; only the workspace's sqlglot/ counts)
#   calibration.json   manifest for claive-orch (same shape as the calibration corpus, plus worker_verify)
# Each task is validated: the verifier must fail at base and pass at the reference, and the visible suite
# (verify.sh --visible, what workers may run) must pass at base. Tasks whose fix touches the executor
# also run the integration tests. JOBS validations run at once (default 3).
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
root=${CORPUS_ROOT:-$HOME/corpus/hard}
src=${SQLGLOT_SRC:-$HOME/corpus/sqlglot}
jobs=${JOBS:-3}
export GIT_AUTHOR_NAME=calibration GIT_AUTHOR_EMAIL=calibration@local GIT_COMMITTER_NAME=calibration GIT_COMMITTER_EMAIL=calibration@local
export GIT_AUTHOR_DATE=2026-01-01T00:00:00Z GIT_COMMITTER_DATE=2026-01-01T00:00:00Z
[ -x "$root/venv/bin/python" ] || { echo "missing $root/venv (see README.md)"; exit 1; }
mkdir -p "$root/tasks" "$root/_ref" "$root/hidden" "$root/bin"
install -m 755 "$here/verify.sh" "$root/bin/verify.sh"
scratch=$(mktemp -d "$root/.build.XXXX")
trap 'rm -rf "$scratch"' EXIT

# 1. repos
jq -c '.[]' "$here/tasks.json" | while read -r row; do
  id=$(jq -r .id <<<"$row"); short=$(jq -r .commit <<<"$row")
  full=$(git -C "$src" rev-parse "$short^{commit}")
  t=$root/tasks/$id r=$root/_ref/$id h=$root/hidden/$id
  rm -rf "$t" "$r" "$h"; mkdir -p "$t" "$h/files"; : >"$h/deleted"
  git -C "$src" archive "$full^" | tar -x -C "$t"
  git -C "$src" diff --name-status --no-renames "$full^" "$full" -- tests | while IFS=$'\t' read -r st f; do
    if [ "$st" = D ]; then echo "$f" >>"$h/deleted"
    else mkdir -p "$h/files/$(dirname "$f")"; git -C "$src" show "$full:$f" >"$h/files/$f"; fi
  done
  git -C "$t" init -q -b main && git -C "$t" add -Af && git -C "$t" commit -q -m "hard base: $id"
  git clone -q "$t" "$r" && git -C "$r" remote remove origin
  git -C "$r" rm -rqf . && git -C "$src" archive "$full" | tar -x -C "$r"
  git -C "$r" add -Af && git -C "$r" commit -q -m "hard reference: $id"
  flag=
  git -C "$src" diff --name-only "$full^" "$full" | grep -qE '^(sqlglot/executor/|tests/test_executor\.py$)' && flag=--integration
  echo "$id${flag:+ $flag}" >>"$scratch/list"   # no trailing blank: xargs -L would join the next line
done

# 2. validation, with time and peak RSS: the hidden suite at base (must fail) and at reference (must pass),
#    and the visible suite at base (must pass: workers start from a green regression check)
validate() {
  local id=$1 flag=$2 kind
  for kind in tasks _ref visible; do
    local dir=$root/$kind/$id vis=
    [ "$kind" = visible ] && dir=$root/tasks/$id vis=--visible
    (cd "$dir" && /usr/bin/time -f '%e %M' -o "$scratch/$id.$kind.time" \
      bash "$root/bin/verify.sh" "$id" ${flag:+"$flag"} ${vis:+"$vis"} >"$scratch/$id.$kind.log" 2>&1) || true
  done
}
export -f validate; export root scratch
# shellcheck disable=SC2016  # expanded by the inner bash
xargs -P "$jobs" -L1 bash -c 'validate "$1" "${2:-}"' _ <"$scratch/list"

# 3. manifest
jq -c '.[]' "$here/tasks.json" | while read -r row; do
  id=$(jq -r .id <<<"$row")
  flag=$(awk -v id="$id" '$1 == id {print $2}' "$scratch/list")
  verify="bash $root/bin/verify.sh $id${flag:+ $flag}"
  wverify="$verify --visible"
  bok=$(head -1 "$scratch/$id.tasks.log"); rok=$(head -1 "$scratch/$id._ref.log"); vok=$(head -1 "$scratch/$id.visible.log")
  read -r bsec bkb < <(tail -1 "$scratch/$id.tasks.time") || true
  read -r rsec rkb < <(tail -1 "$scratch/$id._ref.time") || true
  # "FAIL: test_x (tests.mod.Class.test_x)"; a test module that cannot import shows as unittest.loader._FailedTest.<module>
  mapfile -t failing < <(sed -nE 's/^(FAIL|ERROR): [^ ]+ \(([^)]+)\).*/\2/p' "$scratch/$id.tasks.log" |
    sed -E 's/^unittest\.loader\._FailedTest\.//' | sort -u)
  if [[ $bok == *FAILED* && $rok == *": OK" && $vok == *": OK" ]]; then verdict=OK; else verdict=INVALID; echo "$id" >>"$scratch/invalid"; fi
  printf '%-26s %-14s base: %3d failing %5ss %4d MB | ref: %5ss %4d MB | visible: %-4s  %s\n' "$id" "${flag:-}" "${#failing[@]}" \
    "${bsec:-?}" "$(( ${bkb:-0} / 1024 ))" "${rsec:-?}" "$(( ${rkb:-0} / 1024 ))" "$([[ $vok == *": OK" ]] && echo OK || echo FAIL)" "$verdict"
  skip=1; [ -n "$flag" ] && skip=0
  note="Your checkout is sqlglot at the commit just before the upstream change. Implement the change by editing code under sqlglot/ only; you may add or change tests for your own use, but they are not used."
  note+=" Acceptance is decided by hidden tests that check the exact behaviour and output described above, plus the whole existing suite (a change that breaks another dialect or rule fails). If the hidden tests fail, their failures are shown to you in the next round."
  note+=" Run tests with the corpus venv, e.g. PATH=$root/venv/bin:\$PATH SKIP_INTEGRATION=$skip python -m unittest tests.test_optimizer (seconds per module). The whole visible suite takes one to two minutes; run it with a long command timeout: $wverify"
  base=$(git -C "$root/tasks/$id" rev-parse HEAD); ref=$(git -C "$root/_ref/$id" rev-parse HEAD)
  jq -c --arg repo "$root/tasks/$id" --arg refrepo "$root/_ref/$id" --arg base "$base" --arg ref "$ref" \
    --arg verify "$verify" --arg wverify "$wverify" --arg note "$note" \
    '{id, repo: $repo, base: $base, reference: $ref, reference_repo: $refrepo, task: (.task + "\n\n" + $note),
      verify: $verify, worker_verify: $wverify, category, difficulty: "hard", acceptance_dir: null, score_regex: null}' <<<"$row" >>"$scratch/manifest.jsonl"
done
jq -s . "$scratch/manifest.jsonl" >"$root/calibration.json"
mkdir -p "$root/build-logs" && cp "$scratch"/*.log "$scratch"/*.time "$root/build-logs/"
if [ -s "$scratch/invalid" ]; then echo "invalid tasks: $(tr '\n' ' ' <"$scratch/invalid")"; exit 1; fi
echo "wrote $root/calibration.json ($(jq length "$root/calibration.json") tasks)"
