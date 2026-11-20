"""Completion inbox: append-only events with monotonic seq, digests, and per-consumer cursors."""
import datetime
import fcntl
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import sys

CONSUMER_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
SESSION_RE = CONSUMER_RE

_HARNESS_SESSION_VARS = (
    ("CLAUDE_SESSION_ID", "claude"),
    ("CLAUDE_CODE_SESSION_ID", "claude"),
    ("CODEX_THREAD_ID", "codex"),
    ("ANTIGRAVITY_CONVERSATION_ID", "agy"),
    ("AGY_CONVERSATION_ID", "agy"),
    ("OPENCODE_SESSION_ID", "opencode"),
    ("PI_SESSION_ID", "pi"),
)


def _root():
    from claivelib.cli import root
    return root()


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def sanitize_session(raw):
    if not raw:
        return None
    raw_text = str(raw)
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "-", raw_text)
    cleaned = re.sub(r"-+", "-", cleaned).strip(".-")
    if not cleaned:
        return None
    if len(cleaned) > 64:
        digest = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()[:12]
        cleaned = f"{cleaned[:51]}-{digest}"
    return cleaned


def infer_parent_harness(env=None):
    env = os.environ if env is None else env
    explicit = str(env.get("CLAIVE_PARENT") or "").strip()
    if explicit:
        return explicit
    if (str(env.get("CLAUDE_SESSION_ID") or "").strip()
            or str(env.get("CLAUDE_CODE_SESSION_ID") or "").strip()
            or str(env.get("CLAUDECODE") or "").strip()):
        return "claude"
    if str(env.get("CODEX_THREAD_ID") or "").strip() or str(env.get("CODEX_CI") or "").strip():
        return "codex"
    if str(env.get("ANTIGRAVITY_CONVERSATION_ID") or "").strip() or str(env.get("AGY_CONVERSATION_ID") or "").strip():
        return "agy"
    if str(env.get("OPENCODE_SESSION_ID") or "").strip():
        return "opencode"
    if str(env.get("PI_SESSION_ID") or "").strip():
        return "pi"
    return None


def resolve_parent_session(explicit=None, env=None, workspace=None):
    if explicit is not None:
        check_consumer(explicit, label="session")
        return explicit
    env = os.environ if env is None else env
    env_session = env.get("CLAIVE_PARENT_SESSION")
    if env_session:
        check_consumer(env_session, label="session")
        return env_session
    for var, prefix in _HARNESS_SESSION_VARS:
        val = str(env.get(var) or "").strip()
        if val:
            return sanitize_session(f"{prefix}-{val}")
    if workspace is not None and str(workspace).strip():
        ws_canon = str(Path(str(workspace).strip()).expanduser().resolve())
        ws_hash = hashlib.sha1(ws_canon.encode("utf-8")).hexdigest()[:10]
        return f"ws-{ws_hash}"
    return None


def session_consumer(session):
    check_consumer(session, label="session")
    return sanitize_session(f"session-{session}")


def turn_event(job_id, label, turn, status, code, failure_kind, needs_parent,
               report_status, batch, mission, parent_session=None):
    event = dict(type="turn", seq=0, at=_now_iso(), id=job_id,
                 label=label, turn=turn, status=status, code=code,
                 failure_kind=failure_kind, needs_parent=needs_parent,
                 report_status=report_status, batch=batch, mission=mission,
                 wake=True)
    if parent_session is not None:
        event["parent_session"] = parent_session
    return event


def batch_event(batch_id, label, status, code, mission, parent_session=None):
    event = dict(type="batch", seq=0, at=_now_iso(), id=batch_id,
                 label=label, status=status, code=code, mission=mission,
                 wake=True)
    if parent_session is not None:
        event["parent_session"] = parent_session
    return event


def budget_event(job_id, label, turn, handoff, handoffs, context_tokens, session_id, next_step,
                 parent_session=None):
    event = dict(type="budget_handoff", seq=0, at=_now_iso(), id=job_id,
                 label=label, turn=turn, handoff=handoff, handoffs=handoffs,
                 context_tokens=context_tokens, session_id=session_id, next_step=next_step,
                 wake=False)
    if parent_session is not None:
        event["parent_session"] = parent_session
    return event


