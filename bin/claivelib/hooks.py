"""Deterministic lifecycle hooks (pre-turn, post-tool, stop, status) for parent harnesses."""
import inspect
import json
import os
import re

from claivelib import inbox
from claivelib.ansi import strip_controls

HARNESSES = ("generic", "claude", "agy", "codex", "pi", "opencode")
NON_REACTIVE_HARNESSES = frozenset({"codex"})
_SESSION_TOKEN_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")


def _sanitize_session_token(raw: str) -> str | None:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(raw or "").strip()).strip("-")
    if not cleaned:
        return None
    return cleaned[:64]


def _fallback_resolve_session(session=None, env=None) -> str | None:
    env_map = os.environ if env is None else env
    if session is not None:
        token = str(session).strip()
        if not token:
            return None
        if not _SESSION_TOKEN_RE.fullmatch(token):
            raise ValueError(f"invalid parent session: {session!r}")
        return token
    explicit_env = env_map.get("CLAIVE_PARENT_SESSION")
    if explicit_env and explicit_env.strip():
        return _sanitize_session_token(explicit_env)
    prefixes = (
        ("CLAUDE_SESSION_ID", "claude"),
        ("CODEX_THREAD_ID", "codex"),
        ("ANTIGRAVITY_CONVERSATION_ID", "agy"),
        ("AGY_CONVERSATION_ID", "agy"),
        ("OPENCODE_SESSION_ID", "opencode"),
        ("PI_SESSION_ID", "pi"),
    )
    for var, prefix in prefixes:
        val = env_map.get(var)
        if val and val.strip():
            return _sanitize_session_token(f"{prefix}-{val.strip()}")
    return None


def _fallback_infer_harness(env=None) -> str:
    env_map = os.environ if env is None else env
    explicit = (env_map.get("CLAIVE_PARENT_HARNESS") or "").strip().lower()
    if explicit in HARNESSES:
        return explicit
    if env_map.get("CLAUDE_SESSION_ID") or env_map.get("CLAUDECODE"):
        return "claude"
    if env_map.get("CODEX_THREAD_ID"):
        return "codex"
    if env_map.get("ANTIGRAVITY_CONVERSATION_ID") or env_map.get("AGY_CONVERSATION_ID"):
        return "agy"
    if env_map.get("OPENCODE_SESSION_ID"):
        return "opencode"
    if env_map.get("PI_SESSION_ID"):
        return "pi"
    return "generic"


def _parse_stdin_payload(stdin_data) -> dict:
    if not stdin_data:
        return {}
    if isinstance(stdin_data, dict):
        return stdin_data
    if isinstance(stdin_data, str) and stdin_data.strip():
        try:
            loaded = json.loads(stdin_data)
            return loaded if isinstance(loaded, dict) else {}
        except ValueError:
            return {}
    return {}


def _resolve_session_and_harness(session=None, harness=None, env=None, stdin_payload=None, workspace=None) -> tuple[str | None, str]:
    env_map = os.environ if env is None else env
    payload = _parse_stdin_payload(stdin_payload)
    hook_event = str(payload.get("hookEventName") or payload.get("hook_event_name") or "")
    is_agy_payload = hook_event == "PreInvocation" or bool(payload.get("conversationId"))

    if harness is not None:
        eff_harness = str(harness).strip().lower()
        if eff_harness not in HARNESSES:
            raise ValueError(f"unsupported harness {harness!r}; choose from {', '.join(HARNESSES)}")
    elif hasattr(inbox, "infer_parent_harness"):
        inferred = inbox.infer_parent_harness(env_map)
        if inferred:
            eff_harness = inferred
        elif is_agy_payload:
            eff_harness = "agy"
        elif payload.get("hook_event_name"):
            eff_harness = "claude"
        else:
            eff_harness = "generic"
    else:
        eff_harness = _fallback_infer_harness(env_map)
        if eff_harness == "generic":
            if is_agy_payload:
                eff_harness = "agy"
            elif payload.get("hook_event_name"):
                eff_harness = "claude"

    if eff_harness not in HARNESSES:
        eff_harness = "generic"

    if hasattr(inbox, "resolve_parent_session"):
        eff_session = inbox.resolve_parent_session(session, env_map)
    else:
        eff_session = _fallback_resolve_session(session, env_map)

    stdin_sid = payload.get("session_id") or payload.get("conversationId")
    if eff_session is None and isinstance(stdin_sid, str) and stdin_sid.strip():
        prefix = eff_harness if eff_harness != "generic" else "session"
        raw_sid = f"{prefix}-{stdin_sid.strip()}"
        eff_session = inbox.sanitize_session(raw_sid) if hasattr(inbox, "sanitize_session") else _sanitize_session_token(raw_sid)

    if eff_session is None:
        ws = workspace or payload.get("workspace") or payload.get("cwd")
        if ws is not None and str(ws).strip():
            if hasattr(inbox, "resolve_parent_session") and "workspace" in inspect.signature(inbox.resolve_parent_session).parameters:
                eff_session = inbox.resolve_parent_session(None, env_map, workspace=str(ws).strip())

    return eff_session, eff_harness


