"""Durable, non-throwing notification router for background events."""
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

from claivelib import config, inbox, messaging
from claivelib.ansi import strip_controls

MAX_ERROR_CHARS = 512
DEFAULT_TIMEOUT = 15.0


def _now_iso():
    return inbox._now_iso()


def _bounded_error(text):
    if not text:
        return None
    cleaned = " ".join(strip_controls(str(text)).split())
    if not cleaned:
        return None
    return cleaned[:MAX_ERROR_CHARS]


def event_summary(event: dict) -> str:
    """Return a clean single-line human summary for one inbox event."""
    if not isinstance(event, dict):
        return _bounded_error(str(event)) or ""
    try:
        raw = inbox.format_text([event])
    except Exception:
        raw = f"{event.get('type', 'event')} {event.get('id', '')} {event.get('status', '')}".strip()
    return " ".join(strip_controls(raw).split())


def _safe_job_dir(event_id):
    if not isinstance(event_id, str) or not event_id or Path(event_id).name != event_id or event_id in {".", ".."}:
        return None
    from claivelib import cli
    return cli.root(create=False) / event_id


def _event_context(event: dict) -> tuple[dict | None, bool, bool]:
    """Resolve (worker_state, parked, watched) without raising."""
    state = None
    if isinstance(event, dict) and event.get("type") in {"turn", "budget_handoff"}:
        job_dir = _safe_job_dir(event.get("id"))
        if job_dir is not None and (job_dir / "state.json").is_file():
            try:
                from claivelib import cli
                state = cli.load(job_dir, check_alive=False)
            except Exception:
                state = None
    parked = bool((state or {}).get("parked") or (event.get("parked") if isinstance(event, dict) else False))
    watched = False
    try:
        import importlib
        import sys
        watchers = sys.modules.get("claivelib.watchers")
        if watchers is None:
            watchers = importlib.import_module("claivelib.watchers")
        target = state or {
            "id": event.get("id") if isinstance(event, dict) else None,
            "parent_session": event.get("parent_session") if isinstance(event, dict) else None,
        }
        watched = bool(watchers.is_worker_watched(target))
    except Exception:
        watched = False
    return state, parked, watched


def matches_triggers(triggers, event, *, parked=False, watched=False) -> bool:
    """Return True when any trigger in triggers matches event and its watch/park state."""
    if not triggers or not isinstance(event, dict):
        return False
    trigger_set = set(triggers)
    # Silent mid-turn progress (e.g. automatic budget_handoff) never push-wakes unless explicitly requested.
    if event.get("wake") is False and "budget_handoff" not in trigger_set:
        return False
    active_tags = {"all"}
    event_type = str(event.get("type") or "")
    if event_type:
        active_tags.add(event_type)
    if event.get("needs_parent"):
        active_tags.add("ask")
    if event.get("status") == "completed" and not event.get("needs_parent"):
        active_tags.add("completed")
    code = event.get("code")
    if event.get("status") in {"failed", "interrupted"} or (
        isinstance(code, int) and not isinstance(code, bool) and code not in (0, 3)
    ):
        active_tags.add("failed")
    if parked:
        active_tags.add("parked")
    if not watched:
        active_tags.add("unwatched")
    return bool(set(triggers) & active_tags)


def collect_sinks(event, state=None) -> list[dict]:
    """Collect active sinks from env, per-worker launch metadata, and host config."""
    sinks = []
    env_cmd = os.environ.get("CLAIVE_NOTIFY_CMD")
    if isinstance(env_cmd, str) and env_cmd.strip():
        sinks.append({
            "name": "env-notify-cmd",
            "kind": "command",
            "command": env_cmd,
            "on": ["all"],
            "timeout": DEFAULT_TIMEOUT,
            "fire_and_forget": False,
            "enabled": True,
        })

    launch = state.get("launch") if isinstance(state, dict) else None
    if isinstance(launch, dict):
        notify_cmd = launch.get("notify_cmd")
        if isinstance(notify_cmd, str) and notify_cmd.strip() and notify_cmd != env_cmd:
            sinks.append({
                "name": "worker-notify-cmd",
                "kind": "command",
                "command": notify_cmd,
                "on": ["all"],
                "timeout": DEFAULT_TIMEOUT,
                "enabled": True,
            })
        notify_worker = launch.get("notify_worker")
        if isinstance(notify_worker, str) and notify_worker.strip():
            sinks.append({
                "name": "worker-notify-target",
                "kind": "worker",
                "worker": notify_worker,
                "on": ["turn", "ask", "failed", "completed"],
                "timeout": DEFAULT_TIMEOUT,
                "enabled": True,
            })

    cfg = config.notifications_config(safe=True)
    if cfg.get("enabled", True):
        for entry in cfg.get("sinks") or []:
            if entry.get("enabled", True):
                sinks.append(dict(entry))
    return sinks


def _receipts_path(root_dir=None) -> Path:
    if root_dir is not None:
        base = Path(root_dir)
    else:
        from claivelib import cli
        base = cli.root()
    return base / "notifications" / "receipts.jsonl"


