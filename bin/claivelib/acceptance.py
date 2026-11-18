"""Parent-owned verification and acceptance, independent of worker execution."""
import fnmatch
import fcntl
from contextlib import contextmanager
from functools import wraps
import hashlib
import json
import os
import re
from pathlib import Path, PurePosixPath
import shlex
import shutil
import subprocess
import tempfile
import time

from claivelib import config, procgroup

OUTCOMES = {"passed", "failed", "blocked", "not_run"}
VERIFIER_BYPRODUCT_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".vitest", ".turbo", ".tox", ".nox"}
NOISE_DIR_NAMES = VERIFIER_BYPRODUCT_DIRS | {"node_modules", ".venv", "venv", ".git"}
NOISE_FILENAMES = {"launch.lock"}


def _is_verifier_byproduct(rel_path):
    parts = PurePosixPath(rel_path).parts
    return bool(VERIFIER_BYPRODUCT_DIRS.intersection(parts)) or rel_path.endswith((".pyc", ".pyo"))


def _linked_dep_prefixes(workspace=None, job_dir=None) -> set[str]:
    candidates = []
    if job_dir:
        candidates.append(Path(job_dir) / "linked-deps.json")
    if workspace:
        ws = Path(workspace)
        if ws.name == "worktree":
            candidates.append(ws.parent / "linked-deps.json")
    prefixes: set[str] = set()
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            loaded = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(loaded, dict):
            linked = loaded.get("linked")
            if isinstance(linked, (list, tuple)):
                for p in linked:
                    if isinstance(p, str) and p.strip("/"):
                        prefixes.add(p.strip("/"))
    return prefixes


def is_noise_path(rel_path: str, linked_deps: set[str] | frozenset[str] = frozenset()) -> bool:
    """Return True if rel_path is a verifier/runtime byproduct or linked-deps symlink."""
    rel = str(rel_path or "").strip().strip("/")
    if not rel:
        return True
    parts = PurePosixPath(rel).parts
    if not parts:
        return True
    if _is_verifier_byproduct(rel):
        return True
    if bool(set(parts) & NOISE_DIR_NAMES):
        return True
    if parts[-1].endswith((".pyc", ".pyo")) or parts[-1] in NOISE_FILENAMES:
        return True
    return any(rel == dep or rel.startswith(dep + "/") for dep in linked_deps)


def filter_noise_paths(paths, workspace=None, job_dir=None) -> list[str]:
    """Filter verifier/runtime byproducts and linked-deps symlinks from workspace-relative paths."""
    linked = _linked_dep_prefixes(workspace=workspace, job_dir=job_dir)
    return [p for p in (paths or ()) if isinstance(p, str) and not is_noise_path(p, linked)]


def _refresh_status(path, state):
    if (path / "state.json").is_file() and isinstance(state, dict) and "id" in state and "status" in state:
        try:
            from claivelib import status_file
            status_file.write(path, state)
        except Exception:
            pass


def atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".evidence-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def owned_paths(paths):
    for item in paths:
        if not isinstance(item, str) or not item or item.startswith("/") or ".." in PurePosixPath(item).parts:
            raise ValueError("ownership paths must be nonempty workspace-relative paths or globs without '..'")
    return list(paths)


def manifest(filename):
    if not filename:
        return []
    path = Path(filename)
    if not path.is_absolute():
        raise ValueError("--checks-file must be absolute")
    value = json.loads(path.read_text())
    return validate_manifest(value)


SHELL_META_RE = re.compile(r"(?:&&|\|\||[;|<>`\n$()])|\A\s*cd\s+")
MANIFEST_EXAMPLE = (
    'Example: [{"name": "test", "shell": "cd web && npm test"}] '
    'or {"checks": [{"name": "test", "command": ["pytest", "-q"]}]}'
)


def parse_inline_check_command(command_str: str) -> list[str]:
    if not isinstance(command_str, str):
        raise ValueError(f"check command must be a string. {MANIFEST_EXAMPLE}")
    stripped = command_str.strip()
    if not stripped:
        raise ValueError(f"check command cannot be empty. {MANIFEST_EXAMPLE}")
    if SHELL_META_RE.search(stripped):
        return ["bash", "-lc", stripped]
    parts = shlex.split(stripped)
    if not parts or any(not part for part in parts):
        raise ValueError(f"check command cannot be empty. {MANIFEST_EXAMPLE}")
    return parts


