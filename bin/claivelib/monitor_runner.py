"""Parent-owned monitor persistence, shell checks, and message delivery."""
from contextlib import contextmanager
import fcntl
import json
import logging
import os
from pathlib import Path
import selectors
import signal
import subprocess
import time
import uuid

from claivelib import acceptance, ansi, messaging
from claivelib import monitors as monitor_math


logger = logging.getLogger(__name__)
RUN_TIMEOUT_SECONDS = 60
MAX_OUTPUT_BYTES = 64 * 1024
MAX_ERROR_CHARS = 1000


def _cli():
    from claivelib import cli
    return cli


def _monitor_dir(worker_path):
    return Path(worker_path) / "monitors"


def _monitor_path(worker_path, monitor_id):
    return _monitor_dir(worker_path) / f"{monitor_id}.json"


def _load_monitor(path):
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        logger.warning("monitor record is unreadable: %s", path)
        return None
    valid = (
        isinstance(value, dict)
        and value.get("schema_version") == 1
        and value.get("id") == path.stem
        and value.get("worker_id") == path.parent.parent.name
        and isinstance(value.get("command"), str) and bool(value["command"].strip())
        and isinstance(value.get("label"), str)
        and isinstance(value.get("workspace"), str)
        and isinstance(value.get("interval_seconds"), int)
        and not isinstance(value.get("interval_seconds"), bool)
        and monitor_math.MIN_INTERVAL_SECONDS <= value["interval_seconds"] <= monitor_math.MAX_INTERVAL_SECONDS
        and isinstance(value.get("next_run_at"), (int, float))
        and not isinstance(value.get("next_run_at"), bool)
        and (value.get("last_snapshot") is None or isinstance(value.get("last_snapshot"), str))
        and (value.get("last_snapshot_hash") is None or isinstance(value.get("last_snapshot_hash"), str))
    )
    if not valid:
        logger.warning("monitor record has an invalid shape: %s", path)
        return None
    return value


def _save_monitor(path, monitor):
    acceptance.atomic(path, monitor)


def _worker_state(worker_id):
    cli = _cli()
    path = cli.job_path(worker_id)
    return path, cli.load(path)


def _closed(path, state):
    cli = _cli()
    return (state.get("phase") == "session closed"
            or state.get("status") not in cli.ACTIVE
            or (Path(path) / "close.request").exists())


def _require_open_reusable(path, state):
    worker_id = state.get("id", Path(path).name)
    if _closed(path, state):
        raise ValueError(f"worker {worker_id} is closed; monitors require an open reusable worker")
    if not state.get("reusable"):
        raise ValueError(f"worker {worker_id} is not reusable; monitors require an open reusable worker")


def add(worker_id, command, every, label=None):
    """Persist one read-only shell monitor on an open reusable worker."""
    if not isinstance(command, str) or not command.strip():
        raise ValueError("--command must not be empty")
    interval = monitor_math.parse_interval(every)
    path, state = _worker_state(worker_id)
    _require_open_reusable(path, state)
    workspace = Path(state.get("actual_workspace") or state["workspace"])
    if not workspace.is_absolute() or not workspace.is_dir():
        raise ValueError("worker workspace is unavailable")
    name = ansi.strip_controls(str(label or command.strip().splitlines()[0])).strip()
    if not name:
        name = "monitor"
    if len(name) > 120:
        raise ValueError("--label must be at most 120 characters")
    directory = _monitor_dir(path)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    cli = _cli()
    monitor_id = uuid.uuid4().hex[:12]
    while any(_monitor_path(worker_path, monitor_id).exists()
              for worker_path in cli.root().iterdir()):
        monitor_id = uuid.uuid4().hex[:12]
    now = time.time()
    monitor = {
        "schema_version": 1,
        "id": monitor_id,
        "worker_id": state.get("id", path.name),
        "command": command,
        "label": name,
        "workspace": str(workspace),
        "interval_seconds": interval,
        "created_at": now,
        "next_run_at": now,
        "last_snapshot_hash": None,
        "last_snapshot": None,
        "failure_streak": 0,
        "failure_notified": False,
        "last_error": None,
    }
    _save_monitor(_monitor_path(path, monitor_id), monitor)
    return monitor


