"""Durable scheduled wakes and their pure time calculations."""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import math
import os
import re
import time
import uuid

from claivelib.acceptance import atomic


# Schedule parsing and missed-occurrence behavior are adapted from lop's
# local_operator/harness/wake.py.
# Copyright (c) 2024 Radient Inc. Licensed under the MIT License:
# Permission is hereby granted, free of charge, to any person obtaining a copy of this
# software and associated documentation files (the "Software"), to deal in the Software
# without restriction, including without limitation the rights to use, copy, modify,
# merge, publish, distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to the following
# conditions: The above copyright notice and this permission notice shall be included
# in all copies or substantial portions of the Software. THE SOFTWARE IS PROVIDED "AS
# IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED.


_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_DURATION = re.compile(r"\s*(?:\d+\s*[smhdwSMHDW]\s*)+\Z")
_TERM = re.compile(r"(\d+)\s*([smhdwSMHDW])")
_SCHEDULE_ID = re.compile(r"[0-9a-f]{32}\Z")
MIN_INTERVAL = 60
MAX_SCHEDULES = 16
PAST_GRACE = 5


def parse_duration(text):
    """Parse a positive unit-suffixed duration; bare numbers are ambiguous."""
    if not isinstance(text, str) or not _DURATION.fullmatch(text):
        return None
    seconds = sum(int(amount) * _UNITS[unit.lower()] for amount, unit in _TERM.findall(text))
    return seconds if seconds > 0 else None


def parse_at(text, now=None):
    """Parse ``+duration`` or an ISO-8601 timestamp to epoch seconds."""
    if not isinstance(text, str) or not text.strip():
        return None
    now = time.time() if now is None else now
    value = text.strip()
    if value.startswith("+"):
        duration = parse_duration(value[1:])
        return now + duration if duration is not None else None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.timestamp()


def advance(next_at, every, now):
    """Return (next occurrence, skipped occurrences) after one due delivery."""
    if every is None:
        return None, 0
    periods = max(0, math.floor((now - next_at) / every))
    return next_at + (periods + 1) * every, periods