def validate_manifest(value):
    if isinstance(value, dict):
        if set(value) - {"checks"}:
            raise ValueError(f"checks manifest object may only contain 'checks'. {MANIFEST_EXAMPLE}")
        if "checks" not in value or not isinstance(value["checks"], list):
            raise ValueError(f"checks manifest must be a list or {{'checks': [...]}}. {MANIFEST_EXAMPLE}")
        raw_checks = value["checks"]
    elif isinstance(value, list):
        raw_checks = value
    else:
        raise ValueError(f"checks manifest must be a list or {{'checks': [...]}}. {MANIFEST_EXAMPLE}")

    names = set()
    normalized_checks = []
    for check in raw_checks:
        if not isinstance(check, dict) or not isinstance(check.get("name"), str) or not check["name"]:
            raise ValueError(f"each check needs a nonempty name. {MANIFEST_EXAMPLE}")
        if check["name"] in names:
            raise ValueError("duplicate check name")
        names.add(check["name"])
        if "command" in check and "shell" in check:
            raise ValueError(f"check {check['name']!r} cannot set both 'command' and 'shell'. {MANIFEST_EXAMPLE}")
        if "shell" in check:
            shell_cmd = check["shell"]
            if not isinstance(shell_cmd, str) or not shell_cmd.strip():
                raise ValueError(f"check shell must be a nonempty string. {MANIFEST_EXAMPLE}")
            norm_command = ["bash", "-lc", shell_cmd.strip()]
        elif "command" in check:
            command = check["command"]
            if isinstance(command, str):
                norm_command = parse_inline_check_command(command)
            elif isinstance(command, list):
                if not command or any(not isinstance(x, str) or not x for x in command):
                    raise ValueError(f"check command must be a nonempty argv list. {MANIFEST_EXAMPLE}")
                norm_command = list(command)
            else:
                raise ValueError(f"check command must be a nonempty argv list. {MANIFEST_EXAMPLE}")
        else:
            raise ValueError(f"check {check['name']!r} requires 'command' or 'shell'. {MANIFEST_EXAMPLE}")

        if not isinstance(check.get("required", True), bool):
            raise ValueError("check required must be boolean")
        pytest = check.get("pytest")
        if pytest is not None:
            if not isinstance(pytest, dict) or set(pytest) - {"min_passed", "max_skipped"}:
                raise ValueError("pytest check settings must contain only min_passed/max_skipped")
            for key, number in pytest.items():
                if isinstance(number, bool) or not isinstance(number, int) or number < 0:
                    raise ValueError(f"pytest {key} must be a nonnegative integer")
        timeout = check.get("timeout", 300)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 3600:
            raise ValueError("check timeout must be between 0 and 3600 seconds")
        in_worktree = check.get("in_worktree")
        if in_worktree is not None and not isinstance(in_worktree, bool):
            raise ValueError("check in_worktree must be boolean")
        normalized = {key: val for key, val in check.items() if key != "shell"}
        normalized["command"] = norm_command
        normalized_checks.append(normalized)
    return normalized_checks


