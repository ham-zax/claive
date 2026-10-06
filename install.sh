#!/usr/bin/env bash
# Install the worker tools, Python package, and skill, backing up changed destinations.
set -euo pipefail

source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
bin_dir=${MUSE_SUBAGENTS_BIN_DIR:-$HOME/.local/bin}
skill_dir=${CODEX_HOME:-$HOME/.codex}/skills/subagent-routing
backup_base=${XDG_STATE_HOME:-$HOME/.local/state}/muse-subagents/backups
package_source=$source_dir/bin/codex_workers
package_target=$bin_dir/codex_workers
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

sources=("$source_dir/bin/codex-workers" "$source_dir/bin/codex-subagent-worker"
         "$source_dir/bin/codex-with-workers" "$source_dir/skills/subagent-routing/SKILL.md")
targets=("$bin_dir/codex-workers" "$bin_dir/codex-subagent-worker"
         "$bin_dir/codex-with-workers" "$skill_dir/SKILL.md")
modes=(755 755 755 644)

[[ -d $package_source ]] || { echo "Missing source package: $package_source" >&2; exit 1; }
if [[ -L $package_target || ( -e $package_target && ! -d $package_target ) ]]; then
  echo "Refusing to replace a symlink or non-directory: $package_target" >&2
  exit 1
fi
for index in "${!sources[@]}"; do
  [[ -f ${sources[index]} ]] || { echo "Missing source: ${sources[index]}" >&2; exit 1; }
  target=${targets[index]}
  if [[ -L $target || ( -e $target && ! -f $target ) ]]; then
    echo "Refusing to replace a symlink or non-file: $target" >&2
    exit 1
  fi
done

backup_dir=''
ensure_backup_dir() {
  if [[ -z $backup_dir ]]; then
    mkdir -p -- "$backup_base"
    backup_dir=$(mktemp -d "$backup_base/$(date +%Y%m%d-%H%M%S).XXXXXX")
  fi
}

# Install the package before the launcher that imports it.
if [[ -d $package_target ]] &&
   diff -qr --exclude='__pycache__' "$package_source" "$package_target" >/dev/null 2>&1; then
  echo "Already current: $package_target"
else
  if "$dry_run"; then
    echo "Would install package: $package_source -> $package_target"
  else
    if [[ -d $package_target ]]; then
      ensure_backup_dir
      cp -a -- "$package_target" "$backup_dir/codex_workers"
    fi
    mkdir -p -- "$bin_dir"
    package_stage=$(mktemp -d "$bin_dir/.codex_workers.install.XXXXXX")
    cp -a -- "$package_source/." "$package_stage/"
    find "$package_stage" -type d -name __pycache__ -prune -exec rm -rf -- {} +
    find "$package_stage" -type d -exec chmod 755 -- {} +
    find "$package_stage" -type f -exec chmod 644 -- {} +
    rm -rf -- "$package_target"
    mv -- "$package_stage" "$package_target"
    echo "Installed package: $package_target"
  fi
fi

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
    ensure_backup_dir
    cp -p -- "$target" "$backup_dir/$(basename -- "$target")"
  fi
  mkdir -p -- "$(dirname -- "$target")"
  install -m "${modes[index]}" -- "$source_file" "$target"
  echo "Installed: $target"
done

# Retire only the exact launcher previously installed by this project.
# Preserve unrelated files and symlinks at the old name.
legacy_launcher=$bin_dir/muse-worker
if [[ -f $legacy_launcher && ! -L $legacy_launcher ]] &&
   cmp -s -- "$legacy_launcher" <(cat <<'LEGACY'
#!/usr/bin/env bash
# Keep a Muse worker available for follow-ups with visible progress.
set -euo pipefail
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
exec "$script_dir/codex-workers" open "$@"
LEGACY
); then
  if "$dry_run"; then
    echo "Would retire launcher: $legacy_launcher"
  else
    ensure_backup_dir
    cp -p -- "$legacy_launcher" "$backup_dir/muse-worker"
    rm -- "$legacy_launcher"
    echo "Retired launcher: $legacy_launcher"
  fi
fi
[[ -z $backup_dir ]] || echo "Previous files saved in: $backup_dir"
echo 'Global AGENTS.md is managed separately; reference/global-AGENTS.md is a snapshot.'