def _trim_one_line(text, limit=220):
    if not text:
        return None
    from claivelib.ansi import strip_controls
    cleaned = " ".join(strip_controls(str(text)).split())
    return cleaned[:limit] if cleaned else None


def _alias_for(root, worker_id):
    if not worker_id:
        return None
    try:
        data = json.loads((Path(root) / "aliases.json").read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        matches = sorted(name for name, target in data.items() if target == worker_id)
        return f"@{matches[0]}" if matches else None
    except (OSError, ValueError):
        return None


def _checks_summary_from_evidence(job_dir, state):
    turn = state.get("turn", 1) if isinstance(state, dict) else 1
    ver_file = Path(job_dir) / "evidence" / f"turn-{turn:04}" / "verification.json"
    try:
        ver = json.loads(ver_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        ver = None
    if isinstance(ver, dict) and isinstance(ver.get("checks"), list) and ver["checks"]:
        checks = ver["checks"]
        passed = sum(1 for item in checks if item.get("status") == "passed")
        failed = [item.get("name", "?") for item in checks if item.get("status") in {"failed", "blocked"}]
        if failed:
            return f"{passed}/{len(checks)} passed (failed: {', '.join(failed[:3])})"
        return f"{passed}/{len(checks)} passed"
    contract = state.get("acceptance_contract") if isinstance(state, dict) else None
    declared = contract.get("checks") if isinstance(contract, dict) else None
    if isinstance(declared, list) and declared:
        return f"0/{len(declared)} not_run"
    return "none"


_NOISE_PATH_PARTS = frozenset({
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".nox",
    "node_modules",
    ".venv",
    "venv",
    ".git",
})
_SALVAGEABLE_FAILURE_KINDS = frozenset({"timeout", "stall", "tool_idle_timeout"})


def _filter_noise_paths(paths, workspace=None, job_dir=None):
    cleaned = [str(p) for p in paths if str(p).strip()]
    if not cleaned:
        return []
    try:
        from claivelib import acceptance
        if hasattr(acceptance, "filter_noise_paths"):
            return list(acceptance.filter_noise_paths(cleaned, workspace=workspace, job_dir=job_dir))
    except Exception:
        pass
    from pathlib import PurePosixPath
    kept = []
    for item in cleaned:
        rel = item.strip().strip('"').rstrip("/")
        if not rel or rel == "launch.lock" or rel.endswith((".pyc", ".pyo")):
            continue
        if _NOISE_PATH_PARTS.intersection(PurePosixPath(rel).parts):
            continue
        kept.append(item)
    return kept


def build_digest(event, state=None, root=None):
    """Build a compact (<=200 token) actionable digest for a wake event."""
    base = Path(root) if root is not None else _root()
    worker_id = event.get("id")
    job_dir = base / str(worker_id) if isinstance(worker_id, str) and re.fullmatch(r"[0-9a-f]{12}", worker_id) else None
    if state is None and job_dir is not None and (job_dir / "state.json").is_file():
        try:
            state = json.loads((job_dir / "state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = None

    files_touched = []
    headline = None
    checks_summary = event.get("verification") or "none"
    workspace = state.get("workspace") if isinstance(state, dict) else None
    if isinstance(state, dict):
        turn = state.get("turn", 1)
        exec_file = job_dir / "evidence" / f"turn-{turn:04}" / "execution.json" if job_dir is not None else None
        if exec_file is not None and exec_file.is_file():
            try:
                exec_data = json.loads(exec_file.read_text(encoding="utf-8"))
                if isinstance(exec_data.get("changed_paths"), list):
                    files_touched = _filter_noise_paths(exec_data["changed_paths"], workspace=workspace, job_dir=job_dir)
            except (OSError, ValueError):
                pass
        report = state.get("report") if isinstance(state.get("report"), dict) else None
        if not files_touched and report and isinstance(report.get("changed_files"), list):
            files_touched = _filter_noise_paths(report["changed_files"], workspace=workspace, job_dir=job_dir)
        if not files_touched and isinstance(state.get("touched_files"), list):
            files_touched = _filter_noise_paths(state["touched_files"], workspace=workspace, job_dir=job_dir)
        if report and report.get("summary"):
            headline = _trim_one_line(report.get("summary"), 220)
        elif state.get("error"):
            headline = _trim_one_line(state.get("error"), 220)
        elif job_dir is not None and (job_dir / "result.txt").is_file():
            try:
                first_line = next((ln for ln in (job_dir / "result.txt").read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()), "")
                headline = _trim_one_line(first_line, 220)
            except OSError:
                pass
        if job_dir is not None:
            checks_summary = _checks_summary_from_evidence(job_dir, state)

    if headline is None:
        headline = _trim_one_line(event.get("label"), 160)

    needs_parent = event.get("needs_parent")
    ask_question = None
    if isinstance(needs_parent, dict) and needs_parent.get("question"):
        ask_question = _trim_one_line(needs_parent.get("question"), 240)
    elif isinstance(needs_parent, str) and needs_parent.strip():
        ask_question = _trim_one_line(needs_parent, 240)

    digest = {
        "seq": event.get("seq"),
        "worker_id": worker_id,
        "alias": _alias_for(base, worker_id) if job_dir is not None else None,
        "type": event.get("type", "turn"),
        "status": event.get("status"),
        "exit_code": event.get("code"),
        "files_touched": files_touched[:8],
        "files_touched_total": len(files_touched),
        "checks_summary": checks_summary,
        "report_headline": headline,
        "ask_question": ask_question,
    }
    if (
        event.get("status") == "failed"
        and event.get("failure_kind") in _SALVAGEABLE_FAILURE_KINDS
        and len(files_touched) > 0
    ):
        digest["salvageable"] = True
        digest["salvageable_files"] = files_touched[:8]
    return digest


def _seed_seq_unlocked(inbox_path):
    if not inbox_path.is_file():
        return 0
    count = 0
    max_dense = 0
    try:
        with inbox_path.open("rb") as stream:
            for raw in stream:
                if not raw.strip():
                    continue
                count += 1
                try:
                    item = json.loads(raw.decode("utf-8", errors="replace"))
                    seq = item.get("seq")
                    if isinstance(seq, int) and not isinstance(seq, bool) and 0 < seq < 1_000_000_000:
                        max_dense = max(max_dense, seq)
                except ValueError:
                    continue
    except OSError:
        return 0
    return max(count, max_dense)


def _reserved_seq(base) -> int:
    seq_file = base / "inbox.seq"
    try:
        return int(seq_file.read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        return _seed_seq_unlocked(base / "inbox.jsonl")


def current_seq(root=None) -> int:
    """Return a checkpoint that cannot skip an in-flight append."""
    base = Path(root) if root is not None else _root()
    # Sample first so a writer starting after a missing-lock check cannot race us.
    seq = _reserved_seq(base)
    try:
        lock = (base / ".inbox.lock").open("rb")
    except FileNotFoundError:
        return seq
    with lock:
        # A reserved number is safe to publish after its writer releases the lock.
        fcntl.flock(lock, fcntl.LOCK_SH)
    return seq


def _next_seq_locked(base, inbox_path):
    seq_file = base / "inbox.seq"
    try:
        prev = int(seq_file.read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        prev = _seed_seq_unlocked(inbox_path)
    nxt = prev + 1
    tmp = seq_file.with_name(f".inbox.seq.{os.getpid()}.tmp")
    tmp.write_text(str(nxt), encoding="utf-8")
    os.replace(tmp, seq_file)
    return nxt


def append(event):
    base = _root()
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    inbox_path = base / "inbox.jsonl"
    lock_fd = os.open(base / ".inbox.lock", os.O_WRONLY | os.O_CREAT, 0o600)
    with os.fdopen(lock_fd, "w") as lock_stream:
        fcntl.flock(lock_stream, fcntl.LOCK_EX)
        seq = _next_seq_locked(base, inbox_path)
        event["seq"] = seq
        if "wake" not in event:
            event["wake"] = event.get("type") != "budget_handoff" or bool(event.get("needs_parent"))
        if event.get("wake") and "digest" not in event:
            try:
                event["digest"] = build_digest(event, root=base)
            except Exception:
                pass
        elif "digest" in event and isinstance(event["digest"], dict):
            event["digest"]["seq"] = seq
        line = (json.dumps(event, ensure_ascii=True) + "\n").encode("utf-8")
        fd = os.open(inbox_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    notify(event)
    return event


def _load_notifications_module():
    if "claivelib.notifications" in sys.modules:
        return sys.modules["claivelib.notifications"]
    try:
        return importlib.import_module("claivelib.notifications")
    except ImportError:
        return None


def notify(event):
    notifications = _load_notifications_module()
    if notifications is not None:
        try:
            notifications.dispatch(event)
        except Exception:
            pass
        return
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


def check_consumer(consumer, label="consumer"):
    if not isinstance(consumer, str) or not CONSUMER_RE.fullmatch(consumer) or consumer in {".", ".."}:
        raise ValueError(f"invalid {label}: {consumer}")


def _is_wake_event(event):
    if "wake" in event:
        return bool(event["wake"])
    return event.get("type") != "budget_handoff" or bool(event.get("needs_parent"))


def _matches(event, parent_session=None, mission=None, since_seq=None, wake_only=False):
    if wake_only and not _is_wake_event(event):
        return False
    if since_seq is not None:
        seq = event.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq <= since_seq:
            return False
    if parent_session is not None and event.get("parent_session") != parent_session:
        return False
    if mission is not None and event.get("mission") != mission:
        return False
    return True


def read_since(since_seq, *, parent_session=None, mission=None, wake_only=True, root=None):
    """Stateless monotonic sequence read: return all matching events with seq > since_seq."""
    if isinstance(since_seq, bool) or not isinstance(since_seq, int) or since_seq < 0:
        raise ValueError("--since-seq must be a nonnegative integer")
    base = Path(root) if root is not None else _root()
    inbox = base / "inbox.jsonl"
    if not inbox.is_file():
        return []
    seq_file = base / "inbox.seq"
    if seq_file.is_file() and _reserved_seq(base) <= since_seq:
        return []
    size = inbox.stat().st_size
    events, _ = _read_complete_lines(inbox, 0, size)
    return [
        event for event in events
        if _matches(event, parent_session=parent_session, mission=mission,
                    since_seq=since_seq, wake_only=wake_only)
    ]


def has_since(since_seq, *, parent_session=None, mission=None, wake_only=True, root=None) -> bool:
    """Fast check for wait loops using monotonic --since-seq."""
    if isinstance(since_seq, bool) or not isinstance(since_seq, int) or since_seq < 0:
        raise ValueError("--since-seq must be a nonnegative integer")
    base = Path(root) if root is not None else _root()
    seq_file = base / "inbox.seq"
    if seq_file.is_file() and _reserved_seq(base) <= since_seq:
        return False
    return bool(read_since(since_seq, parent_session=parent_session, mission=mission,
                           wake_only=wake_only, root=base))


def read_new(consumer="default", peek=False, parent_session=None, mission=None,
             since_seq=None, wake_only=False):
    check_consumer(consumer)
    root = _root()
    inbox = root / "inbox.jsonl"
    cursors = root / "inbox-cursors"
    cursors.mkdir(parents=True, exist_ok=True, mode=0o700)
    cursor_file = cursors / consumer
    # One reader per consumer at a time, and the cursor is replaced atomically: a torn or
    # concurrent cursor would read as 0 and replay the whole inbox.
    with (cursors / f".{consumer}.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        events = _read_from(inbox, cursor_file, peek)
    return [
        event for event in events
        if _matches(event, parent_session=parent_session, mission=mission,
                    since_seq=since_seq, wake_only=wake_only)
    ]


def has_unread(consumer="default", parent_session=None, mission=None,
               since_seq=None, wake_only=False):
    """Cheap check for wait loops: true when the inbox holds matching bytes past the cursor.

    Reads no events and moves no cursor. A half-written last line also counts when unfiltered,
    so an empty read_new after a true result means "not yet", not "nothing to report".
    """
    return _has_unread_at(_root(), consumer=consumer, parent_session=parent_session,
                          mission=mission, since_seq=since_seq, wake_only=wake_only)


def _has_unread_at(root, consumer="default", parent_session=None, mission=None,
                   since_seq=None, wake_only=False):
    check_consumer(consumer)
    inbox = Path(root) / "inbox.jsonl"
    try:
        size = inbox.stat().st_size
    except OSError:
        return False
    start = _cursor_offset(Path(root) / "inbox-cursors" / consumer)
    if start > size:
        start = 0
    if size <= start:
        return False
    if parent_session is None and mission is None and since_seq is None and not wake_only:
        return True
    events, _ = _read_complete_lines(inbox, start, size)
    return any(
        _matches(event, parent_session=parent_session, mission=mission,
                 since_seq=since_seq, wake_only=wake_only)
        for event in events
    )


def _cursor_offset(cursor_file):
    try:
        return int(cursor_file.read_text().strip() or 0)
    except (OSError, ValueError):
        return 0


def _read_complete_lines(inbox, start, size):
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
    return events, end


def _read_from(inbox, cursor_file, peek):
    start = _cursor_offset(cursor_file)
    if not inbox.exists():
        if not peek:
            _save_cursor(cursor_file, 0)
        return []
    size = inbox.stat().st_size
    if start > size:
        start = 0
    events, end = _read_complete_lines(inbox, start, size)
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
        seq = event.get("seq")
        seq_prefix = f"#{seq} " if isinstance(seq, int) and 0 < seq < 1_000_000_000 else ""
        badge = "ASK" if event.get("needs_parent") else str(event.get("status", "")).upper()
        label = event.get("label", "")
        if event.get("type") == "batch":
            line = f"{seq_prefix}batch {event.get('id')} {badge} code={event.get('code')} {label}"
        elif event.get("type") == "budget_handoff":
            label = " ".join(str(label).split())
            step = " ".join(str(event.get("next_step") or "").split())[:300]
            line = (f"{seq_prefix}{event.get('id')} {label} | turn {event.get('turn')} | "
                    f"handoff {event.get('handoff')} (total {event.get('handoffs')}) | "
                    f"context {event.get('context_tokens', 0) // 1000}k | next step: {step}")
        elif event.get("type") == "conversation":
            line = f"{seq_prefix}conversation {event.get('id')} {badge} code={event.get('code')}"
            if event.get("needs_parent"):
                line += f" question: {event['needs_parent'].get('question', '')}"
        elif event.get("type") == "channel_closed":
            summary = event.get("summary", {})
            count = summary.get("counts", {}).get("total", 0)
            line = f"{seq_prefix}channel {event.get('channel', event.get('id'))} closed "
            line += f"({summary.get('close_reason', 'unknown')}); {count} messages"
        elif event.get("type") == "goal":
            line = f"{seq_prefix}goal {event.get('id')} {badge} {label}"
            if event.get("needs_parent"):
                line += (f" question: {event['needs_parent'].get('question', '')}"
                         f" (claive goal answer {event.get('id')} --message ...)")
        else:
            digest = event.get("digest") if isinstance(event.get("digest"), dict) else {}
            alias_part = f" ({digest['alias']})" if digest.get("alias") else ""
            line = f"{seq_prefix}{event.get('id')}{alias_part} {badge} code={event.get('code')} {label}"
            if event.get("failure_kind"):
                line += f" kind={event['failure_kind']}"
            if event.get("needs_parent"):
                line += f" question: {event['needs_parent'].get('question', '')}"
            elif digest.get("report_headline") and digest.get("report_headline") != label:
                line += f" — {digest['report_headline']}"
            if digest.get("files_touched"):
                shown = ", ".join(digest["files_touched"][:4])
                more = digest.get("files_touched_total", len(digest["files_touched"])) - min(4, len(digest["files_touched"]))
                if more > 0:
                    shown += f" (+{more})"
                line += f" [files: {shown}]"
            if digest.get("salvageable"):
                line += " [salvageable edits in worktree]"
        lines.append(line)
    return "\n".join(lines)
