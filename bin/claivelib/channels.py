"""Parent-opened worker channels with bounded peer delivery."""
from contextlib import contextmanager
import datetime
import fcntl
import json
import os
import re
import time

from claivelib.acceptance import atomic


NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,31}\Z")


def _directory(name):
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ValueError("channel name must match [a-z0-9][a-z0-9-]{0,31}")
    from claivelib.cli import root
    return root() / "channels" / name


@contextmanager
def _lock(directory):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(directory / ".channel.lock", os.O_WRONLY | os.O_CREAT, 0o600)
    with os.fdopen(fd, "w") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def _read(directory):
    file = directory / "channel.json"
    if not file.is_file():
        raise ValueError(f"unknown channel {directory.name}")
    record = json.loads(file.read_text())
    if not isinstance(record, dict) or record.get("name") != directory.name:
        raise ValueError(f"invalid channel record: {file}")
    return record


def _write(directory, record):
    atomic(directory / "channel.json", record)


def _timestamp(epoch):
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _summary(directory, record):
    file = directory / "summary.json"
    if file.is_file():
        return json.loads(file.read_text())
    opened = record.get("opened_at", time.time())
    closed = record.get("closed_at", time.time())
    per_pair = record.get("counts", {}).get("per_pair", {})
    messages = []
    for pair, count in sorted(per_pair.items()):
        sender, separator, target = pair.partition("->")
        if separator and count:
            messages.append({"from": sender, "to": target, "count": count})
    return dict(name=record["name"], members=record.get("members", []),
                messages=messages, counts=record.get("counts", {"total": 0, "per_pair": {}}),
                close_reason=record.get("close_reason"), opened_at=opened,
                closed_at=closed, duration=max(0, closed - opened))


def _close_locked(directory, record, reason, now=None):
    if record.get("status") == "closed":
        return record, _summary(directory, record), False
    now = time.time() if now is None else now
    record.update(status="closed", close_reason=reason, closed_at=now)
    summary = _summary(directory, record)
    _write(directory, record)
    atomic(directory / "summary.json", summary)
    from claivelib import inbox
    inbox.append(dict(type="channel_closed", kind="channel_closed", id=record["name"], channel=record["name"],
                      at=_timestamp(now), status="closed", summary=summary))
    return record, summary, True


def _expire_locked(directory, record, now=None):
    now = time.time() if now is None else now
    if record.get("status") == "open" and now >= record.get("deadline", float("inf")):
        return _close_locked(directory, record, "timeout", now)
    return record, None, False


def open_channel(name, members, max_messages=20, max_per_pair=6, timeout=900):
    directory = _directory(name)
    with _lock(directory):
        file = directory / "channel.json"
        if file.exists():
            existing = _read(directory)
            if existing.get("status") == "open":
                raise ValueError(f"channel {name} is already open")
            raise ValueError(f"channel {name} already exists and is closed")
    if not isinstance(members, (list, tuple)) or not 2 <= len(members) <= 4:
        raise ValueError("a channel needs 2 to 4 worker members")
    for value, label in ((max_messages, "--max-messages"), (max_per_pair, "--max-per-pair"),
                         (timeout, "--timeout")):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{label} must be a positive integer")

    from claivelib import cli
    paths = [cli.job_path(member) for member in members]
    ids = [path.name for path in paths]
    if len(set(ids)) != len(ids):
        raise ValueError("channel members must be unique workers")
    for path in paths:
        state = cli.load(path)
        if not state.get("reusable") or state.get("status") not in cli.ACTIVE:
            raise ValueError(f"worker {path.name} must be an active reusable worker to join a channel")

    with _lock(directory):
        file = directory / "channel.json"
        if file.exists():
            existing = _read(directory)
            if existing.get("status") == "open":
                raise ValueError(f"channel {name} is already open")
            raise ValueError(f"channel {name} already exists and is closed")
        now = time.time()
        record = dict(name=name, members=ids,
                      limits={"max_messages": max_messages, "max_per_pair": max_per_pair,
                              "timeout": timeout},
                      opened_at=now, deadline=now + timeout, status="open", close_reason=None,
                      counts={"total": 0, "per_pair": {}})
        _write(directory, record)
    return record


def show_channel(name):
    directory = _directory(name)
    with _lock(directory):
        record = _read(directory)
        record, _, _ = _expire_locked(directory, record)
        summary = _summary(directory, record) if record.get("status") == "closed" else None
    return record, summary


def close_channel(name, reason="parent"):
    directory = _directory(name)
    with _lock(directory):
        record = _read(directory)
        record, summary, _ = _close_locked(directory, record, reason)
    return record, summary


def _pair(sender, target):
    return f"{sender}->{target}"


def _append_transcript(directory, identifier, sender, target, mode, at, message):
    record = dict(id=identifier, **{"from": sender}, to=target, mode=mode, at=at,
                  bytes=len(message.encode("utf-8")))
    file = directory / "transcript.jsonl"
    file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    line = (json.dumps(record, ensure_ascii=True) + "\n").encode()
    fd = os.open(file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


def send_peer(target_path, message, mode="note", identifier=None):
    """Send to a shared member, enforcing one channel's budget under its lock."""
    from claivelib import cli, messaging

    if mode not in {"note", "wake"}:
        raise ValueError("workers can send only note or wake to a peer")
    messaging.validate_message(message)
    messaging._worker_for_note_or_wake(target_path)
    sender = cli.job_path(os.environ.get("CLAIVE_WORKER_ID", "")).name
    target = target_path.name
    base = cli.root() / "channels"
    if base.is_dir():
        files = sorted(base.glob("*/channel.json"), key=lambda file: file.parent.name)
    else:
        files = []
    for file in files:
        directory = file.parent
        record = json.loads(file.read_text())
        if (record.get("status") != "open"
                or sender not in record.get("members", [])
                or target not in record.get("members", [])):
            continue
        with _lock(directory):
            record = _read(directory)
            if (record.get("status") != "open"
                    or sender not in record.get("members", [])
                    or target not in record.get("members", [])):
                continue
            record, _, expired = _expire_locked(directory, record)
            if expired:
                raise ValueError(f"channel {record['name']} closed: timeout")
            pair = _pair(sender, target)

            def check_budget():
                nonlocal record
                record, _, expired = _expire_locked(directory, record)
                if expired:
                    raise ValueError(f"channel {record['name']} closed: timeout")
                counts = record.setdefault("counts", {"total": 0, "per_pair": {}})
                per_pair = counts.setdefault("per_pair", {})
                if counts.get("total", 0) >= record["limits"]["max_messages"]:
                    record, _, _ = _close_locked(directory, record, "max-messages")
                    raise ValueError(f"channel {record['name']} closed: max-messages")
                if per_pair.get(pair, 0) >= record["limits"]["max_per_pair"]:
                    record, _, _ = _close_locked(directory, record, "max-per-pair")
                    raise ValueError(f"channel {record['name']} closed: max-per-pair")

            receipt, duplicate = messaging.send(target_path, message, mode, identifier,
                                                channel=record["name"],
                                                before_delivery=check_budget)
            if not duplicate:
                at = time.time()
                counts = record.setdefault("counts", {"total": 0, "per_pair": {}})
                per_pair = counts.setdefault("per_pair", {})
                counts["total"] = counts.get("total", 0) + 1
                per_pair[pair] = per_pair.get(pair, 0) + 1
                _write(directory, record)
                _append_transcript(directory, receipt["id"], sender, target, mode, at, message)
            return receipt, duplicate
    raise ValueError(f"worker {target} is not in an open channel with you; report to the parent, "
                     "who can open one with claive channel open")
