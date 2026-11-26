"""Lightweight, fail-silent, rotating JSONL telemetry for Claive.

Design invariants:
- Never hinders Claive: every write is best-effort, non-blocking (single O_APPEND
  os.write < PIPE_BUF), and wrapped in fail-silent exception handling.
- Never bloatware: compact single-line records (no prompts, tool outputs, or
  file contents), redacted via claivelib.redaction, rotated at 5 MiB (max ~10 MiB).
- Hermetic: respects CLAIVE_DIR via claivelib.cli.root(), and honors CLAIVE_TELEMETRY=0.
"""
from collections import Counter, defaultdict
import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import time

from claivelib import redaction

SCHEMA_VERSION = 1
FILENAME = "telemetry.jsonl"
ARCHIVE_FILENAME = "telemetry.jsonl.1"
MAX_BYTES = 5 * 1024 * 1024
_DISABLE_VALUES = {"0", "off", "false", "no"}
_STDERR_TAIL_BYTES = 4096
_ERROR_KEYWORDS = (
    "error",
    "exception",
    "fatal",
    "failed",
    "panic",
    "quota",
    "exhausted",
    "denied",
    "timeout",
    "cannot find",
    "enoent",
)
_HEX_OR_NUM_RE = re.compile(r"\b(?:[0-9a-f]{8,}|\d{4,})\b", re.IGNORECASE)


def enabled(env=None):
    env = os.environ if env is None else env
    raw = str(env.get("CLAIVE_TELEMETRY", "1")).strip().lower()
    return raw not in _DISABLE_VALUES


def telemetry_path(base=None, create=True):
    if base is None:
        from claivelib.cli import root
        base = root(create=create)
    return Path(base) / FILENAME


def archive_path(base=None, create=False):
    return telemetry_path(base=base, create=create).with_name(ARCHIVE_FILENAME)


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _clean_text(text, limit=240):
    if not text:
        return None
    from claivelib.ansi import strip_controls
    cleaned = " ".join(strip_controls(str(text)).split())
    if not cleaned:
        return None
    cleaned = redaction.redact(cleaned)
    return cleaned[:limit]


