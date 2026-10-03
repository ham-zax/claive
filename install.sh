#!/usr/bin/env bash
# Install the worker tools and skill, backing up any changed destination files.
set -euo pipefail

source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
bin_dir=${MUSE_SUBAGENTS_BIN_DIR:-$HOME/.local/bin}
skill_dir=${CODEX_HOME:-$HOME/.codex}/skills/subagent-routing
backup_base=${XDG_STATE_HOME:-$HOME/.local/state}/muse-subagents/backups
dry_run=false
case ${1:-} in
  --dry-run) dry_run=true ;;
  '') ;;
  *) echo 'Usage: install.sh [--dry-run]' >&2; exit 2 ;;
esac
(( $# <= 1 )) || { echo 'Usage: install.sh [--dry-run]' >&2; exit 2; }
for path in "$bin_dir" "$skill_dir" "$backup_base"; do
  [[ $path == /* ]] || { echo "Installation paths must be absolute: $path" >&2; exit 2; }
done

sources=("$source_dir/bin/codex-workers" "$source_dir/bin/muse-worker"
         "$source_dir/bin/codex-with-workers" "$source_dir/skills/subagent-routing/SKILL.md")
targets=("$bin_dir/codex-workers" "$bin_dir/muse-worker"
         "$bin_dir/codex-with-workers" "$skill_dir/SKILL.md")
modes=(755 755 755 644)
for index in "${!sources[@]}"; do
  [[ -f ${sources[index]} ]] || { echo "Missing source: ${sources[index]}" >&2; exit 1; }
  target=${targets[index]}
  if [[ -L $target || ( -e $target && ! -f $target ) ]]; then
    echo "Refusing to replace a symlink or non-file: $target" >&2
    exit 1
  fi
done

backup_dir=''
for index in "${!sources[@]}"; do
  source_file=${sources[index]}
  target=${targets[index]}
  if [[ -f $target ]] && cmp -s -- "$source_file" "$target" &&
     [[ $(stat -c '%a' -- "$target") == "${modes[index]}" ]]; then
    echo "Already current: $target"
    continue
  fi
  if "$dry_run"; then
    echo "Would install: $source_file -> $target (mode ${modes[index]})"
    continue
  fi
  if [[ -f $target ]]; then
    if [[ -z $backup_dir ]]; then
      mkdir -p -- "$backup_base"
      backup_dir=$(mktemp -d "$backup_base/$(date +%Y%m%d-%H%M%S).XXXXXX")
    fi
    cp -p -- "$target" "$backup_dir/$(basename -- "$target")"
  fi
  mkdir -p -- "$(dirname -- "$target")"
  install -m "${modes[index]}" -- "$source_file" "$target"
  echo "Installed: $target"
done
[[ -z $backup_dir ]] || echo "Previous files saved in: $backup_dir"
echo 'Global AGENTS.md is managed separately; reference/global-AGENTS.md is a snapshot.'
