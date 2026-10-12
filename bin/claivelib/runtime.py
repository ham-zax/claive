"""Installed-runtime provenance and read-only source-to-installed drift/smoke checks.

The installer records where the installed runtime came from (source path and
revision) plus content fingerprints of every runtime file. These helpers only
read files: they never write, launch workers, or touch the network, so the
parent-owned `claive doctor` can call :func:`doctor_checks` directly.
"""

import ast
import hashlib
import json
import subprocess
from pathlib import Path

PROVENANCE_FILENAME = "provenance.json"
# Launchers synced by install.sh alongside the claivelib package.
LAUNCHER_NAMES = ("claive", "claive-worker", "claive-codex", "claive-orch", "claive-memcap")
# The installed Codex adapter must stay opt-in: hermetic by default, user config
# only when CLAIVE_CODEX_USE_USER_CONFIG=1, while --ignore-rules always stays.
CODEX_OPT_IN_MARKER = "CLAIVE_CODEX_USE_USER_CONFIG"
CODEX_RULES_FLAG = "--ignore-rules"


def package_dir():
    """Directory of the installed claivelib package (this file's parent)."""
    return Path(__file__).resolve().parent


def bin_dir(installed_package_dir=None):
    """Directory holding the installed launchers (parent of the package)."""
    base = Path(installed_package_dir).resolve() if installed_package_dir else package_dir()
    return base.parent


def source_dirs(source_dir):
    """Source package and bin directories for a repo checkout root."""
    root = Path(source_dir).resolve()
    return root / "bin" / "claivelib", root / "bin"


def fingerprint_file(path):
    """Hex sha256 of a file's bytes."""
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tracked(root):
    """Content-bearing files under root, excluding caches and provenance."""
    files = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        if path.name == PROVENANCE_FILENAME:
            continue
        files[path.relative_to(root).as_posix()] = path
    return files


def fingerprint_tree(root):
    """Map of relative path to sha256 for the runtime files under root."""
    return {name: fingerprint_file(path) for name, path in _tracked(Path(root)).items()}


def fingerprint_launchers(source_or_install_bin):
    """Map of launcher name to sha256 for launchers present in a bin directory."""
    root = Path(source_or_install_bin)
    fingerprints = {}
    for name in LAUNCHER_NAMES:
        candidate = root / name
        if candidate.is_file() and not candidate.is_symlink():
            fingerprints[name] = fingerprint_file(candidate)
    return fingerprints