def _session_consumer(eff_session: str | None, eff_harness: str) -> str:
    if eff_session:
        if hasattr(inbox, "session_consumer"):
            return inbox.session_consumer(eff_session)
        return f"session-{eff_session}"[:64]
    return eff_harness


def _read_session_events(eff_session: str | None, eff_harness: str, *, peek: bool = False) -> list[dict]:
    consumer = _session_consumer(eff_session, eff_harness)
    params = inspect.signature(inbox.read_new).parameters
    kwargs = {"peek": peek}
    if "parent_session" in params:
        kwargs["parent_session"] = eff_session
    if "wake_only" in params:
        kwargs["wake_only"] = True
    events = inbox.read_new(consumer, **kwargs)
    if "parent_session" not in params and eff_session is not None:
        events = [event for event in events if event.get("parent_session") == eff_session]
    return events


def _has_unread(eff_session: str | None, eff_harness: str) -> bool:
    consumer = _session_consumer(eff_session, eff_harness)
    params = inspect.signature(inbox.has_unread).parameters
    if "parent_session" in params:
        kwargs = {"parent_session": eff_session}
        if "wake_only" in params:
            kwargs["wake_only"] = True
        return bool(inbox.has_unread(consumer, **kwargs))
    if eff_session is not None:
        return bool(_read_session_events(eff_session, eff_harness, peek=True))
    return bool(inbox.has_unread(consumer))


def _all_jobs() -> list[dict]:
    from claivelib import cli
    root_dir = cli.root(create=False)
    if not root_dir.exists():
        return []
    return cli.jobs(limit=None)


def _open_asks(eff_session: str | None) -> list[dict]:
    from claivelib import cli
    root_dir = cli.root(create=False)
    asks = []
    for state in _all_jobs():
        needs = state.get("needs_parent")
        if not needs:
            continue
        if state.get("phase") == "session closed":
            continue
        if state.get("status") in {"cancelled", "interrupted"}:
            continue
        job_id = state.get("id")
        if job_id and (root_dir / job_id / "close.request").exists():
            continue
        if eff_session is not None and state.get("parent_session") != eff_session:
            continue
        question = needs.get("question", "") if isinstance(needs, dict) else str(needs)
        kind = needs.get("kind") if isinstance(needs, dict) else None
        asks.append({
            "id": job_id,
            "label": state.get("label", ""),
            "status": state.get("status"),
            "parent_session": state.get("parent_session"),
            "kind": kind,
            "question": strip_controls(str(question)),
        })
    return asks


def _format_notification_block(events: list[dict], open_asks: list[dict]) -> str:
    sections = ["<claive-notification>"]
    if events:
        sections.append("Settled background events:")
        sections.append(strip_controls(inbox.format_text(events)))
    if open_asks:
        sections.append("Open parent questions (ASK):")
        for ask in open_asks:
            label = f" ({strip_controls(ask['label'])})" if ask.get("label") else ""
            sections.append(
                f"- {ask['id']}{label}: {ask['question']} "
                f"(answer with: claive answer {ask['id']} --message \"...\")"
            )
    sections.append("</claive-notification>")
    return "\n".join(sections)