def list_for(worker_id):
    """List monitor records for one worker, without requiring it to be open."""
    cli = _cli()
    path = cli.job_path(worker_id)
    directory = _monitor_dir(path)
    if not directory.is_dir():
        return []
    rows = []
    for record in sorted(directory.glob("*.json")):
        monitor = _load_monitor(record)
        if monitor is not None:
            rows.append(monitor)
    return rows


def list_all():
    """List monitor records across worker directories in stable ID order."""
    cli = _cli()
    rows = []
    for worker_path in cli.root().iterdir():
        if not (worker_path / "state.json").is_file():
            continue
        directory = _monitor_dir(worker_path)
        if not directory.is_dir():
            continue
        for record in sorted(directory.glob("*.json")):
            monitor = _load_monitor(record)
            if monitor is not None:
                rows.append(monitor)
    return sorted(rows, key=lambda item: (item.get("worker_id", ""), item.get("id", "")))


@contextmanager
def _monitor_lock(path, *, blocking):
    lock_path = path.parent / f".{path.stem}.lock"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "r+") as stream:
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(stream, flags)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def cancel(monitor_id):
    """Remove one monitor, finding its owning worker by the monitor ID."""
    if not isinstance(monitor_id, str) or len(monitor_id) != 12 or any(
            char not in "0123456789abcdef" for char in monitor_id):
        raise ValueError("invalid monitor ID")
    cli = _cli()
    matches = []
    for worker_path in cli.root().iterdir():
        path = _monitor_path(worker_path, monitor_id)
        if (worker_path / "state.json").is_file() and path.is_file():
            matches.append(path)
    if not matches:
        raise ValueError(f"unknown monitor ID: {monitor_id}")
    if len(matches) > 1:
        raise ValueError(f"monitor ID {monitor_id} is ambiguous; inspect worker monitor lists")
    path = matches[0]
    with _monitor_lock(path, blocking=True):
        try:
            path.unlink()
        except FileNotFoundError:
            raise ValueError(f"unknown monitor ID: {monitor_id}") from None
    return monitor_id


