"""Append-only record of Claive-created worktrees."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import subprocess
import time


@contextmanager
def lifecycle_lock():
    """Serialize managed-worktree creation, reuse, and removal."""
    from claivelib import cli
    path = cli.root() / "worktrees.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "r+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def record(event, **fields):
    from claivelib import cli
    path = cli.root() / "worktrees.jsonl"
    item = dict(event=event, at=time.time(), **fields)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "w") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        os.fchmod(stream.fileno(), 0o600)
        stream.write(json.dumps(item, ensure_ascii=True) + "\n")
        stream.flush()


def record_isolation(event, job, isolation, reason=None):
    fields = dict(job=job, path=isolation["existing_path"], branch=isolation.get("branch"),
                  source=isolation["source"], base_commit=isolation["base_commit"])
    if reason is not None:
        fields["reason"] = reason
    record(event, **fields)


def current():
    from claivelib import cli
    path = cli.root() / "worktrees.jsonl"
    if not path.exists():
        return []
    latest = {}
    with path.open() as stream:
        fcntl.flock(stream, fcntl.LOCK_SH)
        for line in stream:
            try:
                item = json.loads(line)
                if isinstance(item, dict) and isinstance(item.get("path"), str):
                    latest[item["path"]] = item
            except ValueError:
                continue
    return [item for item in latest.values()
            if item.get("event") not in {"removed", "cleaned"} and Path(item["path"]).exists()]


def managed_source(target):
    """Allow reuse through its recorded source only when Git confirms the relationship."""
    target = Path(target).resolve()
    for item in current():
        if Path(item["path"]).resolve() != target or not item.get("source"):
            continue
        roots = []
        for workspace in (target, Path(item["source"])):
            try:
                result = subprocess.run(["git", "-C", str(workspace), "rev-parse", "--git-common-dir"],
                                        capture_output=True, text=True, timeout=3)
            except (OSError, subprocess.SubprocessError):
                return None
            if result.returncode:
                return None
            roots.append((workspace / result.stdout.strip()).resolve())
        if roots[0] == roots[1]:
            return item["source"]
    return None


def active_batch_user(target):
    """Protect lane trees between stage workers, when no worker is active."""
    from claivelib import cli
    target = Path(target).resolve()
    for path in (cli.root() / "batches").glob("*/state.json"):
        try:
            state = json.loads(path.read_text())
            if state.get("status") != "running":
                continue
            for lane in state.get("lanes", []):
                for workspace in lane.get("workspaces", {}).values():
                    if workspace.get("path") and Path(workspace["path"]).resolve() == target:
                        return state["id"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return None


def size(path):
    total = 0
    for directory, dirs, files in os.walk(path, followlinks=False):
        dirs[:] = [name for name in dirs if not (Path(directory) / name).is_symlink()]
        for name in files:
            item = Path(directory) / name
            try:
                if not item.is_symlink():
                    total += item.stat().st_size
            except OSError:
                pass
    return total