def _is_active_running(state: dict) -> bool:
    status = state.get("status")
    if status in {"starting", "running", "cancelling"}:
        return True
    if status == "idle" and (state.get("queue_depth") or 0) > 0:
        return True
    return False


def _load_watchers_module():
    import importlib
    import sys
    if "claivelib.watchers" in sys.modules:
        return sys.modules["claivelib.watchers"]
    try:
        return importlib.import_module("claivelib.watchers")
    except ImportError:
        return None


def _watch_info(watchers_mod, eff_session: str | None):
    if watchers_mod is None or not hasattr(watchers_mod, "watch_status"):
        return None
    try:
        sig = inspect.signature(watchers_mod.watch_status)
        if "parent_session" in sig.parameters:
            return watchers_mod.watch_status(parent_session=eff_session)
        return watchers_mod.watch_status()
    except Exception:
        return None


def _worker_is_watched(watchers_mod, state: dict, watch_info) -> bool:
    if watchers_mod is not None and hasattr(watchers_mod, "is_worker_watched"):
        try:
            return bool(watchers_mod.is_worker_watched(state))
        except Exception:
            return False
    if isinstance(watch_info, dict):
        watched_ids = set()
        for key in ("watched_ids", "watched"):
            for item in watch_info.get(key) or []:
                watched_ids.add(item.get("id") if isinstance(item, dict) else item)
        if state.get("id") in watched_ids:
            return True
    return False


def _owned_active_workers(eff_session: str | None, *, all_sessions: bool = False,
                          filter_untagged_when_none: bool = True) -> list[dict]:
    jobs = _all_jobs()
    result = []
    for state in jobs:
        if not _is_active_running(state):
            continue
        if eff_session is not None:
            if state.get("parent_session") != eff_session:
                continue
        elif filter_untagged_when_none and not all_sessions:
            if state.get("parent_session") is not None:
                continue
        result.append(state)
    return result


def run_pre_turn(session=None, harness=None, peek=False, as_json=False, env=None,
                 stdin_payload=None, hook_event_name="PreToolUse", agy_hook: bool = False,
                 workspace=None) -> tuple[int, str]:
    """Emit unread inbox events and open ASK questions for the parent harness."""
    env_map = os.environ if env is None else env
    payload_in = _parse_stdin_payload(stdin_payload)
    is_agy_hook = bool(agy_hook or payload_in.get("hookEventName") == "PreInvocation")
    if env_map.get("CLAIVE_WORKER_ID"):
        return 0, "{}" if is_agy_hook else ""
    eff_session, eff_harness = _resolve_session_and_harness(
        session=session, harness=harness, env=env_map, stdin_payload=payload_in, workspace=workspace
    )
    events = _read_session_events(eff_session, eff_harness, peek=peek)
    open_asks = _open_asks(eff_session)

    if not events and not open_asks:
        if is_agy_hook:
            return 0, "{}"
        if not as_json:
            return 0, ""
        if eff_harness == "claude":
            return 0, json.dumps({"events": [], "open_asks": []})
        return 0, json.dumps({
            "harness": eff_harness,
            "session": eff_session,
            "events": [],
            "open_asks": [],
            "message": "",
        })

    formatted = _format_notification_block(events, open_asks)
    if is_agy_hook:
        return 0, json.dumps({"injectSteps": [{"systemMessage": {"systemMessage": formatted}}]})
    if not as_json:
        return 0, formatted
    if eff_harness == "claude":
        event_name = str(payload_in.get("hook_event_name") or hook_event_name)
        payload = {
            "hookSpecificOutput": {
                "hookEventName": event_name,
                "additionalContext": formatted,
            },
            "events": events,
            "open_asks": open_asks,
        }
        return 0, json.dumps(payload)
    return 0, json.dumps({
        "harness": eff_harness,
        "session": eff_session,
        "events": events,
        "open_asks": open_asks,
        "message": formatted,
    })


