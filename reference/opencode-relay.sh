#!/usr/bin/env bash
# Run opencode (free Zen models) on a prompt file; print only its final answer.
# Usage: opencode-relay <prompt_file> <cwd> [model] [session_id]
# Pass the printed session id back to continue the same worker with its context.
set -euo pipefail
prompt_file=$1 cwd=$2 model=${3:-opencode/muse-spark-1.3-contributor-free} session=${4:-}
[[ -s $prompt_file ]] || { echo "empty or missing prompt file: $prompt_file" >&2; exit 2; }
[[ -d $cwd ]] || { echo "missing cwd: $cwd" >&2; exit 2; }
logdir=${XDG_CACHE_HOME:-$HOME/.cache}/opencode-relay
mkdir -p "$logdir"
log=$logdir/$(date +%Y%m%d-%H%M%S)-$$.jsonl
ln -sfn "$log" "$logdir/latest.jsonl"
echo "log: $log"
args=(run -m "$model" --dir "$cwd" --auto --format json)
[[ $model == *muse-spark* ]] && args+=(--variant max)
[[ -n $session ]] && args+=(-s "$session")
set +e
opencode "${args[@]}" "$(cat "$prompt_file")" < /dev/null > "$log" 2>&1
rc=$?
set -e
echo "exit=$rc"
echo "session: $(jq -rs 'map(.sessionID? // empty) | last // "unknown"' "$log" 2>/dev/null || echo unknown)"
if ! jq -rs '[.[] | select(.type? == "text")] | last | .part.text // empty' "$log" 2>/dev/null | grep -q .; then
  tail -40 "$log"
else
  jq -rs '[.[] | select(.type? == "text")] | last | .part.text' "$log"
fi
exit "$rc"
