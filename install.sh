#!/usr/bin/env bash
# Install the worker tools, Python package, and skills, backing up changed destinations.
# Claude Code uses the shared claive-orchestration skill; Codex has a separate routing adapter.
# TTC experiments are deferred and are not installed by this script.
set -euo pipefail

source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
bin_dir=${CLAIVE_BIN_DIR:-$HOME/.local/bin}
codex_skills=${CODEX_HOME:-$HOME/.codex}/skills
claude_skills=${CLAUDE_HOME:-$HOME/.claude}/skills
skill_dir=$codex_skills/claive-routing-codex
backup_base=${XDG_STATE_HOME:-$HOME/.local/state}/claive-install/backups
package_source=$source_dir/bin/claivelib
package_target=$bin_dir/claivelib
pi_skills=${PI_CODING_AGENT_DIR:-$HOME/.pi/agent}/skills
dry_run=false
with_pi=false
runtime_only=false
for arg in "$@"; do
  case $arg in
    --dry-run) dry_run=true ;;
    --pi) with_pi=true ;;
    --runtime-only) runtime_only=true ;;
    *) echo 'Usage: install.sh [--dry-run] [--pi] [--runtime-only]' >&2; exit 2 ;;
  esac
done
for path in "$bin_dir" "$backup_base"; do
  [[ $path == /* ]] || { echo "Installation paths must be absolute: $path" >&2; exit 2; }
done
if ! "$runtime_only"; then
  for path in "$codex_skills" "$claude_skills" "$pi_skills"; do
    [[ $path == /* ]] || { echo "Installation paths must be absolute: $path" >&2; exit 2; }
  done
fi

sources=("$source_dir/bin/claive" "$source_dir/bin/claive-worker"
         "$source_dir/bin/claive-codex" "$source_dir/bin/claive-orch"
         "$source_dir/bin/claive-memcap")
targets=("$bin_dir/claive" "$bin_dir/claive-worker"
         "$bin_dir/claive-codex" "$bin_dir/claive-orch"
         "$bin_dir/claive-memcap")
modes=(755 755 755 755 755)
# --runtime-only syncs the package, launchers, and provenance, and skips every
# skill check and write: Skillshare owns skill destinations, so a divergent or
# broken managed skill link must not block a runtime repair.
if ! "$runtime_only"; then
  sources+=("$source_dir/skills/claive-routing-codex/SKILL.md")
  targets+=("$skill_dir/SKILL.md")
  modes+=(644)
fi
# Host-neutral skills go to Codex and Claude Code, and with --pi to a Pi parent agent.
# Pi workers read the same directory; claive's recursion guard stops them launching workers.
# Skipped entirely with --runtime-only (see above).
skill_roots=("$codex_skills" "$claude_skills")
! "$with_pi" || skill_roots+=("$pi_skills")
if ! "$runtime_only"; then
for skill in claive-orchestration; do
  for skills_root in "${skill_roots[@]}"; do
    sources+=("$source_dir/skills/$skill/SKILL.md")
    targets+=("$skills_root/$skill/SKILL.md")
    modes+=(644)
  done
done
fi

# Migrate only links to this checkout's own five launchers. Preserve unrelated
# links and all Skillshare-owned skill links; never install through a symlink.
project_launcher_link() {
  [[ $1 -lt 5 && -L $3 ]] &&
    [[ "$(readlink -f -- "$3")" == "$(readlink -f -- "$2")" ]]
}

[[ -d $package_source ]] || { echo "Missing source package: $package_source" >&2; exit 1; }
if [[ -L $package_target || ( -e $package_target && ! -d $package_target ) ]]; then
  echo "Refusing to replace a symlink or non-directory: $package_target" >&2
  exit 1
fi
for index in "${!sources[@]}"; do
  [[ -f ${sources[index]} ]] || { echo "Missing source: ${sources[index]}" >&2; exit 1; }
  source_file=${sources[index]}
  target=${targets[index]}
  if project_launcher_link "$index" "$source_file" "$target"; then
    continue
  fi
  if [[ -L $target || -L $(dirname -- "$target") ]]; then
    # Skillshare links the SKILL DIRECTORY, not SKILL.md. Never write through it.
    # The repo and managed source must match before the rest of the installer runs.
    if [[ ! -f $target ]] || ! cmp -s -- "$source_file" "$target"; then
      echo "Managed skill link differs or is broken: $target (update Skillshare source and sync)" >&2
      exit 1
    fi
  elif [[ -e $target && ! -f $target ]]; then
    echo "Refusing to replace a non-file: $target" >&2
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
# provenance.json is installer-generated (see below), so it is excluded here.
if [[ -d $package_target ]] &&
   diff -qr --exclude='__pycache__' --exclude='provenance.json' \
     "$package_source" "$package_target" >/dev/null 2>&1; then
  echo "Already current: $package_target"
else
  if "$dry_run"; then
    echo "Would install package: $package_source -> $package_target"
  else
    if [[ -d $package_target ]]; then
      ensure_backup_dir
      cp -a -- "$package_target" "$backup_dir/claivelib"
    fi
    mkdir -p -- "$bin_dir"
    package_stage=$(mktemp -d "$bin_dir/.claivelib.install.XXXXXX")
    cp -a -- "$package_source/." "$package_stage/"
    find "$package_stage" -type d -name __pycache__ -prune -exec rm -rf -- {} +
    find "$package_stage" -type d -exec chmod 755 -- {} +
    find "$package_stage" -type f -exec chmod 644 -- {} +
    rm -rf -- "$package_target"
    mv -- "$package_stage" "$package_target"
    echo "Installed package: $package_target"
  fi
fi

# Record source revision/path/content fingerprints beside the installed package.
# bin/claivelib/runtime.py reads this for read-only source-to-installed
# drift/smoke checks (callable by `claive doctor`); never edit it by hand.
provenance_target=$package_target/provenance.json
provenance_current() {
  CLAIVE_SOURCE_DIR=$source_dir CLAIVE_PROVENANCE_FILE=$provenance_target python3 - <<'PY'
import json
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(os.environ["CLAIVE_SOURCE_DIR"]) / "bin"))
from claivelib import runtime
fresh = runtime.build_provenance(Path(os.environ["CLAIVE_SOURCE_DIR"]))
try:
    current = json.loads(Path(os.environ["CLAIVE_PROVENANCE_FILE"]).read_text(encoding="utf-8"))
except (OSError, ValueError, AttributeError):
    # Missing, unparsable, or hand-edited to non-dict JSON: stale, rewrite below.
    sys.exit(1)
keys = ("source_dir", "source_revision", "files", "launchers")
sys.exit(0 if all(current.get(key) == fresh[key] for key in keys) else 1)
PY
}
if "$dry_run"; then
  echo "Would write provenance: $provenance_target"
elif provenance_current; then
  echo "Already current: $provenance_target"
else
  if [[ -f $provenance_target ]]; then
    ensure_backup_dir
    cp -p -- "$provenance_target" "$backup_dir/provenance.json"
  fi
  CLAIVE_SOURCE_DIR=$source_dir CLAIVE_PROVENANCE_FILE=$provenance_target python3 - <<'PY'
import json
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(os.environ["CLAIVE_SOURCE_DIR"]) / "bin"))
from claivelib import runtime
provenance_file = Path(os.environ["CLAIVE_PROVENANCE_FILE"])
record = runtime.build_provenance(Path(os.environ["CLAIVE_SOURCE_DIR"]))
provenance_file.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
  chmod 644 -- "$provenance_target"
  echo "Wrote provenance: $provenance_target"
fi

for index in "${!sources[@]}"; do
  source_file=${sources[index]}
  target=${targets[index]}
  if project_launcher_link "$index" "$source_file" "$target"; then
    if "$dry_run"; then
      echo "Would migrate project launcher link with backup: $target"
    else
      ensure_backup_dir
      cp -a -- "$target" "$backup_dir/$(basename -- "$target")"
      launcher_stage=$(mktemp "$bin_dir/.claive.launcher.XXXXXX")
      install -m "${modes[index]}" -- "$source_file" "$launcher_stage"
      mv -T -- "$launcher_stage" "$target"
      echo "Migrated project launcher link: $target"
    fi
    continue
  fi
  if [[ -L $target || -L $(dirname -- "$target") ]]; then
    echo "Already current (managed link): $target"
    continue
  fi
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
    backup_name=$(basename -- "$target")
    [[ $backup_name != SKILL.md ]] ||
      backup_name=$(basename -- "$(dirname -- "$(dirname -- "$(dirname -- "$target")")")")-$(basename -- "$(dirname -- "$target")").SKILL.md
    cp -p -- "$target" "$backup_dir/$backup_name"
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

# Retire the pre-rename codex-* commands and codex_workers package installed by this
# project (identified by their references to it); preserve symlinks and unrelated files.
for legacy in codex-workers codex-subagent-worker codex-with-workers codex-orch; do
  legacy_path=$bin_dir/$legacy
  if [[ ! -f $legacy_path || -L $legacy_path ]] ||
     ! grep -q 'codex_workers\|codex-workers' -- "$legacy_path"; then
    continue
  fi
  if "$dry_run"; then
    echo "Would retire: $legacy_path"
  else
    ensure_backup_dir
    cp -p -- "$legacy_path" "$backup_dir/$legacy"
    rm -- "$legacy_path"
    echo "Retired: $legacy_path"
  fi
done
legacy_package=$bin_dir/codex_workers
if [[ -d $legacy_package && ! -L $legacy_package && -f $legacy_package/cli.py && -f $legacy_package/engine.py ]]; then
  if "$dry_run"; then
    echo "Would retire package: $legacy_package"
  else
    ensure_backup_dir
    cp -a -- "$legacy_package" "$backup_dir/codex_workers"
    rm -rf -- "$legacy_package"
    echo "Retired package: $legacy_package"
  fi
fi
[[ -z $backup_dir ]] || echo "Previous files saved in: $backup_dir"
echo 'Global AGENTS.md is managed separately; reference/global-AGENTS.md is a snapshot.'