def run_post_tool(session=None, harness=None, peek=False, as_json=False, env=None,
                  stdin_payload=None, agy_hook: bool = False, workspace=None) -> tuple[int, str]:
    """Alias for run_pre_turn on post-tool hook points."""
    return run_pre_turn(session=session, harness=harness, peek=peek, as_json=as_json,
                        env=env, stdin_payload=stdin_payload, hook_event_name="PostToolUse",
                        agy_hook=agy_hook, workspace=workspace)


def _remediation_hint(eff_harness: str, eff_session: str | None, blocking_workers: list[dict]) -> str:
    session_flag = f" --session {eff_session}" if eff_session else ""
    first_id = blocking_workers[0]["id"] if blocking_workers else "<ID>"
    if eff_harness == "claude":
        return (f"Run 'claive wait{session_flag}' with Bash run_in_background=true, "
                f"or 'claive park {first_id}' if intentionally handing off.")
    if eff_harness == "agy":
        return (f"Run 'claive wait{session_flag}' as a background command (WaitMsBeforeAsync), "
                f"or 'claive park {first_id}' if intentionally handing off.")
    if eff_harness == "codex":
        return ("Codex cannot wake from background processes after turn end. "
                f"Run 'claive wait {first_id} --timeout 300' now, or 'claive park {first_id}' to hand off asynchronously.")
    return f"Arm 'claive wait{session_flag}' or mark 'claive park {first_id}' before stopping."


def run_stop(session=None, harness=None, mode="block", as_json=False, env=None,
             all_sessions: bool = False, stdin_payload=None, workspace=None) -> tuple[int, str]:
    """Guard against ending a turn with unread events or unwatched/unparked active workers."""
    if mode not in {"warn", "block"}:
        raise ValueError(f"invalid stop mode {mode!r}; expected 'warn' or 'block'")
    env_map = os.environ if env is None else env
    if env_map.get("CLAIVE_WORKER_ID"):
        return 0, ""
    payload_in = _parse_stdin_payload(stdin_payload)
    # Prevent infinite Stop-hook loops when Claude Code retries with stop_hook_active=true.
    effective_mode = "warn" if payload_in.get("stop_hook_active") is True else mode

    eff_session, eff_harness = _resolve_session_and_harness(
        session=session, harness=harness, env=env_map, stdin_payload=payload_in, workspace=workspace
    )
    unread_events = _read_session_events(eff_session, eff_harness, peek=False)

    watchers_mod = _load_watchers_module()
    watch_info = _watch_info(watchers_mod, eff_session)
    active_owned = _owned_active_workers(eff_session, all_sessions=all_sessions, filter_untagged_when_none=True)
    active_unparked = [w for w in active_owned if not w.get("parked")]

    if not active_unparked and isinstance(watch_info, dict):
        fallback_list = watch_info.get("active_workers") or watch_info.get("unwatched_workers") or []
        for item in fallback_list:
            if isinstance(item, dict) and not item.get("parked"):
                active_unparked.append(item)

    if eff_harness in NON_REACTIVE_HARNESSES:
        blocking_workers = active_unparked
    else:
        blocking_workers = [
            w for w in active_unparked
            if not _worker_is_watched(watchers_mod, w, watch_info)
        ]

    if not unread_events and not blocking_workers:
        if as_json:
            return 0, json.dumps({"decision": "approve", "session": eff_session})
        return 0, ""

    parts = []
    if unread_events:
        parts.append("Unread background events must be reviewed before stopping:")
        parts.append(strip_controls(inbox.format_text(unread_events)))
    if blocking_workers:
        kind_desc = "active non-parked" if eff_harness in NON_REACTIVE_HARNESSES else "unwatched active"
        parts.append(f"Blocking {kind_desc} workers ({len(blocking_workers)}):")
        for w in blocking_workers:
            label = f" ({strip_controls(str(w.get('label')))})" if w.get("label") else ""
            parts.append(f"- {w.get('id')}{label} [{w.get('status', 'running')}]")
        parts.append(_remediation_hint(eff_harness, eff_session, blocking_workers))
    elif unread_events:
        parts.append("Process the settled events above before ending the turn.")
    message = "\n".join(parts)

    exit_code = 2 if effective_mode == "block" else 0
    if not as_json:
        return exit_code, message

    payload = {
        "decision": "block" if effective_mode == "block" else "approve",
        "reason": message,
        "systemMessage": message if effective_mode == "warn" else None,
        "harness": eff_harness,
        "session": eff_session,
        "mode": effective_mode,
        "unread_events": unread_events,
        "blocking_workers": [
            {"id": w.get("id"), "label": w.get("label", ""), "status": w.get("status")}
            for w in blocking_workers
        ],
        "message": message,
    }
    return exit_code, json.dumps(payload)