def run_command(command, workspace, *, timeout=RUN_TIMEOUT_SECONDS,
                output_limit=MAX_OUTPUT_BYTES):
    """Run a shell check with a wall-clock deadline and a combined output cap."""
    process = subprocess.Popen(
        ["bash", "-c", command], cwd=workspace, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, start_new_session=True,
    )
    selector = selectors.DefaultSelector()
    captured = {"stdout": bytearray(), "stderr": bytearray()}
    total = 0
    seen = 0
    timed_out = False
    try:
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        deadline = time.monotonic() + timeout
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                _terminate_group(process)
                break
            events = selector.select(min(remaining, 0.1))
            for key, _ in events:
                chunk = os.read(key.fileobj.fileno(), 8192)
                if not chunk:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                seen += len(chunk)
                room = max(0, output_limit - total)
                kept = chunk[:room]
                if kept:
                    captured[key.data].extend(kept)
                    total += len(kept)
        if process.poll() is None:
            process.wait(timeout=max(0.1, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_group(process)
    finally:
        selector.close()
        for stream in (process.stdout, process.stderr):
            if stream and not stream.closed:
                stream.close()
        if process.poll() is None:
            _terminate_group(process)
    return {
        "returncode": process.returncode,
        "stdout": bytes(captured["stdout"]),
        "stderr": bytes(captured["stderr"]),
        "timed_out": timed_out,
        "truncated": seen > output_limit,
    }


def _terminate_group(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def _decode(data):
    return ansi.strip_controls(data.decode("utf-8", "replace"))


def _failure_text(monitor, result, exception=None):
    if exception is not None:
        reason = str(exception)
    elif result["timed_out"]:
        reason = f"command timed out after {RUN_TIMEOUT_SECONDS}s"
    else:
        reason = _decode(result["stderr"]).strip() or f"command exited with status {result['returncode']}"
    if result and result.get("truncated"):
        reason += " (output capped at 64 KiB)"
    reason = ansi.strip_controls(" ".join(reason.split()))[:MAX_ERROR_CHARS]
    label = ansi.strip_controls(str(monitor.get("label") or monitor["id"]))
    return ansi.strip_controls(f"Monitor '{label}' ({monitor['id']}) failed: {reason}")


def _send(path, message, send):
    cli = _cli()
    latest = cli.load(path)
    _require_open_reusable(path, latest)
    mode = "wake" if latest["status"] == "idle" and not latest.get("needs_parent") else "note"
    send(path, ansi.strip_controls(message), mode=mode)


def _run_one(path, monitor, now, send, command_runner):
    cli = _cli()
    state = cli.load(path)
    if _closed(path, state) or not state.get("reusable"):
        return "skipped"
    workspace = Path(state.get("actual_workspace") or state["workspace"])
    result = None
    exception = None
    try:
        if not workspace.is_absolute() or not workspace.is_dir():
            raise OSError("worker workspace is unavailable")
        result = command_runner(monitor["command"], str(workspace))
        if result["timed_out"] or result["returncode"] != 0:
            raise RuntimeError("monitor command failed")
    except (OSError, RuntimeError, ValueError) as error:
        exception = error if result is None else None
        monitor["failure_streak"] = int(monitor.get("failure_streak") or 0) + 1
        monitor["last_error"] = _failure_text(monitor, result or {}, exception)
        monitor["next_run_at"] = monitor_math.next_due_at(now, monitor["interval_seconds"])
        if not monitor.get("failure_notified"):
            try:
                _send(path, monitor["last_error"], send)
            except (OSError, ValueError) as delivery_error:
                logger.warning("monitor %s failure delivery did not land: %s",
                               monitor["id"], ansi.strip_controls(str(delivery_error)))
            else:
                monitor["failure_notified"] = True
        _save_monitor(_monitor_path(path, monitor["id"]), monitor)
        return "failed"

    output = _decode(result["stdout"])
    if result["truncated"]:
        output += "\n[output truncated at 64 KiB]"
    normalized = monitor_math.normalize_output(output)
    digest = monitor_math.content_hash(normalized)
    old_hash = monitor.get("last_snapshot_hash")
    monitor["failure_streak"] = 0
    monitor["failure_notified"] = False
    monitor["last_error"] = None
    monitor["next_run_at"] = monitor_math.next_due_at(now, monitor["interval_seconds"])
    if old_hash is None:
        monitor.update(last_snapshot=normalized, last_snapshot_hash=digest)
        _save_monitor(_monitor_path(path, monitor["id"]), monitor)
        return "baseline"
    if digest == old_hash:
        _save_monitor(_monitor_path(path, monitor["id"]), monitor)
        return "checked"

    delta = monitor_math.render_unified_diff(monitor.get("last_snapshot") or "", normalized)
    label = ansi.strip_controls(str(monitor.get("label") or monitor["id"]))
    message = ansi.strip_controls(f"Monitor '{label}' ({monitor['id']}) changed:\n{delta}")
    try:
        _send(path, message, send)
    except (OSError, ValueError) as error:
        logger.warning("monitor %s change delivery did not land: %s",
                       monitor["id"], ansi.strip_controls(str(error)))
        _save_monitor(_monitor_path(path, monitor["id"]), monitor)
        return "delivery_failed"
    monitor.update(last_snapshot=normalized, last_snapshot_hash=digest)
    _save_monitor(_monitor_path(path, monitor["id"]), monitor)
    return "changed"


def run_due(*, now=None, send=None, command_runner=None):
    """Run one pass over due monitors; scheduling stays in pure monitor helpers."""
    cli = _cli()
    now = time.time() if now is None else now
    send = messaging.send if send is None else send
    command_runner = run_command if command_runner is None else command_runner
    counts = {"checked": 0, "changed": 0, "failed": 0, "skipped": 0}
    root = cli.root()
    for worker_path in sorted(root.iterdir()):
        if not (worker_path / "state.json").is_file():
            continue
        directory = _monitor_dir(worker_path)
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.json")):
            with _monitor_lock(path, blocking=False) as acquired:
                if not acquired or not path.is_file():
                    continue
                monitor = _load_monitor(path)
                if monitor is None or not monitor_math.is_due(monitor, now):
                    continue
                try:
                    result = _run_one(worker_path, monitor, now, send, command_runner)
                except (OSError, ValueError) as error:
                    logger.warning("monitor %s did not run: %s", path.stem,
                                   ansi.strip_controls(str(error)))
                    counts["skipped"] += 1
                    continue
                if result == "skipped":
                    counts["skipped"] += 1
                else:
                    counts["checked"] += 1
                    if result == "changed":
                        counts["changed"] += 1
                    elif result == "failed":
                        counts["failed"] += 1
    return counts
