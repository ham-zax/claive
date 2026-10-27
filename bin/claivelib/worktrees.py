"""Append-only record of Claive-created worktrees."""
import fcntl
import json
import os
from pathlib import Path
import time


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