def validate_workspace(workspace):
    """Verify workspace is a Git work tree without hashing all files."""
    workspace = Path(workspace)
    timeout = config.launch_setting("git_timeout", 30)
    try:
        result = subprocess.run(
            ["git", "-C", str(workspace), "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise ValueError(f"git rev-parse timed out after {timeout} s in {workspace} "
                         f"(load {os.getloadavg()[0]:.2f}); raise launch.git_timeout in the claive config") from error
    if result.returncode or result.stdout.strip() != "true":
        raise ValueError("write ownership validation requires a Git workspace")


def snapshot(workspace, stat_cache_path=None):
    """Hash tracked and all untracked paths; retain dirty baseline and deletions."""
    workspace = Path(workspace)
    timeout = config.launch_setting("git_timeout", 30)

    def git(path, *args, **kwargs):
        try:
            return subprocess.run(["git", "-C", str(path), *args], timeout=timeout, **kwargs)
        except subprocess.TimeoutExpired as error:
            raise ValueError(f"git {args[0]} timed out after {timeout} s in {path} "
                             f"(load {os.getloadavg()[0]:.2f}); raise launch.git_timeout in the claive config") from error

    result = git(workspace, "ls-files", "-z", "--cached", "--others", capture_output=True)
    if result.returncode:
        raise ValueError("write ownership validation requires a Git workspace")
    head = git(workspace, "rev-parse", "HEAD", capture_output=True, text=True)
    prev_cache = {}
    if stat_cache_path is not None:
        try:
            loaded = json.loads(Path(stat_cache_path).read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                prev_cache = loaded
        except (OSError, ValueError):
            prev_cache = {}
    now_ns = time.time_ns()
    new_cache = {}
    files = {}
    for raw in set(result.stdout.split(b"\0")) - {b""}:
        name = os.fsdecode(raw)
        path = workspace / name
        if path.is_symlink():
            files[name] = ["symlink", os.readlink(path)]
        elif path.is_file():
            st = path.stat()
            mode = st.st_mode & 0o777
            sig = [st.st_ino, st.st_size, mode, st.st_mtime_ns, st.st_ctime_ns]
            cached = prev_cache.get(name)
            if (stat_cache_path is not None and isinstance(cached, list) and len(cached) == 6
                    and cached[:5] == sig and st.st_mtime_ns < now_ns - 2_000_000_000):
                hex_digest = cached[5]
            else:
                digest = hashlib.sha256()
                with path.open("rb") as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(block)
                hex_digest = digest.hexdigest()
            files[name] = ["file", hex_digest, mode]
            new_cache[name] = sig + [hex_digest]
        elif path.is_dir():
            # A submodule is a tracked directory: bind its commit and dirtiness.
            sub = git(path, "status", "--porcelain", capture_output=True)
            commit = git(path, "rev-parse", "HEAD", capture_output=True)
            files[name] = ["directory", commit.stdout.decode(errors="replace"), sub.stdout.decode(errors="replace")]
    if stat_cache_path is not None:
        cache_path = Path(stat_cache_path)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(dir=cache_path.parent, prefix=".stat-cache-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(new_cache, stream, separators=(",", ":"))
            os.replace(temp_name, cache_path)
        finally:
            Path(temp_name).unlink(missing_ok=True)
    return {"head": head.stdout.strip(), "files": files}


def changed(before, after):
    left, right = before["files"], after["files"]
    return sorted(name for name in left.keys() | right.keys() if left.get(name) != right.get(name))


def violations(paths, allowlist):
    return [name for name in paths if not any(fnmatch.fnmatchcase(name, pattern) or
            name.startswith(pattern.rstrip("/") + "/") for pattern in allowlist)]


def directory(path, state):
    return path / "evidence" / f"turn-{state.get('turn', 1):04}"


def begin(path, state):
    target = directory(path, state)
    target.mkdir(parents=True, exist_ok=True)
    contract = state.get("acceptance_contract", {})
    workspace = state.get("actual_workspace") or state["workspace"]
    isolation = (state.get("launch") or {}).get("isolation") or {}
    state.pop("ownership_error", None)
    if contract.get("owned_paths") is not None or contract.get("checks") or isolation.get("owner") == "claive":
        try:
            atomic(target / "before.json", snapshot(workspace, target / ".stat-cache.json"))
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            state["ownership_error"] = str(error)
    # Never carry verification or an accepted decision into another turn.
    pending = [dict(name=check["name"], status="not_run", reason="not yet executed", environment=None)
               for check in contract.get("checks", [])]
    atomic(target / "verification.json", {"checks": pending, "status": "not_run"})
    atomic(target / "acceptance.json", {"status": "pending", "turn": state.get("turn", 1)})
    _refresh_status(path, state)


def finish(path, state):
    target = directory(path, state)
    workspace = state.get("actual_workspace") or state["workspace"]
    before_file = target / "before.json"
    record = {"execution": state["status"], "turn": state.get("turn", 1), "ended_at": time.time(),
              "workspace": workspace}
    if state.get("ownership_error"):
        record.update(ownership_validation="blocked", reason=state["ownership_error"])
    elif before_file.exists():
        try:
            after = snapshot(workspace, target / ".stat-cache.json")
            atomic(target / "after.json", after)
            raw_paths = changed(json.loads(before_file.read_text()), after)
            paths = filter_noise_paths(raw_paths, workspace=workspace, job_dir=path)
            record.update(changed_paths=paths, ownership_validation="passed",
                          ownership_violations=violations(paths, state["acceptance_contract"]["owned_paths"])
                          if state["acceptance_contract"].get("owned_paths") is not None else [])
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            record.update(ownership_validation="blocked", reason=str(error))
    if (not state.get("launch", {}).get("read_only")
            and (state["status"] != "completed" or state.get("report_state") == "missing")):
        delta = workspace_delta(workspace)
        state["workspace_delta"] = delta
        record["workspace_delta"] = delta
    atomic(target / "execution.json", record)
    _refresh_status(path, state)


def workspace_delta(workspace):
    """Best-effort workspace evidence, not an attribution of edits to this worker."""
    result = {"scope": "workspace", "workspace": str(workspace)}

    def git(*args):
        run = subprocess.run(["git", "-C", str(workspace), *args], capture_output=True,
                             text=True, errors="replace", timeout=3)
        if run.returncode:
            raise ValueError(run.stderr.strip()[:500] or "git query failed")
        return run.stdout

    try:
        status = git("status", "--short", "--untracked-files=all")
        # Include staged and unstaged changes together, where a HEAD exists.
        try:
            git("rev-parse", "--verify", "HEAD")
            base = ["HEAD"]
        except ValueError:
            base = ["--cached"]
        stat = git("diff", *base, "--stat", "--no-ext-diff")
        numbers = git("diff", *base, "--numstat", "--no-ext-diff")
        added = deleted = 0
        for line in numbers.splitlines():
            fields = line.split("\t", 2)
            if len(fields) == 3:
                added += int(fields[0]) if fields[0].isdigit() else 0
                deleted += int(fields[1]) if fields[1].isdigit() else 0
        result.update(status_short=status[:16000], diff_stat=stat[:8000],
                      files=len(status.splitlines()), added=added, deleted=deleted,
                      untracked=sum(line.startswith("?? ") for line in status.splitlines()),
                      truncated=len(status) > 16000 or len(stat) > 8000)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        result["error"] = str(error)[:500]
    return result


def gitignored_paths(workspace) -> set[str]:
    """Return workspace-relative untracked paths ignored by Git or recorded in linked-deps.json."""
    workspace_path = Path(workspace)
    timeout = config.launch_setting("git_timeout", 30)
    ignored = set()
    try:
        result = subprocess.run(
            ["git", "-C", str(workspace_path), "ls-files", "-z", "--others", "-i", "--exclude-standard"],
            capture_output=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise ValueError(
            f"git ls-files timed out after {timeout} s in {workspace_path} "
            f"(load {os.getloadavg()[0]:.2f}); raise launch.git_timeout in the claive config"
        ) from error
    if result.returncode == 0:
        for raw in result.stdout.split(b"\0"):
            if raw:
                rel = os.fsdecode(raw).rstrip("/")
                if rel:
                    ignored.add(rel)
    linked_file = workspace_path.parent / "linked-deps.json"
    if linked_file.is_file():
        try:
            loaded = json.loads(linked_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            loaded = {}
        if isinstance(loaded, dict):
            for rel in loaded.get("linked") or ():
                if isinstance(rel, str) and rel.strip("/"):
                    ignored.add(rel.strip("/"))
    return ignored


def _is_ignored_path(rel_path: str, ignored: set[str]) -> bool:
    if rel_path in ignored:
        return True
    parts = PurePosixPath(rel_path).parts
    return any("/".join(parts[:idx]) in ignored for idx in range(1, len(parts)))


def _non_ignored_changes(workspace, expected_snap, actual_snap) -> list[str]:
    if expected_snap.get("head") != actual_snap.get("head"):
        return ["HEAD"]
    diffs = changed(expected_snap, actual_snap)
    if not diffs:
        return []
    ignored = gitignored_paths(workspace)
    return [rel for rel in diffs if not _is_ignored_path(rel, ignored)]


def check_freshness(path, state, evidence=None):
    """Explicit on-demand freshness check; never hash the workspace for watch/list."""
    data = evidence if evidence is not None else view(path, state)
    execution = data.get("execution") or {}
    after = directory(path, state) / "after.json"
    if not after.exists():
        return {"status": "unknown", "reason": "no workspace snapshot was retained"}
    try:
        ws = execution.get("workspace") or state.get("actual_workspace") or state["workspace"]
        expected = json.loads(after.read_text())
        actual = snapshot(ws)
        stale = bool(_non_ignored_changes(ws, expected, actual))
        return {"status": "stale" if stale else "fresh",
                "reason": "workspace changed since completion" if stale else "workspace matches completed turn"}
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        return {"status": "unknown", "reason": str(error)}


def pytest_outcome(check, output):
    """A zero exit code alone does not establish skipped-test acceptance."""
    settings = check.get("pytest")
    if settings is None:
        return None
    # Parse the terminal summary rather than relying on a green process return.
    summaries = [line for line in output.splitlines() if re.search(r"\b\d+ passed\b", line)]
    if not summaries:
        return ("blocked", "pytest pass summary unavailable")
    summary = summaries[-1]
    passed = re.search(r"(\d+) passed", summary)
    skipped = re.search(r"(\d+) skipped", summary)
    count = int(passed.group(1)) if passed else 0
    ignored = int(skipped.group(1)) if skipped else 0
    if count < settings.get("min_passed", 1) or ignored > settings.get("max_skipped", 0):
        return ("failed", f"pytest passed {count}, skipped {ignored}; expected >= {settings.get('min_passed', 1)} passed, <= {settings.get('max_skipped', 0)} skipped")
    return None


def view(path, state):
    target = directory(path, state)
    data = {}
    for key in ("execution", "verification", "acceptance", "scope"):
        file = target / f"{key}.json"
        if file.exists():
            data[key] = json.loads(file.read_text())
    data.setdefault("acceptance", {"status": "pending"})
    data.setdefault("verification", {"status": "not_run", "checks": []})
    return data


def settled(path, state):
    target = directory(path, state)
    if state["status"] in {"starting", "running", "cancelling"} or not (target / "execution.json").exists():
        raise ValueError("verification/acceptance requires a settled turn")
    return target


def pending_worktree(state):
    contract = state.get("acceptance_contract", {})
    needs_review = contract.get("checks") or contract.get("owned_paths") is not None
    decision = state.get("evidence", {}).get("acceptance", {}).get("status", "pending")
    return bool(needs_review and decision not in {"accepted", "rejected"})


@contextmanager
def parent_lock(path, state):
    with (directory(path, state) / ".parent.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def aggregate(checks, required):
    outcomes = {item["name"]: item["status"] for item in checks}
    statuses = [outcomes.get(name, "not_run") for name in required] if required else list(outcomes.values())
    for status in ("failed", "blocked", "not_run"):
        if status in statuses:
            return status
    return "passed" if statuses else "not_run"


def serialized(function):
    @wraps(function)
    def locked(path, state, *args, **kwargs):
        settled(path, state)
        with parent_lock(path, state):
            return function(path, state, *args, **kwargs)
    return locked


def _run_check_command(command, cwd, timeout, env=None):
    with tempfile.TemporaryFile() as output_log, tempfile.TemporaryFile() as error_log:
        child = subprocess.Popen(command, cwd=cwd, env=env, stdout=output_log,
                                 stderr=error_log, start_new_session=True, stdin=subprocess.DEVNULL)
        try:
            child.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            procgroup.terminate_tree(child.pid, grace=0.2, reap_exclude={child.pid})
            child.wait()
            raise

        def tail(stream):
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - 8000))
            return stream.read().decode("utf-8", errors="replace")

        return child.returncode, tail(output_log), tail(error_log)


def _cleanup_byproducts(workspace, pre_files, post_files):
    ws = Path(workspace)
    added = sorted(set(post_files) - set(pre_files))
    created_dirs = set()
    for rel in added:
        if not _is_verifier_byproduct(rel):
            continue
        target = ws / rel
        if target.is_symlink() or target.is_file():
            target.unlink(missing_ok=True)
        parent = target.parent
        while parent != ws:
            if parent.name in VERIFIER_BYPRODUCT_DIRS:
                created_dirs.add(parent)
            parent = parent.parent
    for directory in sorted(created_dirs, key=lambda item: len(item.parts), reverse=True):
        try:
            shutil.rmtree(directory)
        except OSError:
            pass


@serialized
def verify(path, state, name=None, status=None, reason=None, environment=None, in_worktree=False):
    target = settled(path, state)
    atomic(target / "acceptance.json", {"status": "pending", "turn": state.get("turn", 1)})
    contract = state.get("acceptance_contract", {})
    checks = contract.get("checks", [])
    required = [item["name"] for item in checks if item.get("required", True)]
    evidence = view(path, state)["verification"]
    records = {item["name"]: item for item in evidence["checks"]}
    workspace = state.get("actual_workspace") or state["workspace"]
    stat_cache = target / ".stat-cache.json"
    after_file = target / "after.json"
    after_snap = json.loads(after_file.read_text()) if after_file.exists() else None
    if after_snap is not None and _non_ignored_changes(workspace, after_snap, snapshot(workspace, stat_cache)):
        raise ValueError("workspace changed since execution; verification requires a fresh turn")
    check_mutated = False
    if name:
        if name not in {item["name"] for item in checks}:
            raise ValueError("check must be declared in the launch manifest")
        if status not in OUTCOMES or not str(reason or "").strip() or not str(environment or "").strip():
            raise ValueError("external evidence requires --status, --reason and --environment")
        records[name] = dict(name=name, status=status, reason=reason, environment=environment, observed_at=time.time(), source="parent")
    else:
        if status or reason or environment:
            raise ValueError("--status, --reason and --environment require --check")
        clean_env = {key: value for key, value in os.environ.items() if key != "CLAIVE_WORKER_ID"}
        for check in checks:
            record = dict(name=check["name"], command=check["command"], observed_at=time.time(), source="verifier")
            run_in_place = bool(in_worktree or contract.get("verify_in_worktree") or check.get("in_worktree"))
            try:
                if run_in_place:
                    pre_snap = snapshot(workspace, stat_cache)
                    record["environment"] = {"workspace": workspace, "mode": "in_worktree",
                                             "platform": os.uname().sysname}
                    code, output, errors = _run_check_command(
                        check["command"], workspace, check.get("timeout", 300), env=clean_env)
                    post_snap = snapshot(workspace, stat_cache)
                    _cleanup_byproducts(workspace, pre_snap["files"], post_snap["files"])
                    clean_snap = snapshot(workspace, stat_cache)
                    raw_mutated = changed(pre_snap, clean_snap)
                    if raw_mutated:
                        ignored = gitignored_paths(workspace)
                        mutated = [rel for rel in raw_mutated if not _is_ignored_path(rel, ignored)]
                    else:
                        mutated = []
                    if mutated:
                        check_mutated = True
                        record.update(status="failed", exit_code=code, stdout=output, stderr=errors,
                                      mutated_files=mutated[:20],
                                      reason="check mutated workspace files: " + ", ".join(mutated[:10]))
                    else:
                        record.update(status="passed" if code == 0 else "failed", exit_code=code,
                                      stdout=output, stderr=errors, reason=f"exit {code}")
                        if code == 0:
                            verdict = pytest_outcome(check, output + "\n" + errors)
                            if verdict:
                                record.update(status=verdict[0], reason=verdict[1])
                else:
                    with tempfile.TemporaryDirectory(prefix="claive-verify-") as scratch:
                        copy = Path(scratch) / "workspace"
                        ignored = shutil.ignore_patterns(".git", "__pycache__", ".venv", "venv", "node_modules",
                                                         ".tox", ".nox", ".pytest_cache", ".mypy_cache",
                                                         ".ruff_cache", "build", "dist")
                        shutil.copytree(workspace, copy, symlinks=True, ignore=ignored)
                        # Never dereference a source symlink into the original checkout or an external path.
                        # Relative links internal to the scratch copy remain functional.
                        for link in copy.rglob("*"):
                            if link.is_symlink() and not (link.parent / os.readlink(link)).resolve().is_relative_to(copy.resolve()):
                                raise ValueError(f"unsafe verification symlink: {link.relative_to(copy)}")
                        record["environment"] = {"workspace": workspace, "scratch": str(copy),
                                                 "mode": "scratch", "platform": os.uname().sysname}
                        code, output, errors = _run_check_command(
                            check["command"], copy, check.get("timeout", 300), env=clean_env)
                        record.update(status="passed" if code == 0 else "failed", exit_code=code,
                                      stdout=output, stderr=errors, reason=f"exit {code}")
                        if code == 0:
                            verdict = pytest_outcome(check, output + "\n" + errors)
                            if verdict:
                                record.update(status=verdict[0], reason=verdict[1])
            except (OSError, ValueError, subprocess.TimeoutExpired) as error:
                record.update(status="blocked", reason=str(error), environment=record.get("environment", {"workspace": workspace}))
            records[check["name"]] = record
    if not check_mutated and after_snap is not None and _non_ignored_changes(workspace, after_snap, snapshot(workspace, stat_cache)):
        raise ValueError("workspace changed during verification; results are stale")
    evidence = {"checks": list(records.values()), "status": aggregate(list(records.values()), required)}
    atomic(target / "verification.json", evidence)
    atomic(target / "acceptance.json", {"status": "pending", "turn": state.get("turn", 1)})
    _refresh_status(path, state)
    return evidence


@serialized
def scope(path, state, paths, reason):
    target = settled(path, state)
    if not str(reason or "").strip():
        raise ValueError("scope changes require --reason")
    if state.get("acceptance_contract", {}).get("owned_paths") is None:
        raise ValueError("worker has no ownership contract")
    atomic(target / "scope.json", {"owned_paths": owned_paths(paths), "reason": reason, "source": "parent", "changed_at": time.time()})
    atomic(target / "acceptance.json", {"status": "pending", "turn": state.get("turn", 1)})
    _refresh_status(path, state)


@serialized
def decide(path, state, reject=False, reason=None, exception=None):
    target = settled(path, state)
    if exception is not None and not str(exception).strip():
        raise ValueError("exceptions require a nonblank explanation")
    data = view(path, state)
    execution = data["execution"]
    if not reject:
        if execution["execution"] != "completed":
            raise ValueError("only normally completed execution can be accepted")
        if state.get("report_contract") and (state.get("report_state") != "ok" or (state.get("report") or {}).get("status") != "done"):
            raise ValueError("worker report is missing, invalid, or requires parent action")
        contract = state.get("acceptance_contract", {})
        if execution.get("ownership_validation") == "blocked":
            raise ValueError("ownership/evidence validation blocked: " + execution.get("reason", "unknown"))
        if (contract.get("owned_paths") is not None or contract.get("checks")) and not (target / "after.json").exists():
            raise ValueError("ownership/evidence snapshot missing; acceptance remains pending")
        if (target / "after.json").exists():
            after = json.loads((target / "after.json").read_text())
            if _non_ignored_changes(execution["workspace"], after, snapshot(execution["workspace"], target / ".stat-cache.json")):
                raise ValueError("workspace changed since execution; verification evidence is stale")
            allowlist = data.get("scope", {}).get("owned_paths", contract.get("owned_paths"))
            outside = violations(execution.get("changed_paths", []), allowlist) if allowlist is not None else []
            if outside:
                raise ValueError("write ownership violation; explicit scope change required: " + ", ".join(outside))
        checks = data["verification"]["checks"]
        required = [item["name"] for item in contract.get("checks", []) if item.get("required", True)]
        status = aggregate(checks, required)
        if required and status == "failed":
            raise ValueError("required verification failed")
        if required and status != "passed" and not exception:
            raise ValueError("required verification " + status + "; acceptance remains pending")
    if reject and not str(reason or "").strip():
        raise ValueError("rejection requires --reason")
    record = dict(status="rejected" if reject else "accepted", turn=state.get("turn", 1),
                  reason=reason, exception=exception, source="parent", decided_at=time.time())
    atomic(target / "acceptance.json", record)
    _refresh_status(path, state)
    return record