def format_time(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def format_duration(seconds):
    remainder = seconds
    parts = []
    for unit, size in (("w", _UNITS["w"]), ("d", _UNITS["d"]), ("h", _UNITS["h"]),
                       ("m", _UNITS["m"]), ("s", _UNITS["s"])):
        count, remainder = divmod(remainder, size)
        if count:
            parts.append(f"{count}{unit}")
    return "".join(parts) or "0s"


def _directory(path):
    return path / "messages" / "schedules"


@contextmanager
def _lock(path):
    file = path / ".schedules.lock"
    file.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(file, os.O_WRONLY | os.O_CREAT, 0o600)
    with os.fdopen(fd, "w") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def _file(path, identifier):
    if not isinstance(identifier, str) or not _SCHEDULE_ID.fullmatch(identifier):
        raise ValueError("schedule ID must be exactly 32 lowercase hexadecimal characters")
    return _directory(path) / f"{identifier}.json"


def _read(file):
    record = json.loads(file.read_text())
    if not isinstance(record, dict):
        raise ValueError(f"invalid wake schedule record: {file}")
    identifier = record.get("id")
    next_at = record.get("next_at")
    every = record.get("every")
    status = record.get("status")
    if (not isinstance(identifier, str) or not _SCHEDULE_ID.fullmatch(identifier)
            or not isinstance(record.get("message"), str)
            or not isinstance(next_at, (int, float)) or isinstance(next_at, bool)
            or not math.isfinite(next_at)
            or every is not None and (not isinstance(every, int) or isinstance(every, bool)
                                      or every < MIN_INTERVAL)
            or not isinstance(status, str) or status not in {"scheduled", "delivered", "cancelled"}):
        raise ValueError(f"invalid wake schedule record: {file}")
    return record


def _records(path):
    directory = _directory(path)
    if not directory.is_dir():
        return []
    return [_read(file) for file in sorted(directory.glob("*.json"))]


def create(path, message, at, every=None, identifier=None):
    """Store a scheduled wake for an active reusable worker."""
    from claivelib import messaging

    messaging.validate_message(message)
    messaging._worker_for_note_or_wake(path)
    now = time.time()
    next_at = parse_at(at, now)
    if next_at is None:
        raise ValueError("--at must be an ISO-8601 timestamp or +duration such as +30m")
    if next_at < now - PAST_GRACE:
        raise ValueError("--at time is in the past")
    next_at = max(now, next_at)
    interval = None
    if every is not None:
        interval = parse_duration(every)
        if interval is None:
            raise ValueError("--every must be a positive unit-suffixed duration such as 1h")
        if interval < MIN_INTERVAL:
            raise ValueError(f"--every must be at least {MIN_INTERVAL}s")
    if identifier is None:
        identifier = uuid.uuid4().hex
    file = _file(path, identifier)
    record = dict(id=identifier, message=message, created_at=now, next_at=next_at,
                  every=interval, status="scheduled", fired_count=0, skipped_count=0)
    with _lock(path):
        existing = _records(path)
        if any(item["id"] == identifier for item in existing):
            raise ValueError(f"schedule {identifier} already exists")
        active = sum(item["status"] == "scheduled" for item in existing)
        if active >= MAX_SCHEDULES:
            raise ValueError(f"worker already has {MAX_SCHEDULES} scheduled wakes")
        atomic(file, record)
    return record


def list_schedules(path):
    with _lock(path):
        records = _records(path)
    return sorted(records, key=lambda item: (item["next_at"], item["id"]))


def cancel(path, identifier):
    file = _file(path, identifier)
    with _lock(path):
        if not file.is_file():
            raise ValueError(f"no scheduled wake {identifier} for this worker")
        record = _read(file)
        if record["status"] != "scheduled":
            raise ValueError(f"scheduled wake {identifier} is {record['status']}")
        record.update(status="cancelled", cancelled_at=time.time())
        atomic(file, record)
    return record


def deliver_due(path, now=None):
    """Queue due wake messages once, skipping missed recurrence intervals."""
    from claivelib import cli, messaging

    directory = _directory(path)
    if not directory.is_dir() or not any(directory.glob("*.json")):
        return {"delivered": [], "errors": []}
    now = time.time() if now is None else now
    delivered, errors = [], []
    with _lock(path):
        for file in sorted(directory.glob("*.json")):
            try:
                record = _read(file)
            except (OSError, ValueError, TypeError) as error:
                errors.append(f"{file.name}: {error}")
                continue
            if record["status"] != "scheduled" or record["next_at"] > now:
                continue
            try:
                state = cli.load(path, check_alive=False)
            except (OSError, ValueError) as error:
                errors.append(f"{record['id']}: {error}")
                continue
            if (not state.get("reusable") or state.get("status") not in cli.ACTIVE
                    or state.get("needs_parent") or (path / "close.request").exists()):
                continue

            delivery_id = record.get("delivery_id")
            if delivery_id is None:
                delivery_id = uuid.uuid4().hex
                record["delivery_id"] = delivery_id
                atomic(file, record)
            try:
                receipt, _duplicate = messaging.send(
                    path, record["message"], "wake", delivery_id, kind="scheduled_wake")
            except (OSError, ValueError) as error:
                errors.append(f"{record['id']}: {error}")
                continue

            record["fired_count"] = record.get("fired_count", 0) + 1
            record["last_delivery_id"] = delivery_id
            record["last_delivered_at"] = now
            record.pop("delivery_id", None)
            next_at, skipped = advance(record["next_at"], record.get("every"), now)
            record["skipped_count"] = record.get("skipped_count", 0) + skipped
            if next_at is None:
                record.update(status="delivered", receipt_id=receipt["id"])
            else:
                record["next_at"] = next_at
                record["last_receipt_id"] = receipt["id"]
            atomic(file, record)
            delivered.append(dict(schedule_id=record["id"], receipt_id=receipt["id"],
                                  status=receipt.get("status")))
    return {"delivered": delivered, "errors": errors}


def deliver_all(root=None):
    """Deliver due schedules from every worker directory, for ``claive serve``."""
    if root is None:
        from claivelib import cli
        root = cli.root()
    delivered, errors = [], []
    if not root.is_dir():
        return {"delivered": delivered, "errors": errors}
    for path in sorted(root.iterdir()):
        if not re.fullmatch(r"[0-9a-f]{12}", path.name):
            continue
        if not (path / "state.json").is_file():
            continue
        result = deliver_due(path)
        delivered.extend(dict(worker=path.name, **item) for item in result["delivered"])
        errors.extend(dict(worker=path.name, error=error) for error in result["errors"])
    return {"delivered": delivered, "errors": errors}