def _extract_stderr_hint(job_dir):
    """Best-effort compact root-cause extraction from the tail of stderr.log."""
    try:
        path = Path(job_dir) / "stderr.log"
        size = path.stat().st_size
        if size <= 0:
            return None
        with open(path, "rb") as stream:
            if size > _STDERR_TAIL_BYTES:
                stream.seek(size - _STDERR_TAIL_BYTES)
            raw = stream.read(_STDERR_TAIL_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return None

    lines = raw.splitlines()
    for line in reversed(lines):
        if "AGY_ERROR:" in line:
            _, json_part = line.split("AGY_ERROR:", 1)
            try:
                payload = json.loads(json_part.strip())
            except ValueError:
                payload = None
            if isinstance(payload, dict):
                msg = payload.get("short_error") or payload.get("message")
                cleaned = _clean_text(msg, limit=240)
                if cleaned:
                    return cleaned

    last_non_empty = None
    for line in reversed(lines):
        cleaned = _clean_text(line, limit=240)
        if not cleaned:
            continue
        if last_non_empty is None:
            last_non_empty = cleaned
        low = cleaned.lower()
        if any(kw in low for kw in _ERROR_KEYWORDS):
            return cleaned
    return last_non_empty


def _maybe_rotate(path, max_bytes=MAX_BYTES):
    try:
        if path.stat().st_size >= max_bytes:
            os.replace(path, path.with_name(ARCHIVE_FILENAME))
    except OSError:
        pass


def emit(event, base=None, max_bytes=MAX_BYTES, existing_only=False):
    """Append one compact JSONL telemetry event. Fail-silent and non-blocking."""
    try:
        if not enabled():
            return False
        if not isinstance(event, dict):
            return False
        record = {"v": SCHEMA_VERSION, "ts": _now_iso()}
        for key, value in event.items():
            if value is not None:
                record[key] = value
        line = (json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
        path = telemetry_path(base=base, create=not existing_only)
        flags = os.O_WRONLY | os.O_APPEND
        if existing_only:
            if path.stat().st_size + len(line) > max_bytes:
                return False
        else:
            _maybe_rotate(path, max_bytes=max_bytes)
            flags |= os.O_CREAT
        fd = os.open(path, flags, 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
        return True
    except Exception:
        return False


def _compact_usage(state):
    usage = {}
    tu = state.get("turn_usage")
    if isinstance(tu, dict):
        mapping = (
            ("in", "input_tokens"),
            ("out", "output_tokens"),
            ("reasoning", "reasoning_tokens"),
            ("cache_read", "native_cache_read_tokens"),
        )
        for short, field in mapping:
            val = tu.get(field)
            if isinstance(val, int) and not isinstance(val, bool) and val > 0:
                usage[short] = val
    ctx = state.get("context_tokens")
    if isinstance(ctx, int) and not isinstance(ctx, bool) and ctx > 0:
        usage["ctx"] = ctx
    return usage or None


def record_turn(state, job_dir=None, evidence=None, base=None):
    """Record a compact turn-completion telemetry event. Fail-silent."""
    try:
        if not enabled() or not isinstance(state, dict):
            return False
        from claivelib import inbox as inbox_mod
        status = state.get("status")
        if status == "idle":
            status = state.get("last_turn_status", "failed")
        started = state.get("turn_started_at") or state.get("started_at")
        ended = state.get("ended_at") or time.time()
        duration_s = round(max(0.0, float(ended) - float(started)), 2) if isinstance(started, (int, float)) else None

        launch = state.get("launch") if isinstance(state.get("launch"), dict) else {}
        workspace = state.get("workspace")
        repo = Path(workspace).name if isinstance(workspace, str) and workspace.strip() else None

        raw_error = _clean_text(state.get("error"), limit=240)
        stderr_hint = None
        if status in {"failed", "interrupted"} or (isinstance(state.get("exit_code"), int) and state.get("exit_code") != 0):
            is_generic = not raw_error or raw_error.startswith("exit=") or "terminal=failed" in raw_error
            if is_generic and job_dir is not None:
                stderr_hint = _extract_stderr_hint(job_dir)
        best_error = stderr_hint or raw_error

        changed_files = None
        if isinstance(state.get("workspace_delta"), dict) and isinstance(state["workspace_delta"].get("files"), int):
            changed_files = state["workspace_delta"]["files"]
        elif isinstance(state.get("touched_files"), list):
            changed_files = len(state["touched_files"])

        verify_status = None
        accept_status = None
        if isinstance(evidence, dict):
            verify_status = (evidence.get("verification") or {}).get("status")
            accept_status = (evidence.get("acceptance") or {}).get("status")

        fallbacks = state.get("fallbacks")
        fallback_count = len(fallbacks) if isinstance(fallbacks, list) else 0

        event = {
            "event": "turn",
            "id": state.get("id"),
            "engine": state.get("engine"),
            "provider": launch.get("provider"),
            "model": state.get("model"),
            "effort": state.get("reasoning_effort"),
            "reusable": bool(state.get("reusable", False)),
            "turn": state.get("turn", 1),
            "attempts": state.get("attempt", 1),
            "parent_harness": inbox_mod.infer_parent_harness(),
            "parent_session": state.get("parent_session"),
            "repo": repo,
            "status": status,
            "exit_code": state.get("exit_code"),
            "failure_kind": state.get("failure_kind"),
            "terminal_reason": _clean_text(state.get("terminal_reason"), limit=240),
            "duration_s": duration_s,
            "steps": state.get("event_steps", state.get("steps", 0)),
            "model_steps": state.get("steps", 0),
            "malformed_events": state.get("malformed_events", 0),
            "task_failures": state.get("turn_task_failures", state.get("task_failures", 0)),
            "fallbacks": fallback_count,
            "usage": _compact_usage(state),
            "changed_files": changed_files,
            "verify": verify_status,
            "accept": accept_status,
            "report_state": state.get("report_state"),
            "needs_parent": bool(state.get("needs_parent")),
            "error": best_error,
        }
        return emit(event, base=base)
    except Exception:
        return False


def record_fallback(state, from_model, to_model, failure_kind, error=None, attempt=None, base=None):
    """Record a model fallback transition. Fail-silent."""
    try:
        if not enabled() or not isinstance(state, dict):
            return False
        event = {
            "event": "fallback",
            "id": state.get("id"),
            "engine": state.get("engine"),
            "from_model": from_model,
            "to_model": to_model,
            "failure_kind": failure_kind,
            "attempt": attempt,
            "error": _clean_text(error, limit=200),
        }
        return emit(event, base=base)
    except Exception:
        return False


def record_cli_error(action, error, base=None):
    """Append to initialized telemetry without leaving files for rejected commands."""
    try:
        if not enabled():
            return False
        from claivelib import inbox as inbox_mod
        if isinstance(error, subprocess.CalledProcessError):
            detail = f"Subprocess exited with status {error.returncode}"
        elif isinstance(error, subprocess.TimeoutExpired):
            detail = f"Subprocess timed out after {error.timeout} seconds"
        else:
            detail = str(error)
        event = {
            "event": "cli_error",
            "action": action or "unknown",
            "error_type": type(error).__name__,
            "error": _clean_text(detail, limit=240),
            "parent_harness": inbox_mod.infer_parent_harness(),
        }
        return emit(event, base=base, existing_only=True)
    except Exception:
        return False


def _parse_iso(ts):
    if not isinstance(ts, str) or not ts:
        return None
    try:
        return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def read_events(base=None, include_archive=True, since_days=None, engine=None, event_type=None, failures_only=False):
    """Read telemetry events from archive + active JSONL, skipping malformed lines."""
    paths = []
    if include_archive:
        arch = archive_path(base=base, create=False)
        if arch.is_file():
            paths.append(arch)
    cur = telemetry_path(base=base, create=False)
    if cur.is_file():
        paths.append(cur)

    cutoff = time.time() - (float(since_days) * 86400.0) if since_days is not None else None
    out = []
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as stream:
                for raw in stream:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        item = json.loads(raw)
                    except ValueError:
                        continue
                    if not isinstance(item, dict):
                        continue
                    if cutoff is not None:
                        ts = _parse_iso(item.get("ts"))
                        if ts is not None and ts < cutoff:
                            continue
                    if engine and item.get("engine") != engine:
                        continue
                    if event_type and item.get("event") != event_type:
                        continue
                    if failures_only:
                        ev = item.get("event")
                        if ev == "turn" and item.get("status") not in {"failed", "interrupted"}:
                            continue
                        if ev not in {"turn", "fallback", "cli_error"}:
                            continue
                    out.append(item)
        except OSError:
            continue
    return out


def _normalize_error_sig(kind, error):
    prefix = kind or "error"
    if not error:
        return prefix
    short = _HEX_OR_NUM_RE.sub("<id>", str(error))[:90]
    return f"{prefix}: {short}"


def _percentile(sorted_vals, pct):
    if not sorted_vals:
        return None
    idx = min(len(sorted_vals) - 1, max(0, int(round((pct / 100.0) * (len(sorted_vals) - 1)))))
    return round(sorted_vals[idx], 2)


def summarize(events):
    """Compute a compact diagnostic summary from telemetry events."""
    turns = [e for e in events if e.get("event") == "turn"]
    fallbacks = [e for e in events if e.get("event") == "fallback"]
    cli_errors = [e for e in events if e.get("event") == "cli_error"]

    ok_n = sum(1 for t in turns if t.get("status") == "completed")
    fail_n = sum(1 for t in turns if t.get("status") in {"failed", "interrupted"})
    cancel_n = sum(1 for t in turns if t.get("status") == "cancelled")
    durations = sorted(float(t["duration_s"]) for t in turns if isinstance(t.get("duration_s"), (int, float)))

    by_model = defaultdict(lambda: {
        "turns": 0, "ok": 0, "fail": 0, "cancelled": 0,
        "fallbacks": 0, "malformed_events": 0, "durations": [],
    })
    failure_sigs = Counter()
    for t in turns:
        key = f"{t.get('engine') or '?'}/{t.get('model') or '?'}"
        bucket = by_model[key]
        bucket["turns"] += 1
        st = t.get("status")
        if st == "completed":
            bucket["ok"] += 1
        elif st in {"failed", "interrupted"}:
            bucket["fail"] += 1
            failure_sigs[_normalize_error_sig(t.get("failure_kind"), t.get("error"))] += 1
        elif st == "cancelled":
            bucket["cancelled"] += 1
        bucket["fallbacks"] += int(t.get("fallbacks") or 0)
        bucket["malformed_events"] += int(t.get("malformed_events") or 0)
        if isinstance(t.get("duration_s"), (int, float)):
            bucket["durations"].append(float(t["duration_s"]))

    models_summary = []
    for key, b in sorted(by_model.items(), key=lambda kv: (-kv[1]["turns"], kv[0])):
        durs = b["durations"]
        avg_s = round(sum(durs) / len(durs), 1) if durs else None
        fail_pct = round((b["fail"] * 100.0) / b["turns"], 1) if b["turns"] else 0.0
        models_summary.append({
            "target": key,
            "turns": b["turns"],
            "ok": b["ok"],
            "fail": b["fail"],
            "fail_pct": fail_pct,
            "avg_s": avg_s,
            "fallbacks": b["fallbacks"],
            "malformed_events": b["malformed_events"],
        })

    cli_err_sigs = Counter(
        f"{e.get('action') or '?'} ({e.get('error_type') or 'Error'}): {(e.get('error') or '')[:80]}"
        for e in cli_errors
    )

    return {
        "total_events": len(events),
        "turns": len(turns),
        "ok": ok_n,
        "fail": fail_n,
        "cancelled": cancel_n,
        "success_pct": round((ok_n * 100.0) / len(turns), 1) if turns else None,
        "median_s": _percentile(durations, 50),
        "p95_s": _percentile(durations, 95),
        "fallback_events": len(fallbacks),
        "cli_errors": len(cli_errors),
        "by_model": models_summary,
        "top_failures": [{"signature": sig, "count": cnt} for sig, cnt in failure_sigs.most_common(10)],
        "top_cli_errors": [{"signature": sig, "count": cnt} for sig, cnt in cli_err_sigs.most_common(5)],
    }


def format_summary(summary):
    lines = []
    if summary["total_events"] == 0:
        return "No telemetry events recorded yet."
    succ = f"{summary['success_pct']}%" if summary["success_pct"] is not None else "n/a"
    med = f"{summary['median_s']}s" if summary["median_s"] is not None else "n/a"
    p95 = f"{summary['p95_s']}s" if summary["p95_s"] is not None else "n/a"
    lines.append(
        f"Turns: {summary['turns']} (ok {summary['ok']}, fail {summary['fail']}, "
        f"cancelled {summary['cancelled']}, success {succ}) | median {med}, p95 {p95} | "
        f"fallbacks {summary['fallback_events']} | cli_errors {summary['cli_errors']}"
    )
    if summary["by_model"]:
        lines.append("")
        lines.append("By engine/model:")
        for row in summary["by_model"]:
            avg = f"{row['avg_s']}s" if row["avg_s"] is not None else "n/a"
            extra = []
            if row["fallbacks"]:
                extra.append(f"fb={row['fallbacks']}")
            if row["malformed_events"]:
                extra.append(f"malformed={row['malformed_events']}")
            suffix = f" ({', '.join(extra)})" if extra else ""
            lines.append(
                f"  {row['target']:<36} turns={row['turns']:<4} ok={row['ok']:<4} "
                f"fail={row['fail']:<4} ({row['fail_pct']:>4.1f}%) avg={avg}{suffix}"
            )
    if summary["top_failures"]:
        lines.append("")
        lines.append("Top failures:")
        for item in summary["top_failures"]:
            lines.append(f"  {item['count']:>3}x  {item['signature']}")
    if summary["top_cli_errors"]:
        lines.append("")
        lines.append("Top CLI errors:")
        for item in summary["top_cli_errors"]:
            lines.append(f"  {item['count']:>3}x  {item['signature']}")
    return "\n".join(lines)


def format_tail_line(item):
    ts = str(item.get("ts") or "")[5:19].replace("T", " ")
    ev = item.get("event") or "?"
    if ev == "turn":
        dur = f"{item['duration_s']}s" if isinstance(item.get("duration_s"), (int, float)) else "-"
        target = f"{item.get('engine') or '?'}/{item.get('model') or '?'}"
        err = f" | {item['error']}" if item.get("error") else ""
        return f"{ts}  turn      {item.get('id') or '-'}  {item.get('status') or '?':<11} {dur:>7}  {target}{err}"
    if ev == "fallback":
        return (
            f"{ts}  fallback  {item.get('id') or '-'}  "
            f"{item.get('from_model') or '?'} -> {item.get('to_model') or '?'} "
            f"({item.get('failure_kind') or 'error'})"
        )
    if ev == "cli_error":
        return f"{ts}  cli_error {item.get('action') or '?'}  {item.get('error_type') or 'Error'}: {item.get('error') or ''}"
    return f"{ts}  {ev}  {json.dumps(item, separators=(',', ':'))[:120]}"


def clear(base=None):
    removed = []
    for path in (telemetry_path(base=base, create=False), archive_path(base=base, create=False)):
        try:
            if path.is_file():
                path.unlink()
                removed.append(str(path))
        except OSError:
            pass
    return removed
