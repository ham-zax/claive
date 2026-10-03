#!/usr/bin/env bash
# Run AGY (Gemini 3.8 Flash, free) on a prompt file; print only its final answer.
# Usage: agy-relay <prompt_file> <cwd> [model]
set -euo pipefail
prompt_file=$1 cwd=$2 model=${3:-gemini-3.8-flash-high}
[[ -s $prompt_file ]] || { echo "empty or missing prompt file: $prompt_file" >&2; exit 2; }
[[ -d $cwd ]] || { echo "missing cwd: $cwd" >&2; exit 2; }
logdir=${XDG_CACHE_HOME:-$HOME/.cache}/agy-relay
mkdir -p "$logdir"
log=$logdir/$(date +%Y%m%d-%H%M%S)-$$.log
ln -sfn "$log" "$logdir/latest.log"
echo "log: $log"
set +e
(cd "$cwd" && agy -p "$(cat "$prompt_file")" --model "$model" --dangerously-skip-permissions) < /dev/null > "$log" 2>&1
rc=$?
set -e
echo "exit=$rc"
tail -c 12000 "$log"
exit "$rc"
