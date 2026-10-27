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
import shutil
import signal
import subprocess
import tempfile
import time

from claivelib import config

OUTCOMES = {"passed", "failed", "blocked", "not_run"}


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


def validate_manifest(value):
    if not isinstance(value, list):
        raise ValueError("checks manifest must be a list")
    names = set()
    for check in value:
        if not isinstance(check, dict) or not isinstance(check.get("name"), str) or not check["name"]:
            raise ValueError("each check needs a nonempty name")
        if check["name"] in names:
            raise ValueError("duplicate check name")
        names.add(check["name"])
        command = check.get("command")
        if not isinstance(command, list) or not command or any(not isinstance(x, str) or not x for x in command):
            raise ValueError("check command must be a nonempty argv list")
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
    return value


def snapshot(workspace):
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
    files = {}
    for raw in set(result.stdout.split(b"\0")) - {b""}:
        name = os.fsdecode(raw)
        path = workspace / name
        if path.is_symlink():
            files[name] = ["symlink", os.readlink(path)]
        elif path.is_file():
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            files[name] = ["file", digest.hexdigest(), path.stat().st_mode & 0o777]
        elif path.is_dir():
            # A submodule is a tracked directory: bind its commit and dirtiness.
            sub = git(path, "status", "--porcelain", capture_output=True)
            commit = git(path, "rev-parse", "HEAD", capture_output=True)
            files[name] = ["directory", commit.stdout.decode(errors="replace"), sub.stdout.decode(errors="replace")]
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
    state.pop("ownership_error", None)
    if contract.get("owned_paths") is not None or contract.get("checks"):
        try:
            atomic(target / "before.json", snapshot(workspace))
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            state["ownership_error"] = str(error)
    # Never carry verification or an accepted decision into another turn.
    pending = [dict(name=check["name"], status="not_run", reason="not yet executed", environment=None)
               for check in contract.get("checks", [])]
    atomic(target / "verification.json", {"checks": pending, "status": "not_run"})
    atomic(target / "acceptance.json", {"status": "pending", "turn": state.get("turn", 1)})


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
            after = snapshot(workspace)
            atomic(target / "after.json", after)
            paths = changed(json.loads(before_file.read_text()), after)
            record.update(changed_paths=paths, ownership_validation="passed",
                          ownership_violations=violations(paths, state["acceptance_contract"]["owned_paths"])
                          if state["acceptance_contract"].get("owned_paths") is not None else [])
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            record.update(ownership_validation="blocked", reason=str(error))
    atomic(target / "execution.json", record)


def check_freshness(path, state, evidence=None):
    """Explicit on-demand freshness check; never hash the workspace for watch/list."""
    data = evidence if evidence is not None else view(path, state)
    execution = data.get("execution") or {}
    after = directory(path, state) / "after.json"
    if not after.exists():
        return {"status": "unknown", "reason": "no workspace snapshot was retained"}
    try:
        expected = json.loads(after.read_text())
        actual = snapshot(execution.get("workspace") or state.get("actual_workspace") or state["workspace"])
        return {"status": "fresh" if actual == expected else "stale",
                "reason": "workspace matches completed turn" if actual == expected else "workspace changed since completion"}
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


@serialized
def verify(path, state, name=None, status=None, reason=None, environment=None):
    target = settled(path, state)
    atomic(target / "acceptance.json", {"status": "pending", "turn": state.get("turn", 1)})
    checks = state.get("acceptance_contract", {}).get("checks", [])
    required = [item["name"] for item in checks if item.get("required", True)]
    evidence = view(path, state)["verification"]
    records = {item["name"]: item for item in evidence["checks"]}
    workspace = state.get("actual_workspace") or state["workspace"]
    after_file = target / "after.json"
    if after_file.exists() and snapshot(workspace) != json.loads(after_file.read_text()):
        raise ValueError("workspace changed since execution; verification requires a fresh turn")
    if name:
        if name not in {item["name"] for item in checks}:
            raise ValueError("check must be declared in the launch manifest")
        if status not in OUTCOMES or not str(reason or "").strip() or not str(environment or "").strip():
            raise ValueError("external evidence requires --status, --reason and --environment")
        records[name] = dict(name=name, status=status, reason=reason, environment=environment, observed_at=time.time(), source="parent")
    else:
        if status or reason or environment:
            raise ValueError("--status, --reason and --environment require --check")
        workspace = state.get("actual_workspace") or state["workspace"]
        for check in checks:
            record = dict(name=check["name"], command=check["command"], observed_at=time.time(), source="verifier")
            try:
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
                    record["environment"] = {"workspace": workspace, "scratch": str(copy), "platform": os.uname().sysname}
                    with tempfile.TemporaryFile() as output_log, tempfile.TemporaryFile() as error_log:
                        child = subprocess.Popen(check["command"], cwd=copy, stdout=output_log,
                                                 stderr=error_log, start_new_session=True, stdin=subprocess.DEVNULL)
                        try:
                            child.wait(timeout=check.get("timeout", 300))
                        except subprocess.TimeoutExpired:
                            try:
                                os.killpg(child.pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                            child.wait()
                            raise
                        def tail(stream):
                            stream.seek(0, os.SEEK_END)
                            stream.seek(max(0, stream.tell() - 8000))
                            return stream.read().decode("utf-8", errors="replace")
                        output, errors = tail(output_log), tail(error_log)
                    record.update(status="passed" if child.returncode == 0 else "failed", exit_code=child.returncode,
                                  stdout=output, stderr=errors, reason=f"exit {child.returncode}")
                    if child.returncode == 0:
                        verdict = pytest_outcome(check, output + "\n" + errors)
                        if verdict:
                            record.update(status=verdict[0], reason=verdict[1])
            except (OSError, ValueError, subprocess.TimeoutExpired) as error:
                record.update(status="blocked", reason=str(error), environment=record.get("environment", {"workspace": workspace}))
            records[check["name"]] = record
    if after_file.exists() and snapshot(workspace) != json.loads(after_file.read_text()):
        raise ValueError("workspace changed during verification; results are stale")
    evidence = {"checks": list(records.values()), "status": aggregate(list(records.values()), required)}
    atomic(target / "verification.json", evidence)
    atomic(target / "acceptance.json", {"status": "pending", "turn": state.get("turn", 1)})
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
            if snapshot(execution["workspace"]) != after:
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
    return record
