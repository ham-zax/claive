#!/usr/bin/env bash
# Build the calibration corpus from tasks.json (needs ~/corpus/qap with its .venv: see README.md).
#   tasks/<id>   single-commit repo: parent of the fix + the fix's tests. No fix history.
#   _ref/<id>    same base commit plus the reference commit (validation only; never shown to workers)
#   calibration.json   manifest for the ttc-experiment skill
# Each task is validated: the verifier must fail at base and pass at the reference.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
root=${CORPUS_ROOT:-$HOME/corpus}
src=$root/qap
py=$src/.venv/bin/python
export GIT_AUTHOR_NAME=calibration GIT_AUTHOR_EMAIL=calibration@local GIT_COMMITTER_NAME=calibration GIT_COMMITTER_EMAIL=calibration@local
export GIT_AUTHOR_DATE=2026-01-01T00:00:00Z GIT_COMMITTER_DATE=2026-01-01T00:00:00Z
mkdir -p "$root/tasks" "$root/_ref"
scratch=$(mktemp -d "$root/.build.XXXX")
trap 'git -C "$src" worktree prune; rm -rf "$scratch"' EXIT
manifest=$scratch/manifest.jsonl

jq -c '.[]' "$here/tasks.json" | while read -r row; do
  id=$(jq -r .id <<<"$row"); short=$(jq -r .commit <<<"$row")
  full=$(git -C "$src" rev-parse "$short^{commit}")
  verify="PYTHONPATH=src $py -m pytest -q -p no:cacheprovider $(jq -r '.tests | join(" ")' <<<"$row")"
  wt=$scratch/wt-$id
  git -C "$src" worktree add -q --detach "$wt" "$full^"
  mapfile -t files < <(git -C "$src" diff-tree --no-commit-id --name-only -r --diff-filter=AM "$full" | grep -E '(^|/)tests?/')
  git -C "$wt" checkout -q "$full" -- "${files[@]}"
  rm -rf "$root/tasks/$id" "$root/_ref/$id"
  for kind in tasks _ref; do
    mkdir -p "$root/$kind/$id"
    git -C "$wt" ls-files -z | tar --null -C "$wt" -T - -cf - | tar -C "$root/$kind/$id" -xf -
    git -C "$root/$kind/$id" init -q -b main
    git -C "$root/$kind/$id" add -A
    git -C "$root/$kind/$id" commit -q -m "calibration base: $id"
  done
  base=$(git -C "$root/tasks/$id" rev-parse HEAD)
  [ "$base" = "$(git -C "$root/_ref/$id" rev-parse HEAD)" ] || { echo "$id: base hash differs"; exit 1; }
  git -C "$src" worktree remove --force "$wt"
  # reference = base + the real fix's tree
  git -C "$root/_ref/$id" rm -rq --cached . && find "$root/_ref/$id" -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +
  git -C "$src" archive "$full" | tar -C "$root/_ref/$id" -xf -
  git -C "$root/_ref/$id" add -A
  git -C "$root/_ref/$id" commit -q -m "calibration reference: $id" --allow-empty
  ref=$(git -C "$root/_ref/$id" rev-parse HEAD)
  b=0; (cd "$root/tasks/$id" && bash -c "$verify" >/dev/null 2>&1) || b=$?
  r=0; (cd "$root/_ref/$id" && bash -c "$verify" >/dev/null 2>&1) || r=$?
  if [ "$b" -ne 0 ] && [ "$r" -eq 0 ]; then verdict=OK; else verdict="INVALID(base=$b ref=$r)"; fi
  printf '%-28s base %.8s ref %.8s %s\n' "$id" "$base" "$ref" "$verdict"
  jq -c --arg repo "$root/tasks/$id" --arg refrepo "$root/_ref/$id" --arg base "$base" --arg ref "$ref" --arg verify "$verify" \
    '{id, repo: $repo, base: $base, reference: $ref, reference_repo: $refrepo, task, verify: $verify, category, difficulty, acceptance_dir: null, score_regex: null}' <<<"$row" >>"$manifest"
  [ "$verdict" = OK ] || echo "$id" >>"$scratch/invalid"
done
jq -s . "$manifest" >"$root/calibration.json"
if [ -s "$scratch/invalid" ]; then echo "invalid tasks: $(tr '\n' ' ' <"$scratch/invalid")"; exit 1; fi
echo "wrote $root/calibration.json ($(jq length "$root/calibration.json") tasks)"