def source_revision(source_dir):
    """Best-effort `<sha>[+dirty]` for a checkout, or `unknown` outside git."""
    try:
        head = subprocess.run(
            ["git", "-C", str(source_dir), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL,
        )
        if head.returncode != 0 or not head.stdout.strip():
            return "unknown"
        dirty = subprocess.run(
            ["git", "-C", str(source_dir), "status", "--porcelain"],
            capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL,
        )
        suffix = "+dirty" if dirty.returncode == 0 and dirty.stdout.strip() else ""
        return head.stdout.strip() + suffix
    except (OSError, ValueError, subprocess.SubprocessError):
        return "unknown"


def build_provenance(source_dir, installed_at=None):
    """Provenance record for a source checkout (pure; writes nothing)."""
    package_source, bin_source = source_dirs(source_dir)
    if installed_at is None:
        from datetime import datetime, timezone
        installed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return {
        "source_dir": str(Path(source_dir).resolve()),
        "source_revision": source_revision(source_dir),
        "installed_at": installed_at,
        "files": fingerprint_tree(package_source) if package_source.is_dir() else {},
        "launchers": fingerprint_launchers(bin_source) if bin_source.is_dir() else {},
    }


def load_provenance(installed_package_dir=None):
    """Installed provenance record (a dict), or None when absent/unreadable (read-only).

    Non-dict JSON (a list, string, number, or boolean left by a manual edit)
    counts as unreadable: every caller calls `.get` or key checks on the result.
    """
    base = Path(installed_package_dir).resolve() if installed_package_dir else package_dir()
    try:
        record = json.loads((base / PROVENANCE_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def _compare(source_map, installed_map):
    changed = sorted(name for name in source_map
                     if name in installed_map and installed_map[name] != source_map[name])
    deleted = sorted(name for name in source_map if name not in installed_map)
    extra = sorted(name for name in installed_map if name not in source_map)
    return changed, deleted, extra


def check_drift(source_dir=None, installed_package_dir=None):
    """Read-only source-to-installed content comparison.

    Returns a dict with `status` (`clean`, `drifted`, or `unknown` when no
    source checkout is available), `ok`, sorted `changed`/`deleted`/`extra`
    relative paths (`bin/<launcher>` prefix for launchers), and a `detail`
    string. Never writes.
    """
    installed_base = (Path(installed_package_dir).resolve()
                      if installed_package_dir else package_dir())
    installed_bin = bin_dir(installed_base)
    provenance = load_provenance(installed_base)
    if source_dir is None and isinstance(provenance, dict):
        candidate = provenance.get("source_dir")
        if isinstance(candidate, str) and Path(candidate).is_dir():
            source_dir = candidate
    if source_dir is None or not Path(source_dir).is_dir():
        return {
            "status": "unknown", "ok": True,
            "source_dir": None, "installed_dir": str(installed_base),
            "source_revision": None,
            "installed_revision": provenance.get("source_revision") if provenance else None,
            "changed": [], "deleted": [], "extra": [],
            "detail": "no source checkout available; cannot compare drift",
        }
    package_source, bin_source = source_dirs(source_dir)
    source_files = fingerprint_tree(package_source) if package_source.is_dir() else {}
    installed_files = fingerprint_tree(installed_base)
    source_launchers = fingerprint_launchers(bin_source) if bin_source.is_dir() else {}
    installed_launchers = fingerprint_launchers(installed_bin)
    changed, deleted, extra = _compare(source_files, installed_files)
    launcher_changed, launcher_deleted, launcher_extra = _compare(
        source_launchers, installed_launchers)
    changed += [f"bin/{name}" for name in launcher_changed]
    deleted += [f"bin/{name}" for name in launcher_deleted]
    extra += [f"bin/{name}" for name in launcher_extra]
    changed.sort()
    deleted.sort()
    extra.sort()
    drifted = bool(changed or deleted or extra)
    live_revision = source_revision(source_dir)
    if drifted:
        parts = []
        if changed:
            parts.append(f"changed: {', '.join(changed)}")
        if deleted:
            parts.append(f"missing from install: {', '.join(deleted)}")
        if extra:
            parts.append(f"extra in install: {', '.join(extra)}")
        detail = "; ".join(parts)[:400]
    else:
        detail = f"installed runtime matches {source_dir} ({live_revision})"
    return {
        "status": "drifted" if drifted else "clean", "ok": not drifted,
        "source_dir": str(Path(source_dir).resolve()), "installed_dir": str(installed_base),
        "source_revision": live_revision,
        "installed_revision": provenance.get("source_revision") if provenance else None,
        "changed": changed, "deleted": deleted, "extra": extra,
        "detail": detail,
    }


def smoke_check(installed_package_dir=None):
    """Read-only installed-runtime sanity checks (no source needed).

    Verifies the provenance record parses, every installed file and launcher
    still matches its recorded fingerprint, every installed file parses as
    Python, and the installed Codex adapter keeps the user-config opt-in
    while always retaining --ignore-rules. Returns
    `{"ok": bool, "failures": [...], "detail": str}`.
    """
    installed_base = (Path(installed_package_dir).resolve()
                      if installed_package_dir else package_dir())
    failures = []
    provenance = load_provenance(installed_base)
    if provenance is None:
        failures.append(f"{PROVENANCE_FILENAME} missing or unreadable: reinstall with install.sh")
    else:
        missing_keys = {"source_dir", "source_revision", "installed_at", "files", "launchers"
                        } - set(provenance)
        if missing_keys:
            failures.append(f"{PROVENANCE_FILENAME} missing keys: {', '.join(sorted(missing_keys))}")
        for key in ("files", "launchers"):
            if key in provenance and not isinstance(provenance[key], dict):
                failures.append(
                    f"{PROVENANCE_FILENAME} has a malformed {key!r} map: reinstall with install.sh")
        recorded_files = provenance.get("files")
        if isinstance(recorded_files, dict):
            live_files = {name: fingerprint_file(path)
                          for name, path in sorted(_tracked(installed_base).items())}
            for name in sorted(recorded_files):
                if name not in live_files:
                    failures.append(f"{name} recorded in {PROVENANCE_FILENAME} is missing: "
                                    "reinstall with install.sh")
                elif live_files[name] != recorded_files[name]:
                    failures.append(f"{name} differs from its {PROVENANCE_FILENAME} fingerprint: "
                                    "reinstall with install.sh")
            for name in sorted(live_files):
                if name not in recorded_files:
                    failures.append(f"{name} is not recorded in {PROVENANCE_FILENAME}: "
                                    "reinstall with install.sh")
        recorded_launchers = provenance.get("launchers")
        if isinstance(recorded_launchers, dict):
            live_launchers = fingerprint_launchers(bin_dir(installed_base))
            for name in sorted(recorded_launchers):
                if name not in live_launchers:
                    failures.append(f"bin/{name} recorded in {PROVENANCE_FILENAME} is missing: "
                                    "reinstall with install.sh")
                elif live_launchers[name] != recorded_launchers[name]:
                    failures.append(f"bin/{name} differs from its {PROVENANCE_FILENAME} fingerprint: "
                                    "reinstall with install.sh")
    for name, path in sorted(_tracked(installed_base).items()):
        if path.suffix != ".py":
            continue
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=name)
        except (OSError, ValueError, SyntaxError) as error:
            failures.append(f"{name} does not parse: {error}")
    codex = installed_base / "engines" / "codex.py"
    try:
        text = codex.read_text(encoding="utf-8")
    except OSError:
        failures.append("engines/codex.py is missing")
    else:
        if CODEX_OPT_IN_MARKER not in text:
            failures.append("engines/codex.py lost the CLAIVE_CODEX_USE_USER_CONFIG opt-in")
        if CODEX_RULES_FLAG not in text:
            failures.append("engines/codex.py lost --ignore-rules")
    ok = not failures
    return {
        "ok": ok, "failures": failures,
        "detail": "installed runtime parses" if ok else "; ".join(failures)[:400],
    }


def doctor_checks(source_dir=None, installed_package_dir=None):
    """Doctor-ready `(name, ok, required, detail)` dicts; all read-only.

    All three checks are advisory (`required=False`): an install predating
    provenance, or a host without the source checkout, warns instead of
    failing. The parent-owned `claive doctor` decides whether to harden them.
    """
    installed_base = (Path(installed_package_dir).resolve()
                      if installed_package_dir else package_dir())
    provenance = load_provenance(installed_base)
    if provenance is None:
        provenance_check = {
            "name": "runtime_provenance", "ok": False, "required": False,
            "detail": f"{PROVENANCE_FILENAME} missing: reinstall with install.sh",
        }
    else:
        provenance_check = {
            "name": "runtime_provenance", "ok": True, "required": False,
            "detail": f"{provenance.get('source_revision', 'unknown')} from "
                      f"{provenance.get('source_dir', 'unknown')} "
                      f"at {provenance.get('installed_at', 'unknown')}",
        }
    drift = check_drift(source_dir, installed_base)
    drift_check = {
        "name": "runtime_drift", "ok": drift["ok"], "required": False,
        "detail": drift["detail"],
    }
    smoke_result = smoke_check(installed_base)
    smoke_check_result = {
        "name": "runtime_smoke", "ok": smoke_result["ok"], "required": False,
        "detail": smoke_result["detail"],
    }
    return [provenance_check, drift_check, smoke_check_result]
