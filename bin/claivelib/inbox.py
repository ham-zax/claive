"""Completion inbox: append-only events with per-consumer cursors."""
import datetime
import fcntl
import json
import os
import re
import time

CONSUMER_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")


def _root():
    from claivelib.cli import root
    return root()


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def turn_event(job_id, label, turn, status, code, failure_kind, needs_parent,
               report_status, batch, mission):
    return dict(type="turn", seq=time.time_ns(), at=_now_iso(), id=job_id,
                label=label, turn=turn, status=status, code=code,
                failure_kind=failure_kind, needs_parent=needs_parent,
                report_status=report_status, batch=batch, mission=mission)


def batch_event(batch_id, label, status, code, mission):
    return dict(type="batch", seq=time.time_ns(), at=_now_iso(), id=batch_id,
                label=label, status=status, code=code, mission=mission)


def append(event):
    line = (json.dumps(event, ensure_ascii=True) + "\n").encode()
    path = _root() / "inbox.jsonl"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)
    notify(event)


def notify(event):
    cmd = os.environ.get("CLAIVE_NOTIFY_CMD")
    if not cmd:
        return
    try:
        import subprocess
        env = dict(os.environ, CLAIVE_EVENT_ID=str(event.get("id", "")),
                   CLAIVE_EVENT_STATUS=str(event.get("status", "")),
                   CLAIVE_EVENT_CODE=str(event.get("code", "")))
        proc = subprocess.Popen(["/bin/sh", "-c", cmd], stdin=subprocess.PIPE,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True, env=env)
        try:
            proc.stdin.write((json.dumps(event) + "\n").encode())
            proc.stdin.close()
        except Exception:
            pass
    except Exception:
        pass


def read_new(consumer="default", peek=False):
    if not CONSUMER_RE.fullmatch(consumer or ""):
        raise ValueError(f"invalid consumer: {consumer}")
    root = _root()
    inbox = root / "inbox.jsonl"
    cursors = root / "inbox-cursors"
    cursors.mkdir(parents=True, exist_ok=True, mode=0o700)
    cursor_file = cursors / consumer
    # One reader per consumer at a time, and the cursor is replaced atomically: a torn or
    # concurrent cursor would read as 0 and replay the whole inbox.
    with (cursors / f".{consumer}.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _read_from(inbox, cursor_file, peek)


def _read_from(inbox, cursor_file, peek):
    try:
        start = int(cursor_file.read_text().strip() or 0)
    except (OSError, ValueError):
        start = 0
    if not inbox.exists():
        if not peek:
            _save_cursor(cursor_file, 0)
        return []
    size = inbox.stat().st_size
    if start > size:
        start = 0
    with inbox.open("rb") as stream:
        stream.seek(start)
        data = stream.read(size - start)
    if data and not data.endswith(b"\n"):
        cut = data.rfind(b"\n")
        if cut == -1:
            data, end = b"", start
        else:
            data, end = data[:cut + 1], start + cut + 1
    else:
        end = size
    events = []
    for line in data.decode("utf-8", errors="replace").splitlines():
        if line.strip():
            events.append(json.loads(line))
    if not peek:
        _save_cursor(cursor_file, end)
    return events


def _save_cursor(cursor_file, offset):
    temporary = cursor_file.with_name(f".{cursor_file.name}.{os.getpid()}.tmp")
    temporary.write_text(str(offset))
    os.replace(temporary, cursor_file)


def format_text(events):
    if not events:
        return "No new events"
    lines = []
    for event in events:
        badge = "ASK" if event.get("needs_parent") else str(event.get("status", "")).upper()
        label = event.get("label", "")
        if event.get("type") == "batch":
            line = f"batch {event.get('id')} {badge} code={event.get('code')} {label}"
        elif event.get("type") == "conversation":
            line = f"conversation {event.get('id')} {badge} code={event.get('code')}"
            if event.get("needs_parent"):
                line += f" question: {event['needs_parent'].get('question', '')}"
        elif event.get("type") == "goal":
            line = f"goal {event.get('id')} {badge} {label}"
            if event.get("needs_parent"):
                line += (f" question: {event['needs_parent'].get('question', '')}"
                         f" (claive goal answer {event.get('id')} --message ...)")
        else:
            line = f"{event.get('id')} {badge} code={event.get('code')} {label}"
            if event.get("failure_kind"):
                line += f" kind={event['failure_kind']}"
            if event.get("needs_parent"):
                line += f" question: {event['needs_parent'].get('question', '')}"
        lines.append(line)
    return "\n".join(lines)
