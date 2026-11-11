"""Suspend unused supervisors while retaining their durable session and work."""
import json
from pathlib import Path
import time

from claivelib import messaging, wakes


PHASE = "idle ttl closed"


def protected(path, state):
    if state.get("needs_parent") or (path / "close.request").exists():
        return "awaiting parent or closing"
    if any((path / "requests").glob("*.json")) or any((path / "messages" / "pending").glob("*.json")):
        return "queued work"
    # Cancelled monitors are removed. Any retained record, including a corrupt one,
    # conservatively prevents automatic session suspension.
    if any((path / "monitors").glob("*.json")):
        return "attached monitor"
    for file in wakes._directory(path).glob("*.json"):
        try:
            if json.loads(file.read_text()).get("status") == "scheduled":
                return "scheduled wake"
        except (OSError, ValueError, AttributeError):
            return "unreadable schedule"
    return None


def expire(path, age):
    from claivelib import cli, config, turns
    with wakes._lock(path), messaging.lock(path), turns.request_lock(path):
        state = cli.load(path, check_alive=False)
        ttl = state.get("idle_ttl", config.launch_setting("idle_ttl", 7200))
        if (state.get("status") != "idle" or not state.get("reusable") or not ttl
                or age < ttl or protected(path, state)):
            return False
        cli.release_worktree(state)
        state.update(status=state.get("last_turn_status", "completed"), phase=PHASE,
                     ended_at=time.time(), suspended_at=time.time())
        cli.save(path / "state.json", state)
        return True


def resume(path):
    """Called under the message lock; manual closes are deliberately not reopened."""
    from claivelib import cli
    from claivelib.engines import get_engine
    state = cli.load(path, check_alive=False)
    if state.get("phase") != PHASE:
        return state
    pid, recorded = state.get("supervisor_pid"), state.get("supervisor_identity")
    deadline = time.monotonic() + 3
    while pid and cli.identity(pid) == recorded:
        if time.monotonic() >= deadline:
            raise ValueError("idle supervisor is still closing; retry the message shortly")
        time.sleep(.02)
    engine = get_engine(state["engine"])
    isolation = state.get("launch", {}).get("isolation") or {}
    target = isolation.get("existing_path")
    if target and not Path(target).is_dir():
        if isolation.get("owner") != "claive":
            raise ValueError("retained session workspace is unavailable; restore it before resuming")
        launch = dict(state["launch"], isolation=dict(mode="create", base=isolation["base_commit"]))
        from claivelib import worktrees
        with worktrees.lifecycle_lock():
            state["launch"] = engine.prepare_isolation(launch, state["workspace"], str(path))
        state["actual_workspace"] = state["launch"]["isolation"]["existing_path"]
        state.pop("retained_worktree", None)
    state.update(status="idle", phase="resuming idle session", resumed_at=time.time())
    for field in ("supervisor_pid", "supervisor_identity", "worker_pid", "worker_identity", "ended_at"):
        state.pop(field, None)
    cli.save(path / "state.json", state)
    try:
        cli.spawn_supervisor(path, state, "_reusable_idle")
    except (OSError, ValueError):
        saved = cli.load(path, check_alive=False)
        pid = saved.get("supervisor_pid")
        if not pid or cli.identity(pid) != saved.get("supervisor_identity"):
            saved.update(phase=PHASE, status=saved.get("last_turn_status", "completed"), ended_at=time.time())
            cli.save(path / "state.json", saved)
        raise
    return cli.load(path, check_alive=False)


def cleanup(older_than, dry_run=False, json_output=False):
    from claivelib import cli, turns
    seconds = wakes.parse_duration(older_than)
    if seconds is None:
        raise ValueError("--idle-older-than requires a duration such as 2h")
    rows = []
    for item in cli.jobs(limit=None):
        if item.get("status") != "idle" or not item.get("reusable"):
            continue
        path = cli.job_path(item["id"])
        with wakes._lock(path), messaging.lock(path), turns.request_lock(path):
            state = cli.load(path)
            age = time.time() - (state.get("last_turn_ended_at") or state.get("last_activity") or state["started_at"])
            if state["status"] != "idle" or age < seconds:
                continue
            reason = protected(path, state)
            action = "skipped" if reason else "would_close" if dry_run else "closing"
            if not reason and not dry_run:
                cli.save(path / "close.request", {"requested_at": time.time(), "reason": "idle cleanup"})
            rows.append(dict(id=state["id"], action=action, reason=reason))
    if json_output:
        print(json.dumps(rows, indent=2))
    else:
        for row in rows:
            print(f"{row['id']} {row['action']}" + (f": {row['reason']}" if row['reason'] else ""))
        if not rows:
            print("No eligible idle supervisors.")
    return 0
