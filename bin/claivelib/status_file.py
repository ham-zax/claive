"""Compact atomic status.json projection for zero-shell worker inspection."""
import json
import os
from pathlib import Path
import tempfile
import time

from claivelib import acceptance, redaction

MAX_CHANGED_FILES = 64
MAX_CHANGED_FILES_BYTES = 16 * 1024


def _bound_changed_files(paths, max_items=MAX_CHANGED_FILES, max_bytes=MAX_CHANGED_FILES_BYTES):
    """Bound changed_files by count and UTF-8 JSON-encoded byte budget."""
    bounded = []
    # Reserve 2 bytes for enclosing '[' and ']'.
    used = 2
    for item in paths:
        if len(bounded) >= max_items:
            break
        encoded = json.dumps(item, ensure_ascii=False).encode("utf-8")
        needed = len(encoded) + (2 if bounded else 0)
        if bounded and used + needed > max_bytes:
            break
        if not bounded and used + needed > max_bytes:
            # Always keep at most one truncated entry if even the first path exceeds the budget.
            break
        bounded.append(item)
        used += needed
    return bounded


def _read_json(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _alias_for(job_dir, worker_id):
    if not worker_id:
        return None
    aliases_file = Path(job_dir).parent / "aliases.json"
    data = _read_json(aliases_file)
    if not data:
        return None
    matches = sorted(name for name, target in data.items() if target == worker_id)
    return matches[0] if matches else None


def _evidence_summary(job_dir, state):
    turn = state.get("turn", 1)
    ev_dir = Path(job_dir) / "evidence" / f"turn-{turn:04}"
    execution = _read_json(ev_dir / "execution.json") or {}
    verification = _read_json(ev_dir / "verification.json") or {}
    acceptance_data = _read_json(ev_dir / "acceptance.json") or {}
    return execution, verification, acceptance_data


def build(job_dir, state, now=None):
    """Build the compact public status summary dict for a worker."""
    job_dir = Path(job_dir)
    now = time.time() if now is None else now
    worker_id = state.get("id") or job_dir.name
    launch = state.get("launch") if isinstance(state.get("launch"), dict) else {}
    execution, verification, acceptance_doc = _evidence_summary(job_dir, state)
    started_at = state.get("started_at")
    ended_at = state.get("ended_at")
    end_ref = ended_at if isinstance(ended_at, (int, float)) else now
    elapsed_s = round(max(0.0, end_ref - started_at), 2) if isinstance(started_at, (int, float)) else None
    turn_started = state.get("turn_started_at") or started_at
    turn_elapsed_s = round(max(0.0, end_ref - turn_started), 2) if isinstance(turn_started, (int, float)) else elapsed_s
    last_activity = state.get("last_activity")
    last_event_age_s = round(max(0.0, now - last_activity), 2) if isinstance(last_activity, (int, float)) else None

    last_tool = state.get("last_tool")
    if isinstance(last_tool, dict):
        last_tool = dict(last_tool)
        if isinstance(last_tool.get("detail"), str):
            last_tool["detail"] = redaction.redact(last_tool["detail"])[:200]

    report = state.get("report")
    if isinstance(execution.get("changed_paths"), list):
        changed_files = list(execution["changed_paths"])
    elif isinstance(report, dict) and isinstance(report.get("changed_files"), list) and report["changed_files"]:
        changed_files = list(report["changed_files"])
    else:
        changed_files = list(state.get("touched_files") or [])
    changed_files = acceptance.filter_noise_paths(
        changed_files,
        workspace=state.get("actual_workspace") or state.get("workspace"),
        job_dir=job_dir,
    )
    changed_files_total = len(changed_files)
    changed_files = _bound_changed_files(changed_files)
    changed_files_truncated = len(changed_files) < changed_files_total

    if isinstance(report, dict):
        report = dict(report)
        if isinstance(report.get("changed_files"), list):
            report_files = acceptance.filter_noise_paths(
                list(report["changed_files"]),
                workspace=state.get("actual_workspace") or state.get("workspace"),
                job_dir=job_dir,
            )
            bounded_report_files = _bound_changed_files(report_files)
            if len(bounded_report_files) < len(report_files):
                report["changed_files"] = bounded_report_files
                report["changed_files_total"] = len(report_files)
                report["changed_files_truncated"] = True
            else:
                report["changed_files"] = report_files

    turn_usage = state.get("turn_usage") if isinstance(state.get("turn_usage"), dict) else {}
    native_cache_read = turn_usage.get("native_cache_read_tokens")

    latest_intent = state.get("latest_intent")
    if isinstance(latest_intent, str) and latest_intent.strip():
        latest_intent = redaction.redact(latest_intent.strip())[:140]
    else:
        latest_intent = None

    raw_activity = state.get("activity")
    activity_payload = None
    if isinstance(raw_activity, dict) and raw_activity.get("stage"):
        targets = [
            redaction.redact(str(t))[:80]
            for t in (raw_activity.get("targets") or [])[:3]
            if isinstance(t, str) and t.strip()
        ]
        activity_payload = {
            "stage": str(raw_activity.get("stage"))[:24],
            "label": redaction.redact(str(raw_activity.get("label") or ""))[:100],
            "targets": targets,
        }

    raw_check = state.get("last_check")
    last_check_payload = None
    if isinstance(raw_check, dict) and raw_check.get("status"):
        last_check_payload = {
            "command": redaction.redact(str(raw_check.get("command") or ""))[:100],
            "status": str(raw_check.get("status"))[:16],
            "summary": redaction.redact(str(raw_check.get("summary") or ""))[:100],
        }

    payload = {
        "id": worker_id,
        "alias": _alias_for(job_dir, worker_id),
        "label": state.get("label"),
        "engine": state.get("engine"),
        "provider": launch.get("provider"),
        "model": state.get("model"),
        "reasoning_effort": state.get("reasoning_effort"),
        "workspace": state.get("workspace"),
        "actual_workspace": state.get("actual_workspace"),
        "retained_worktree": state.get("retained_worktree"),
        "reusable": bool(state.get("reusable")),
        "parent_session": state.get("parent_session"),
        "parked": bool(state.get("parked")),
        "turn": state.get("turn", 1),
        "status": state.get("status"),
        "phase": state.get("phase"),
        "step_count": state.get("event_steps", 0),
        "model_steps": state.get("steps", 0),
        "updated_at": round(now, 3),
        "elapsed_s": elapsed_s,
        "turn_elapsed_s": turn_elapsed_s,
        "last_event": state.get("last_event"),
        "last_event_age_s": last_event_age_s,
        "active_tools": dict(state.get("active_tools") or {}),
        "last_tool": last_tool,
        "task_failures": state.get("turn_task_failures", state.get("task_failures", 0)),
        "needs_parent": state.get("needs_parent"),
        "failure_kind": state.get("failure_kind"),
        "error": state.get("error"),
        "exit_code": state.get("exit_code"),
        "changed_files": changed_files,
        "changed_files_total": changed_files_total,
        "changed_files_truncated": changed_files_truncated,
        "verification_status": verification.get("status", "not_run"),
        "acceptance_status": acceptance_doc.get("status", "pending"),
        "report_state": state.get("report_state"),
        "report": report,
        "status_file": str(job_dir / "status.json"),
    }
    if native_cache_read is not None:
        payload["native_cache_read_tokens"] = native_cache_read
    if latest_intent is not None:
        payload["latest_intent"] = latest_intent
    if activity_payload is not None:
        payload["activity"] = activity_payload
    if last_check_payload is not None:
        payload["last_check"] = last_check_payload
    return payload


def write(job_dir, state):
    """Atomically write <job_dir>/status.json."""
    job_dir = Path(job_dir)
    if not job_dir.is_dir():
        return None
    payload = build(job_dir, state)
    target = job_dir / "status.json"
    fd, temporary = tempfile.mkstemp(prefix=".status-", dir=job_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return payload


def format_preview(summary: dict) -> str:
    """Format a compact 1-line progress preview for parent wait heartbeats."""
    from claivelib.ansi import strip_controls

    wid = summary.get("id") or "?"
    alias = summary.get("alias")
    name = f"{wid} (@{alias})" if alias else str(wid)
    status = summary.get("status") or "unknown"
    phase = strip_controls(str(summary.get("phase") or "running"))
    elapsed = summary.get("turn_elapsed_s")
    elapsed_str = f"{elapsed:.0f}s" if isinstance(elapsed, (int, float)) else "?s"
    steps = summary.get("step_count", 0)
    files = summary.get("changed_files") or []
    files_total = summary.get("changed_files_total")
    if not isinstance(files_total, int) or isinstance(files_total, bool) or files_total < len(files):
        files_total = len(files)
    parts = [f"{name} [{status} {elapsed_str}]", f"phase={phase}", f"steps={steps}", f"files={files_total}"]
    if files:
        shown = ", ".join(strip_controls(str(f)) for f in files[:4])
        if files_total > 4:
            shown += f", +{files_total - 4} more"
        parts.append(f"touched=[{shown}]")
    active_tools = summary.get("active_tools") or {}
    if active_tools:
        active_desc = ", ".join(strip_controls(str(v.get("tool") or k)) if isinstance(v, dict) else strip_controls(str(v))
                                for k, v in list(active_tools.items())[:2])
        parts.append(f"active_tool={active_desc}")
    elif isinstance(summary.get("last_tool"), dict):
        lt = summary["last_tool"]
        tname = strip_controls(str(lt.get("tool") or lt.get("name") or "tool"))
        tstat = strip_controls(str(lt.get("status") or "done"))
        detail = strip_controls(str(lt.get("detail") or "")).strip()
        if detail:
            detail = " ".join(detail.split())[:80]
            parts.append(f"last_tool={tname}({tstat}: {detail})")
        else:
            parts.append(f"last_tool={tname}({tstat})")
    failures = summary.get("task_failures") or 0
    if failures:
        parts.append(f"cmd_failures={failures}")
    return " | ".join(parts)


def read_or_build(job_dir, state):
    """Return a fresh compact status dict (without mutating disk if read-only)."""
    return build(job_dir, state)
