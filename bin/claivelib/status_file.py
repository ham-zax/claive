"""Compact atomic status.json projection for zero-shell worker inspection."""
import json
import os
from pathlib import Path
import tempfile
import time

from claivelib import acceptance, redaction


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

    turn_usage = state.get("turn_usage") if isinstance(state.get("turn_usage"), dict) else {}
    native_cache_read = turn_usage.get("native_cache_read_tokens")

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
        "verification_status": verification.get("status", "not_run"),
        "acceptance_status": acceptance_doc.get("status", "pending"),
        "report_state": state.get("report_state"),
        "report": report,
        "status_file": str(job_dir / "status.json"),
    }
    if native_cache_read is not None:
        payload["native_cache_read_tokens"] = native_cache_read
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
    parts = [f"{name} [{status} {elapsed_str}]", f"phase={phase}", f"steps={steps}", f"files={len(files)}"]
    if files:
        shown = ", ".join(strip_controls(str(f)) for f in files[:4])
        if len(files) > 4:
            shown += f", +{len(files) - 4} more"
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