def run_status(session=None, harness=None, as_json=False, env=None,
               all_sessions: bool = False, workspace=None) -> tuple[int, str]:
    """Report session, harness, live watchers, worker watch/park breakdown, open ASKs, and unread state."""
    eff_session, eff_harness = _resolve_session_and_harness(
        session=session, harness=harness, env=env, workspace=workspace
    )
    watchers_mod = _load_watchers_module()
    watch_info = _watch_info(watchers_mod, eff_session)

    live_watchers = []
    if isinstance(watch_info, dict) and isinstance(watch_info.get("watchers"), list):
        live_watchers = list(watch_info["watchers"])
    elif watchers_mod is not None and hasattr(watchers_mod, "live_watchers"):
        try:
            live_watchers = list(watchers_mod.live_watchers())
        except Exception:
            live_watchers = []

    active = _owned_active_workers(
        eff_session,
        all_sessions=all_sessions or (eff_session is None),
        filter_untagged_when_none=False,
    )
    parked = [w for w in active if w.get("parked")]
    unparked = [w for w in active if not w.get("parked")]
    watched = [w for w in unparked if _worker_is_watched(watchers_mod, w, watch_info)]
    unwatched = [w for w in unparked if not _worker_is_watched(watchers_mod, w, watch_info)]
    open_asks = _open_asks(eff_session)
    unread = _has_unread(eff_session, eff_harness)

    summary = {
        "session": eff_session,
        "harness": eff_harness,
        "reactive": eff_harness not in NON_REACTIVE_HARNESSES,
        "unread": unread,
        "watchers": live_watchers,
        "counts": {
            "active": len(active),
            "watched": len(watched),
            "unwatched": len(unwatched),
            "parked": len(parked),
            "open_asks": len(open_asks),
            "live_watchers": len(live_watchers),
        },
        "watched_workers": [{"id": w.get("id"), "label": w.get("label", ""), "status": w.get("status")} for w in watched],
        "unwatched_workers": [{"id": w.get("id"), "label": w.get("label", ""), "status": w.get("status")} for w in unwatched],
        "parked_workers": [{"id": w.get("id"), "label": w.get("label", ""), "status": w.get("status")} for w in parked],
        "open_asks": open_asks,
    }
    if as_json:
        return 0, json.dumps(summary)

    lines = [
        f"session={eff_session or 'none'} harness={eff_harness} "
        f"unread={'yes' if unread else 'no'} watchers={len(live_watchers)}",
        f"workers: active={len(active)} watched={len(watched)} "
        f"unwatched={len(unwatched)} parked={len(parked)} open_asks={len(open_asks)}",
    ]
    for w in unwatched:
        lines.append(f"  unwatched: {w.get('id')} ({strip_controls(str(w.get('label', '')))}) [{w.get('status')}]")
    for w in parked:
        lines.append(f"  parked: {w.get('id')} ({strip_controls(str(w.get('label', '')))}) [{w.get('status')}]")
    for ask in open_asks:
        lines.append(f"  ask: {ask.get('id')}: {ask.get('question')}")
    return 0, "\n".join(lines)