def _write_receipt(receipt: dict, root_dir=None) -> None:
    path = _receipts_path(root_dir=root_dir)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    line = (json.dumps(receipt, ensure_ascii=True) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


def list_receipts(limit=50, root_dir=None) -> list[dict]:
    """Read newest notification delivery receipts (newest first)."""
    if root_dir is not None:
        path = _receipts_path(root_dir=root_dir)
    else:
        from claivelib import cli
        path = cli.root(create=False) / "notifications" / "receipts.jsonl"
    if not path.is_file():
        return []
    items = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            items.append(record)
    items.reverse()
    if limit is not None and limit >= 0:
        return items[:limit]
    return items


def _execute_command_sink(sink, event, state, summary):
    timeout = float(sink.get("timeout") or DEFAULT_TIMEOUT)
    code_value = event.get("code")
    session_value = event.get("parent_session") or (state.get("parent_session") if isinstance(state, dict) else None)
    env = dict(
        os.environ,
        CLAIVE_EVENT_ID=str(event.get("id") or ""),
        CLAIVE_EVENT_STATUS=str(event.get("status") or ""),
        CLAIVE_EVENT_CODE="" if code_value is None else str(code_value),
        CLAIVE_EVENT_TYPE=str(event.get("type") or ""),
        CLAIVE_EVENT_SESSION="" if session_value is None else str(session_value),
        CLAIVE_EVENT_SUMMARY=summary,
    )
    payload = (json.dumps(event, ensure_ascii=True) + "\n").encode("utf-8")
    with tempfile.TemporaryFile() as err_file:
        proc = subprocess.Popen(
            ["/bin/sh", "-c", sink["command"]],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=err_file,
            start_new_session=True,
            env=env,
        )
        try:
            if proc.stdin is not None:
                proc.stdin.write(payload)
                proc.stdin.close()
        except OSError:
            pass
        try:
            exit_code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return "detached_timeout", None, f"command timed out after {timeout}s (detached)"
        err_file.seek(0)
        stderr_text = err_file.read(MAX_ERROR_CHARS * 2).decode("utf-8", errors="replace")
        if exit_code == 0:
            return "ok", 0, None
        return "failed", exit_code, _bounded_error(stderr_text) or f"exit code {exit_code}"


def _execute_worker_sink(sink, event, summary):
    from claivelib import cli
    target_path = cli.job_path(sink["worker"])
    if target_path.name == str(event.get("id") or ""):
        return None
    message = f"[Claive Background Event] {summary}".strip()
    messaging.send(target_path, message=message, mode="wake")
    return "ok", None, None


def _execute_url_sink(sink, event):
    timeout = float(sink.get("timeout") or DEFAULT_TIMEOUT)
    payload = (json.dumps(event, ensure_ascii=True) + "\n").encode("utf-8")
    req = urllib.request.Request(
        sink["url"],
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            status_code = getattr(response, "status", None)
            if status_code is None:
                status_code = response.getcode()
            if 200 <= int(status_code) < 400:
                return "ok", None, None
            return "failed", int(status_code), f"HTTP {status_code}"
    except urllib.error.HTTPError as error:
        code = error.code
        reason = error.reason
        error.close()
        return "failed", code, _bounded_error(f"HTTP {code}: {reason}")
    except (TimeoutError, socket.timeout) as error:
        return "timeout", None, _bounded_error(error) or f"request timed out after {timeout}s"
    except urllib.error.URLError as error:
        if isinstance(error.reason, (TimeoutError, socket.timeout)):
            return "timeout", None, _bounded_error(error.reason) or f"request timed out after {timeout}s"
        return "failed", None, _bounded_error(error.reason or error)


def _deliver_sink(sink, event, state, summary):
    started = time.monotonic()
    kind = sink["kind"]
    status = "failed"
    exit_code = None
    error = None
    try:
        if kind == "command":
            status, exit_code, error = _execute_command_sink(sink, event, state, summary)
        elif kind == "worker":
            result = _execute_worker_sink(sink, event, summary)
            if result is None:
                return None
            status, exit_code, error = result
        elif kind == "url":
            status, exit_code, error = _execute_url_sink(sink, event)
        else:
            error = f"unknown sink kind: {kind}"
    except Exception as exc:
        status = "failed"
        exit_code = None
        error = _bounded_error(exc) or type(exc).__name__
    duration_ms = int((time.monotonic() - started) * 1000)
    receipt = {
        "seq": time.time_ns(),
        "at": _now_iso(),
        "event_id": event.get("id"),
        "event_type": event.get("type"),
        "sink": sink["name"],
        "kind": kind,
        "status": status,
        "exit_code": exit_code,
        "error": _bounded_error(error),
        "duration_ms": duration_ms,
    }
    try:
        _write_receipt(receipt)
    except Exception:
        pass
    return receipt


def dispatch(event: dict) -> list[dict]:
    """Route an inbox event to all matching sinks without raising."""
    try:
        if not isinstance(event, dict):
            return []
        state, parked, watched = _event_context(event)
        sinks = collect_sinks(event, state=state)
        if not sinks:
            return []
        summary = event_summary(event)
        receipts = []
        for sink in sinks:
            if not sink.get("enabled", True):
                continue
            if not matches_triggers(sink.get("on", ["all"]), event, parked=parked, watched=watched):
                continue
            receipt = _deliver_sink(sink, event, state, summary)
            if receipt is not None:
                receipts.append(receipt)
        return receipts
    except Exception:
        return []
