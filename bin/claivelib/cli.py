#!/usr/bin/python3
"""Track reusable coding workers and display their progress beside any parent agent. No dependencies."""

import argparse
import dataclasses
import datetime
import importlib
import json
import os
from pathlib import Path
import re
import selectors
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid

from claivelib import config, provider
from claivelib.engine import TurnRequest
from claivelib.engines import DEFAULT_ENGINE, default_engine, get_engine
from claivelib.roles import ROLES, resolved_roles
from claivelib.state import SCHEMA_VERSION, launch_config, session_id as worker_session_id


ACTIVE = {"starting", "running", "cancelling", "idle"}
# Seconds between SIGTERM and SIGKILL for a worker process group that is being stopped.
KILL_GRACE_SECONDS = float(os.environ.get("CLAIVE_KILL_GRACE", "3"))
SCRIPT = None
REPORT_CONTRACT = (
    "End your final answer with exactly one fenced block tagged claive-report "
    "containing one JSON object:\n"
    '{"status": "done" | "blocked" | "needs_decision", '
    '"summary": "<one or two sentences>", '
    '"changed_files": ["path", ...], '
    '"commands_run": [{"command": "...", "exit_code": 0}], '
    '"residual_risks": ["..."], '
    '"question": "<required when status is blocked or needs_decision>"}\n'
    "Use needs_decision instead of guessing when a choice would change design, "
    "public behaviour, data formats, scope, or acceptance criteria."
)
PREFERRED_MODELS = ("mimo-v2.6-flash-free", "big-pickle", "space-bunny-free",
                    "muse-spark-1.3-contributor-free")


def disallowed_model(model):
    name = str(model or "").lower()
    return "nemotron" in name or name.startswith("ling-3.1-flash")


def check_model_policy(model):
    if model and disallowed_model(model):
        raise ValueError(f"model {model} is disallowed by claive policy")


def parse_turn_timeout(raw):
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise ValueError("--turn-timeout must be a positive number")
    try:
        value = float(raw) if isinstance(raw, str) else float(raw)
    except (TypeError, ValueError):
        raise ValueError("--turn-timeout must be a positive number")
    if not value > 0:
        raise ValueError("--turn-timeout must be a positive number")
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError("--turn-timeout must be a positive number")
    if float(value).is_integer():
        return int(value)
    return float(value)


def parse_fallback_models(raw):
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        models = [str(item).strip() for item in raw]
    else:
        models = [part.strip() for part in str(raw).split(",")]
    models = [model for model in models if model]
    for model in models:
        check_model_policy(model)
    return models


def check_nested():
    if os.environ.get("CLAIVE_WORKER_ID") and os.environ.get("CLAIVE_ALLOW_NESTED") != "1":
        raise ValueError("claive workers may not launch workers (CLAIVE_WORKER_ID is set)")


def _report_status(state):
    if state.get("report_state") == "ok":
        return (state.get("report") or {}).get("status")
    return None


def _write_turn_event(state):
    try:
        from claivelib import inbox as inbox_mod
        status = state.get("status")
        if status == "idle":
            status = state.get("last_turn_status", "failed")
        event = inbox_mod.turn_event(state["id"], state.get("label", ""), state.get("turn", 1),
                                     status, outcome_code(state), state.get("failure_kind"),
                                     state.get("needs_parent"), _report_status(state),
                                     state.get("batch"), state.get("mission"))
        inbox_mod.append(event)
    except Exception:
        pass


def parse_report(text):
    blocks = re.findall(r"```claive-report(.*?)```", text or "", re.S)
    if not blocks:
        return "missing", None, None
    try:
        raw = json.loads(blocks[-1].strip())
    except ValueError:
        return "invalid", None, "report is not valid JSON"
    if not isinstance(raw, dict):
        return "invalid", None, "report is not a JSON object"
    status = raw.get("status")
    if status not in {"done", "blocked", "needs_decision"}:
        return "invalid", None, "invalid report status"
    summary = raw.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return "invalid", None, "summary must be a non-empty string"
    for field in ("changed_files", "residual_risks"):
        if field in raw:
            value = raw[field]
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                return "invalid", None, f"{field} must be a list of strings"
    if "commands_run" in raw:
        commands = raw["commands_run"]
        if not isinstance(commands, list):
            return "invalid", None, "commands_run must be a list"
        for item in commands:
            if (not isinstance(item, dict) or not isinstance(item.get("command"), str)
                    or not isinstance(item.get("exit_code"), int)
                    or isinstance(item.get("exit_code"), bool)):
                return "invalid", None, "commands_run entries need command and exit_code"
    question = raw.get("question")
    if status != "done":
        if not isinstance(question, str) or not question.strip():
            return "invalid", None, "question is required unless done"
    elif question is not None and not isinstance(question, str):
        return "invalid", None, "question must be a string"
    report = dict(status=status, summary=summary,
                  changed_files=list(raw.get("changed_files") or []),
                  commands_run=list(raw.get("commands_run") or []),
                  residual_risks=list(raw.get("residual_risks") or []),
                  question=raw.get("question") if "question" in raw else None)
    return "ok", report, None


def record_report(state, answer):
    text = answer if isinstance(answer, str) else json.dumps(answer)
    report_state, parsed, reason = parse_report(text)
    state["report_state"] = report_state
    if report_state == "ok":
        state["report"] = parsed
        if parsed["status"] in {"needs_decision", "blocked"}:
            state["needs_parent"] = {"kind": parsed["status"], "question": parsed["question"]}
    elif report_state == "invalid":
        state["report_error"] = reason


def set_launcher_path(path):
    global SCRIPT
    SCRIPT = str(Path(path).resolve())


def launcher_path():
    if not SCRIPT:
        raise ValueError("claive launcher path is not configured")
    return SCRIPT


def turn_request(state, prompt_file=None, reasoning_effort=None, max_model_steps=None, isolation=None):
    launch = launch_config(state)
    return TurnRequest(
        binary=launch["binary"],
        workspace=state["workspace"],
        prompt_file=prompt_file or state["prompt_file"],
        session_id=worker_session_id(state),
        provider=launch["provider"],
        model=launch.get("model"),
        reasoning_effort=reasoning_effort or state["reasoning_effort"],
        max_model_steps=state.get("max_model_steps", 100) if max_model_steps is None else max_model_steps,
        read_only=bool(launch.get("read_only")),
        web=bool(launch.get("web")),
        output_schema=launch.get("output_schema"),
        session_logging=bool(launch.get("session_logging", True)),
        isolation=isolation if isolation is not None else dict(launch.get("isolation") or {}),
        session_dir=launch.get("session_dir"),
    )


def root():
    base = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
    path = Path(os.environ.get("CLAIVE_DIR", str(base / "claive")))
    if not path.is_absolute():
        raise ValueError("CLAIVE_DIR must be absolute")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def save(path, data):
    fd, temporary = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream, ensure_ascii=True)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def identity(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        return f"{boot}:{fields[19]}"
    except (OSError, IndexError):
        return None


def job_path(job_id):
    if not re.fullmatch(r"[0-9a-f]{12}", job_id):
        raise ValueError("invalid worker ID")
    path = root() / job_id
    if not (path / "state.json").is_file():
        raise ValueError(f"unknown worker: {job_id}")
    return path


def load(path, check_alive=True):
    state = json.loads((path / "state.json").read_text())
    if check_alive and state["status"] in ACTIVE:
        pid = state.get("supervisor_pid")
        if pid and identity(pid) != state.get("supervisor_identity"):
            state["status"] = "interrupted"
            state["error"] = "worker supervisor exited without a final result"
            state["failure_kind"] = "interrupted"
        elif not pid and time.time() - state["started_at"] > 5:
            state["status"] = "interrupted"
            state["error"] = "worker supervisor did not start"
            state["failure_kind"] = "interrupted"
    return state


def jobs(limit=20):
    records = []
    for path in root().iterdir():
        if path.is_dir() and re.fullmatch(r"[0-9a-f]{12}", path.name):
            try:
                records.append(load(path))
            except (OSError, ValueError, KeyError):
                continue
    records.sort(key=lambda item: item["started_at"], reverse=True)
    active = [item for item in records if item["status"] in ACTIVE]
    finished = [item for item in records if item["status"] not in ACTIVE]
    return active + finished[:limit]


def clean(value):
    return "".join(char for char in str(value) if char.isprintable())


def duration(seconds):
    seconds = max(0, int(seconds))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02}m" if hours else f"{minutes}m{seconds:02}s"


def summary(records):
    running = sum(item["status"] in ACTIVE - {"idle"} for item in records)
    idle = sum(item["status"] == "idle" for item in records)
    done = sum(item["status"] == "completed" for item in records)
    failed = sum(item["status"] in {"failed", "interrupted"} for item in records)
    stopped = sum(item["status"] == "cancelled" for item in records)
    needy = sum(1 for item in records if item.get("needs_parent"))
    idle_text = f" | {idle} idle" if idle else ""
    text = f"Workers: {running} running{idle_text} | {done} done | {failed} failed | {stopped} stopped"
    return f"{text} | {needy} need you" if needy else text


def terminal_size():
    # tmux may leave COLUMNS/LINES at the detached session's original size.
    try:
        return os.get_terminal_size(sys.stdout.fileno())
    except (OSError, ValueError):
        return os.terminal_size((100, 25))


def compact_view(records, color=False, width=None, height=5):
    width = width or terminal_size().columns
    height = max(1, height)
    badges = {"starting": "WAIT", "running": "RUN", "cancelling": "STOPPING", "idle": "IDLE",
              "completed": "OK", "failed": "FAIL", "interrupted": "LOST", "cancelled": "STOP"}
    palette = {"running": "36", "starting": "36", "idle": "36", "cancelling": "33", "completed": "32",
               "failed": "31", "interrupted": "31", "cancelled": "33"}

    def fit(text):
        return text if len(text) <= width else text[:max(0, width - 1)] + "…"

    lines = [fit(summary(records))]
    capacity = max(0, height - 2)
    for state in records[:capacity]:
        status = state["status"]
        age = duration(state.get("ended_at", time.time()) - state["started_at"])
        badge = "ASK" if state.get("needs_parent") else badges.get(status, status.upper())
        warning = f" !{state['task_failures']}" if state.get("task_failures") else ""
        phase = " | " + clean(state.get("phase", "working")) if status in ACTIVE else ""
        line = fit(f"{badge:7} {state['id']} {age:7} {clean(state['label'])}{warning}"
                   f" [{clean(Path(state['workspace']).name)}]{phase}")
        if color:
            line = f"\033[{palette.get(status, '0')}m{line}\033[0m"
        lines.append(line)
    hidden = max(0, len(records) - capacity)
    if height > 1:
        hint = f"+{hidden} more | " if hidden else ""
        lines.append(fit(hint + "claive: show ID | logs ID | cancel ID | list"))
    return "\n".join(lines)


def render(records, color=False, height=None):
    width = terminal_size().columns
    palette = {"running": "36", "starting": "36", "idle": "36", "cancelling": "33",
               "completed": "32", "failed": "31", "interrupted": "31",
               "cancelled": "33"}
    lines = [summary(records), "STATE        ID            ELAPSED   STEPS  TASK / WORKSPACE"]
    hidden = 0
    for index, state in enumerate(records):
        needed = 2 if state["status"] in ACTIVE else 1
        if height is not None and len(lines) + needed > max(2, height - 3):
            hidden = len(records) - index
            break
        status = state["status"]
        age = duration(state.get("ended_at", time.time()) - state["started_at"])
        shown = "ASK" if state.get("needs_parent") else status.upper()
        badge = shown.ljust(12)
        if color:
            badge = f"\033[{palette.get(status, '0')}m{badge}\033[0m"
        label = clean(state["label"])
        if state.get("task_failures"):
            reasons = state.get("task_failure_reasons") or []
            if reasons:
                last = str(reasons[-1])
                if len(last) > 60:
                    last = last[:60] + "..."
                label += f" [!{state['task_failures']} task failures: {clean(last)}]"
            else:
                label += f" [!{state['task_failures']} task failures]"
        workspace = clean(Path(state["workspace"]).name)
        prefix = f"{shown:12} {state['id']}  {age:8}  {state.get('steps', 0):5}  "
        detail = f"{label} / {workspace}"[:max(8, width - len(prefix))]
        lines.append(f"{badge} {state['id']}  {age:8}  {state.get('steps', 0):5}  {detail}")
        if status in ACTIVE:
            idle = duration(time.time() - state.get("last_activity", state["started_at"]))
            lines.append(f"  {clean(state.get('phase', 'starting'))} | last event {idle} ago"[:width])
    if not records:
        lines.append("No tracked workers yet. Jobs launched through claive appear here.")
    lines.extend([f"{hidden} more: claive list" if hidden else "Active + 20 recent results; done = CLI completed, verify the work.",
                  "claive: show ID | logs ID | cancel ID | wait ID"])
    return "\n".join(lines)


TASK_FAILURE_REASONS = 5


def event_update(state, event):
    kind = event.get("type", "activity")
    state["last_event"] = event.get("native_kind", kind)
    state["last_activity"] = time.time()
    if kind == "model_step":
        state["steps"] += 1
        state["phase"] = "model working"
    elif kind == "tool_started":
        state["phase"] = "tool: " + str(event.get("tool", "tool"))
    elif kind == "output_delta":
        state["phase"] = "producing answer"
    elif kind == "task_warning":
        state["task_failures"] += 1
        state["phase"] = "task failure reported"
        detail = " ".join(str(event.get("reason") or "").split())
        if event.get("tool"):
            detail = f"{event['tool']}: {detail}" if detail else str(event["tool"])
        if detail:
            recent = state.get("task_failure_reasons", [])[-(TASK_FAILURE_REASONS - 1):]
            state["task_failure_reasons"] = recent + [detail[:300]]
    elif kind in {"terminal_completed", "terminal_failed"}:
        state["terminal"] = event.get("terminal")
        state["terminal_reason"] = event.get("reason")
        state["answer"] = event.get("text", "")
        state["phase"] = "finishing"
    elif kind == "session_bound":
        state["session_id"] = event["session_id"]  # engines that choose their own session ID
    elif kind == "quota_exhausted":
        state["quota_exhausted"] = True
        state["fallback_requires_user_approval"] = True
        if event.get("reset_at"):
            state["quota_reset_at"] = event["reset_at"]


FALLBACKABLE_FAILURES = {"timeout", "quota", "worker", "protocol"}

PER_TURN_RESET_KEYS = ("ended_at", "error", "terminal", "terminal_reason", "quota_exhausted",
                       "quota_reset_at", "fallback_requires_user_approval", "report", "report_state",
                       "report_error", "needs_parent", "failure_kind")


def rebuild_for_fallback(state, engine, new_model):
    launch = launch_config(state)
    provider = launch.get("provider")
    read_only = bool(launch.get("read_only"))
    web = bool(launch.get("web"))
    output_schema = launch.get("output_schema")
    session_logging = bool(launch.get("session_logging", True))
    isolation = dict(launch.get("isolation") or {})
    workspace = state["workspace"]
    prompt_file = state["prompt_file"]
    effort = state["reasoning_effort"]
    steps = state.get("max_model_steps")
    fresh_session = engine.resolve_session_id(None, session_logging)
    fresh_launch = engine.resolve_launch(
        provider=provider, model=new_model, read_only=read_only, web=web,
        output_schema=output_schema, session_logging=session_logging,
        isolation=isolation, session_id=fresh_session,
        session_root=str(root() / "sessions"), workspace=workspace,
    )
    request = TurnRequest(
        binary=fresh_launch["binary"], workspace=workspace, prompt_file=prompt_file,
        session_id=fresh_session, provider=fresh_launch["provider"], model=fresh_launch["model"],
        reasoning_effort=effort, max_model_steps=steps,
        read_only=fresh_launch["read_only"], web=fresh_launch["web"],
        output_schema=fresh_launch["output_schema"],
        session_logging=fresh_launch["session_logging"], isolation=fresh_launch["isolation"],
        session_dir=fresh_launch.get("session_dir"),
    )
    command = engine.build_command(request)
    return fresh_launch, command, fresh_session


def supervise(job_id, control=None):
    path = job_path(job_id)
    state = load(path, check_alive=False)
    for key in PER_TURN_RESET_KEYS:
        state.pop(key, None)
    state["malformed_events"] = 0
    (path / "result.txt").unlink(missing_ok=True)
    engine = get_engine(state.get("engine", DEFAULT_ENGINE))
    if not isinstance(state.get("fallbacks"), list):
        state["fallbacks"] = []
    if not isinstance(state.get("fallback_models"), list):
        fallback_raw = state.get("fallback_models")
        state["fallback_models"] = parse_fallback_models(fallback_raw) if fallback_raw else []
    state.update(supervisor_pid=os.getpid(), supervisor_identity=identity(os.getpid()),
                 status="running", phase="launching worker")
    stopping = False
    child = None
    timed_out = False

    def stop_requested(_signum, _frame):
        nonlocal stopping
        stopping = True
        if control is not None:
            control["stopping"] = True

    signal.signal(signal.SIGTERM, stop_requested)
    signal.signal(signal.SIGINT, stop_requested)
    # A foreground run whose terminal closes gets SIGHUP; stop the worker (it is in its own
    # session and would outlive us) and release the worktree instead of dying mid-turn.
    signal.signal(signal.SIGHUP, stop_requested)
    save(path / "state.json", state)
    attempt = 0
    try:
        while True:
            if attempt > 0:
                for key in PER_TURN_RESET_KEYS:
                    state.pop(key, None)
                state.pop("answer", None)
                state.pop("exit_code", None)
                state["malformed_events"] = 0
                (path / "result.txt").unlink(missing_ok=True)
                state.update(status="running", phase="launching worker")
                save(path / "state.json", state)
            turn_timeout = state.get("turn_timeout")
            if turn_timeout is not None:
                try:
                    turn_timeout = parse_turn_timeout(turn_timeout)
                except ValueError:
                    turn_timeout = None
            log_mode = "a" if (state.get("reusable") or attempt > 0) else "w"
            child = None
            timed_out = False
            kill_at = None
            turn_start = None
            child_env = dict(os.environ, CLAIVE_WORKER_ID=job_id)
            if state.get("engine") == "pi":
                # Rebuilt every turn so models.json edits and config changes take effect.
                overlay = provider.overlay_agent_dir(root())
                if overlay:
                    child_env["PI_CODING_AGENT_DIR"] = overlay
                    state["provider_base_url"] = provider.override_url()
                else:
                    state.pop("provider_base_url", None)
            with (path / "events.jsonl").open(log_mode) as events, (path / "stderr.log").open(log_mode) as errors:
                child = subprocess.Popen(state["command"], stdout=subprocess.PIPE, stderr=errors,
                                         stdin=subprocess.DEVNULL, start_new_session=True,
                                         cwd=state["workspace"], env=child_env)
                turn_start = time.monotonic()
                state["worker_pid"] = child.pid
                state["worker_identity"] = identity(child.pid)
                state["phase"] = "waiting for first event"
                save(path / "state.json", state)
                pending = b""
                last_save = 0
                with selectors.DefaultSelector() as selector:
                    selector.register(child.stdout, selectors.EVENT_READ)
                    while selector.get_map() or child.poll() is None:
                        now = time.monotonic()
                        if (turn_timeout is not None and not timed_out and not stopping
                                and child.poll() is None and now - turn_start > turn_timeout):
                            timed_out = True
                            state["status"] = "cancelling"
                            state["phase"] = "stopping worker"
                            save(path / "state.json", state)
                            kill_at = now
                            try:
                                os.killpg(child.pid, signal.SIGTERM)
                            except ProcessLookupError:
                                pass
                        elif stopping and kill_at is None:
                            state["status"] = "cancelling"
                            state["phase"] = "stopping worker"
                            save(path / "state.json", state)
                            kill_at = time.monotonic()
                            try:
                                os.killpg(child.pid, signal.SIGTERM)
                            except ProcessLookupError:
                                pass
                        if kill_at is not None and time.monotonic() - kill_at > KILL_GRACE_SECONDS:
                            try:
                                os.killpg(child.pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                        for key, _mask in selector.select(0.25):
                            chunk = os.read(key.fd, 65536)
                            if not chunk:
                                selector.unregister(key.fileobj)
                                continue
                            events.write(chunk.decode("utf-8", errors="replace"))
                            events.flush()
                            pending += chunk
                            while b"\n" in pending:
                                line, pending = pending.split(b"\n", 1)
                                if line.strip():
                                    try:
                                        event = json.loads(line)
                                        if not isinstance(event, dict):
                                            raise ValueError("event is not an object")
                                        for normalized_event in engine.normalize_event(event):
                                            event_update(state, normalized_event)
                                    except (ValueError, UnicodeDecodeError):
                                        state["malformed_events"] += 1
                        if time.monotonic() - last_save > 0.25:
                            save(path / "state.json", state)
                            last_save = time.monotonic()
                if pending.strip():
                    try:
                        event = json.loads(pending)
                        if not isinstance(event, dict):
                            raise ValueError("event is not an object")
                        for normalized_event in engine.normalize_event(event):
                            event_update(state, normalized_event)
                    except (ValueError, UnicodeDecodeError):
                        state["malformed_events"] += 1
                state["exit_code"] = child.wait()
                child.stdout.close()
            actual = engine.discover_workspace(
                state["command"], (path / "stderr.log").read_text(errors="replace")
            )
            if actual:
                state["actual_workspace"] = actual
            answer = state.pop("answer", "")
            (path / "result.txt").write_text(answer if isinstance(answer, str) else json.dumps(answer))
            if state.get("reusable"):
                (path / f"turn-{state['turn']:04}.txt").write_text(
                    answer if isinstance(answer, str) else json.dumps(answer))
            if stopping:
                state["status"] = "cancelled"
            elif timed_out:
                state["status"] = "failed"
                state["failure_kind"] = "timeout"
                state["error"] = f"turn timed out after {'%g' % turn_timeout}s"
            elif state["exit_code"] != 0 or state.get("terminal") != "completed" or state["malformed_events"]:
                state["status"] = "failed"
                state["error"] = (f"exit={state['exit_code']}; terminal={state.get('terminal', 'missing')}; "
                                  f"malformed events={state['malformed_events']}")
                if state.get("quota_exhausted"):
                    state["failure_kind"] = "quota"
                elif state.get("malformed_events") or state.get("terminal") is None:
                    state["failure_kind"] = "protocol"
                else:
                    state["failure_kind"] = "worker"
            else:
                state["status"] = "completed"
            if state.get("report_contract"):
                record_report(state, answer)
            if (state["status"] == "failed" and not stopping
                    and state.get("failure_kind") in FALLBACKABLE_FAILURES):
                fallbacks = state.get("fallbacks")
                if not isinstance(fallbacks, list):
                    fallbacks = []
                    state["fallbacks"] = fallbacks
                fallback_models = state.get("fallback_models") or []
                if len(fallbacks) < len(fallback_models):
                    next_model = fallback_models[len(fallbacks)]
                    try:
                        fresh_launch, fresh_command, fresh_session = rebuild_for_fallback(
                            state, engine, next_model)
                    except Exception:
                        break
                    fallbacks.append({"model": state.get("model"),
                                      "failure_kind": state.get("failure_kind"),
                                      "error": state.get("error")})
                    state["fallbacks"] = fallbacks
                    state["model"] = fresh_launch.get("model")
                    state["launch"] = fresh_launch
                    state["command"] = fresh_command
                    state["session_id"] = fresh_session
                    save(path / "state.json", state)
                    attempt += 1
                    continue
            break
    except Exception as error:
        if timed_out and not stopping:
            state["status"] = "failed"
            state["failure_kind"] = "timeout"
            turn_timeout = state.get("turn_timeout")
            try:
                label = "%g" % parse_turn_timeout(turn_timeout)
            except ValueError:
                label = str(turn_timeout)
            state["error"] = f"turn timed out after {label}s"
            if state.get("report_contract"):
                record_report(state, state.get("answer", ""))
        else:
            state["status"] = "cancelled" if stopping else "failed"
            state["error"] = str(error)
            if state["status"] == "failed":
                state["failure_kind"] = "supervisor" if child is not None else "launch"
                if state.get("report_contract"):
                    record_report(state, state.get("answer", ""))
        if child is not None:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                child.wait()
            except Exception:
                pass
    state.pop("answer", None)
    state["ended_at"] = time.time()
    state["phase"] = state["status"]
    if control is None or stopping:
        release_worktree(state)
    save(path / "state.json", state)
    _write_turn_event(state)
    return outcome_code(state)


def outcome_code(state):
    if state["status"] == "idle":
        state = dict(state, status=state.get("last_turn_status", "failed"))
    if state["status"] == "completed":
        return 3 if state.get("needs_parent") else 0
    if state["status"] == "cancelled":
        return 130
    code = state.get("exit_code")
    return min(code, 255) if isinstance(code, int) and code > 0 else 1


def _worktree_git(cwd, *args):
    return subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=120)


DEPENDENCY_DIRS = {"node_modules", ".venv", "venv", ".tox", ".nox"}  # ignored and reinstallable


def _worktree_changes(target, base_commit):
    """Return why a writer worktree must be kept, or None when it holds nothing new.

    Untracked and ignored files count, except tool caches and reinstallable dependency trees;
    a failed git query counts too.
    """
    from claivelib.orchestration import BYPRODUCT_DIRS
    status = _worktree_git(target, "status", "--porcelain", "--untracked-files=all", "--ignored")
    if status.returncode:
        return "git status failed"
    for line in status.stdout.splitlines():
        parts = line[3:].strip('"').rstrip("/").split("/")
        if line.startswith("!!") and (set(parts) & (BYPRODUCT_DIRS | DEPENDENCY_DIRS)
                                      or parts[-1].endswith((".pyc", ".pyo"))):
            continue
        return "has changes"
    ahead = _worktree_git(target, "rev-list", "--count", f"{base_commit}..HEAD")
    if ahead.returncode or ahead.stdout.strip() != "0":
        return "has new commits"
    return None


def remove_worktree(isolation):
    """Remove a claive-created worktree (and its branch); return an error string when git refuses."""
    target, source, branch = isolation["existing_path"], isolation["source"], isolation.get("branch")
    removed = _worktree_git(source, "worktree", "remove", "--force", target)
    if removed.returncode:
        return (removed.stderr or removed.stdout).strip() or "git worktree remove failed"
    if branch:
        _worktree_git(source, "branch", "-D", branch)
    return None


def release_worktree(state):
    """Remove the worktree claive created for this worker (see MuseEngine.prepare_isolation).

    Runs before the worker's terminal state is saved, so `claive wait` sees `retained_worktree`.
    A writer's worktree and branch are kept when it has changes or new commits, and any
    worktree is kept when git refuses to remove it.
    """
    isolation = launch_config(state).get("isolation") or {}
    if (isolation.get("owner") != "claive" or state.get("retained_worktree")
            or not Path(isolation.get("existing_path") or "").is_dir()):
        return
    target, source, branch = isolation["existing_path"], isolation["source"], isolation.get("branch")
    reason = _worktree_changes(target, isolation["base_commit"]) if branch else None
    if reason is None:
        error = remove_worktree(isolation)
        if error is None:
            return
        reason = f"could not be removed ({error})"
    state["retained_worktree"] = target
    label = f"worktree {target}" + (f" (branch {branch})" if branch else "")
    try:
        print(f"Worker {state['id']}: {label} {reason} and is kept; remove it with: "
              f"git -C {shlex.quote(source)} worktree remove {shlex.quote(target)}", flush=True)
    except OSError:
        pass  # the terminal may be gone (SIGHUP); retained_worktree records it anyway


def run_supervisor(job_id, reusable):
    try:
        return reusable_worker(job_id) if reusable else supervise(job_id)
    finally:
        # Normal exits released the worktree already; this covers a supervisor that raised.
        # A failure here must not replace the supervisor's own result or exception.
        try:
            path = job_path(job_id)
            state = load(path, check_alive=False)
            before = state.get("retained_worktree")
            release_worktree(state)
            if state.get("retained_worktree") != before:
                save(path / "state.json", state)
        except Exception as error:
            print(f"Worker {job_id}: worktree cleanup failed: {error}", file=sys.stderr, flush=True)


def reusable_worker(job_id):
    """Keep one supervisor and durable worker session available between turns."""
    path = job_path(job_id)
    control = {"stopping": False}
    while True:
        supervise(job_id, control)
        state = load(path, check_alive=False)
        if control["stopping"] or state["status"] == "cancelled":
            return 130
        state["last_turn_status"] = state["status"]
        state["last_turn_ended_at"] = state.pop("ended_at")
        state.update(status="idle", phase=f"turn {state['turn']} {state['last_turn_status']}; ready for follow-up")
        save(path / "state.json", state)
        print(f"Worker {job_id}: turn {state['turn']} {state['last_turn_status']}; "
              f"idle in session {worker_session_id(state)}", flush=True)
        report(state)
        while not control["stopping"]:
            pending = sorted((path / "requests").glob("*.json"))
            if pending:
                request = json.loads(pending[0].read_text())
                if not Path(request["prompt_file"]).is_file():
                    state["phase"] = "follow-up prompt missing; ready for another assignment"
                    state.update(last_turn_status="failed", error="queued follow-up prompt disappeared",
                                 failure_kind="rejected")
                    save(path / "state.json", state)
                    _write_turn_event(state)
                    pending[0].unlink()
                    print(f"Worker {job_id}: rejected missing prompt {clean(request['prompt_file'])}", flush=True)
                    continue
                policy = json.loads((path / "policy.json").read_text())
                effort = request.get("reasoning_effort") or policy["reasoning_effort"]
                steps = request.get("max_model_steps") or policy["max_model_steps"]
                isolation = dict((launch_config(state).get("isolation") or {}))
                if isolation.get("mode") == "create":
                    actual = state.get("actual_workspace", "")
                    if not actual or not Path(actual).is_dir():
                        state.update(phase="cannot identify isolated worktree; follow-up rejected",
                                     last_turn_status="failed", error="actual worktree is unknown",
                                     failure_kind="rejected")
                        save(path / "state.json", state)
                        _write_turn_event(state)
                        pending[0].unlink()
                        print(f"Worker {job_id}: follow-up rejected; actual worktree is unknown", flush=True)
                        continue
                    isolation = {"mode": "existing", "base": None, "existing_path": actual}
                    state["actual_workspace"] = actual
                engine = get_engine(state.get("engine", DEFAULT_ENGINE))
                new_turn = state["turn"] + 1
                effective = request["prompt_file"]
                source = None
                if state.get("report_contract"):
                    source = request["prompt_file"]
                    effective = str(path / f"prompt-{new_turn:04}.md")
                    (path / f"prompt-{new_turn:04}.md").write_text(
                        Path(source).read_text() + "\n\n" + REPORT_CONTRACT)
                try:
                    next_turn = turn_request(state, prompt_file=effective,
                                             reasoning_effort=effort, max_model_steps=steps,
                                             isolation=isolation)
                    engine.validate_turn(next_turn)
                    command = engine.build_command(next_turn)
                except ValueError as error:
                    state.update(last_turn_status="failed", error=str(error),
                                 phase="follow-up rejected; ready for another assignment",
                                 failure_kind="rejected")
                    save(path / "state.json", state)
                    _write_turn_event(state)
                    pending[0].unlink()
                    print(f"Worker {job_id}: follow-up rejected: {clean(error)}", flush=True)
                    continue
                state.update(prompt_file=effective, reasoning_effort=effort,
                             max_model_steps=steps, command=command,
                             label=request.get("label") or state["label"], turn=new_turn,
                             status="running", phase="starting related follow-up")
                if source:
                    state["source_prompt_file"] = source
                save(path / "state.json", state)
                pending[0].unlink()
                break
            if (path / "close.request").exists():
                state.update(status=state["last_turn_status"], phase="session closed", ended_at=time.time())
                release_worktree(state)
                save(path / "state.json", state)
                print(f"Worker {job_id} closed; durable session {worker_session_id(state)} retained", flush=True)
                return outcome_code(state)
            time.sleep(0.2)
        if control["stopping"]:
            state.update(status="cancelled", phase="cancelled", ended_at=time.time())
            release_worktree(state)
            save(path / "state.json", state)
            return 130


def create_job(args):
    workspace, prompt = Path(args.workspace), Path(args.prompt_file)
    if not workspace.is_absolute() or not workspace.is_dir():
        raise ValueError("--workspace must be an existing absolute directory")
    if not prompt.is_absolute() or not prompt.is_file() or not prompt.stat().st_size:
        raise ValueError("--prompt-file must be an existing nonempty absolute file")
    if args.max_model_steps is not None and args.max_model_steps < 1:
        raise ValueError("--max-model-steps must be positive")
    turn_timeout = parse_turn_timeout(getattr(args, "turn_timeout", None))
    fallback_raw = getattr(args, "fallback_models", None)
    if args.action == "open" and fallback_raw is not None:
        raise ValueError("--fallback-models is only supported for single-turn workers (run/start), not open")
    fallback_models = parse_fallback_models(fallback_raw)
    if args.action == "open" and args.no_session_log:
        raise ValueError("reusable workers require session logging to preserve follow-up history")
    if args.session_id and args.no_session_log:
        raise ValueError("--session-id requires retained session logging")

    role = getattr(args, "role", None)
    roles = resolved_roles()  # validates the whole claive config, even without --role
    role_def = roles.get(role) if role else None
    if role and role_def is None:
        raise ValueError(f"unknown role: {role}")
    engine_name = getattr(args, "engine", None) or (role_def["engine"] if role_def else None) or default_engine()
    engine = get_engine(engine_name)
    session_id = engine.resolve_session_id(args.session_id, not args.no_session_log)
    effort = args.reasoning_effort or (role_def["reasoning_effort"] if role_def else None) or engine.default_reasoning_effort
    if args.max_model_steps is not None:
        steps = args.max_model_steps
    elif role_def and role_def.get("max_model_steps") is not None and engine.default_max_model_steps is not None:
        steps = role_def["max_model_steps"]
    else:
        steps = engine.default_max_model_steps
    read_only = bool(args.read_only or (role_def and role_def.get("read_only")))
    config.check_workspace([workspace] + ([Path(args.worktree_existing)] if args.worktree_existing else []),
                           read_only)
    effective_model = args.model or (role_def["model"] if role_def else None)
    check_model_policy(effective_model)
    isolation = dict(mode="create" if args.worktree else "existing" if args.worktree_existing else "none",
                     base=args.worktree_base, existing_path=args.worktree_existing)
    launch = engine.resolve_launch(
        provider=args.provider, model=effective_model, read_only=read_only, web=args.web,
        output_schema=args.output_schema, session_logging=not args.no_session_log,
        isolation=isolation, session_id=session_id, session_root=str(root() / "sessions"),
        workspace=str(workspace), remember_model=bool(args.model and getattr(args, "remember_model", True)),
    )
    check_model_policy(launch.get("model"))
    engine.validate_launch(launch)
    initial_turn = TurnRequest(
        binary=launch["binary"], workspace=str(workspace), prompt_file=str(prompt),
        session_id=session_id, provider=launch["provider"], model=launch["model"],
        reasoning_effort=effort, max_model_steps=steps,
        read_only=launch["read_only"], web=launch["web"], output_schema=launch["output_schema"],
        session_logging=launch["session_logging"], isolation=launch["isolation"],
        session_dir=launch.get("session_dir"),
    )
    engine.validate_turn(initial_turn)
    mission_id = getattr(args, "mission", None) or os.environ.get("CLAIVE_MISSION") or None
    if mission_id:
        from claivelib import mission as mission_mod
        mission_mod.require_open(mission_id)
    job_id = uuid.uuid4().hex[:12]
    path = root() / job_id
    path.mkdir(mode=0o700)
    (path / "requests").mkdir(mode=0o700)
    try:
        launch = engine.prepare_isolation(launch, str(workspace), str(path))
    except ValueError:
        shutil.rmtree(path, ignore_errors=True)
        raise
    try:
        initial_turn = dataclasses.replace(initial_turn, isolation=launch["isolation"])
        save(path / "policy.json", dict(reasoning_effort=effort, max_model_steps=steps))
        report_contract = bool(getattr(args, "report", False) or role)
        effective_prompt, source_prompt = str(prompt), None
        if report_contract:
            source_prompt = str(prompt)
            preamble = role_def["preamble"] if role_def else None
            body = prompt.read_text()
            composed = f"{preamble}\n\n{body}\n\n{REPORT_CONTRACT}" if preamble else f"{body}\n\n{REPORT_CONTRACT}"
            (path / "prompt-0001.md").write_text(composed)
            effective_prompt = str(path / "prompt-0001.md")
            initial_turn = TurnRequest(
                binary=launch["binary"], workspace=str(workspace), prompt_file=effective_prompt,
                session_id=session_id, provider=launch["provider"], model=launch["model"],
                reasoning_effort=effort, max_model_steps=steps,
                read_only=launch["read_only"], web=launch["web"], output_schema=launch["output_schema"],
                session_logging=launch["session_logging"], isolation=launch["isolation"],
                session_dir=launch.get("session_dir"),
            )
        command = engine.build_command(initial_turn)
        state = dict(schema_version=SCHEMA_VERSION, engine=engine_name, session_id=session_id,
                     id=job_id, label=args.label or prompt.stem, workspace=str(workspace), log_dir=str(path),
                     prompt_file=effective_prompt, command=command, launch=launch, status="starting", phase="starting",
                     started_at=time.time(), steps=0, task_failures=0, malformed_events=0,
                     model=launch.get("model"), reasoning_effort=effort, max_model_steps=steps,
                     reusable=args.action == "open", turn=1,
                     fallback_models=fallback_models, fallbacks=[])
        if turn_timeout is not None:
            state["turn_timeout"] = turn_timeout
        if report_contract:
            state["report_contract"] = True
        if role:
            state["role"] = role
        if source_prompt:
            state["source_prompt_file"] = source_prompt
        if launch["isolation"].get("mode") == "existing":
            state["actual_workspace"] = launch["isolation"]["existing_path"]
        if mission_id:
            state["mission"] = mission_id
        save(path / "state.json", state)
        if mission_id:
            from claivelib import mission as mission_mod
            mission_mod.link_auto(mission_id, "worker", job_id)
    except BaseException:
        discard_job(path, launch)
        raise
    return path, state


def discard_job(path, launch):
    """Undo a job no supervisor ever ran: its claive-created worktree, branch and job dir."""
    isolation = launch.get("isolation") or {}
    if isolation.get("owner") == "claive":
        remove_worktree(isolation)
    shutil.rmtree(path, ignore_errors=True)


def spawn_supervisor(path, state, entry):
    """Start a detached supervisor (`_supervise` or `_reusable`); discard the job if it cannot start."""
    try:
        with (path / "supervisor.log").open("w") as log:
            subprocess.Popen([sys.executable, launcher_path(), entry, state["id"]],
                             stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    except BaseException:
        discard_job(path, launch_config(state))
        raise


def report(state):
    path = job_path(state["id"])
    print(f"{state['id']} {state['status']} | {clean(state['label'])}")
    session = worker_session_id(state)
    if session:
        print(f"Session: {session} | {state.get('engine', DEFAULT_ENGINE)} | {state.get('model')} | {state['reasoning_effort']}")
    if state.get("retained_worktree"):
        print(f"Changed worktree kept: {clean(state['retained_worktree'])}")
    if state.get("provider_base_url"):
        print(f"Provider override: {clean(state['provider_base_url'])} (claive config providers.opencode2api)")
    if state.get("error"):
        print(clean(state["error"]))
    if state.get("task_failures"):
        print(f"Task failures reported: {state['task_failures']} (inspect logs before accepting work)")
        for detail in state.get("task_failure_reasons", []):
            print(f"  - {clean(detail)}")
    if state.get("quota_exhausted"):
        print(f"Worker engine quota exhausted ({state.get('engine', DEFAULT_ENGINE)}). Reset: {state.get('quota_reset_at', 'not reported')}.")
        print("Pre-approved fallback: Pi muse-spark-1.3-contributor-free (opencode2api) for the affected lane; "
              "tell the user. Any other fallback needs their approval; this manager never switches automatically.")
    if state.get("failure_kind"):
        print(f"Failure kind: {state['failure_kind']}")
    for entry in state.get("fallbacks") or []:
        print(f"Fallback from {clean(entry.get('model', ''))}: {clean(entry.get('failure_kind', ''))} "
              f"({clean(entry.get('error', ''))})")
    report_state = state.get("report_state")
    if report_state == "missing":
        print("Report missing: no claive-report block in final answer")
    elif report_state == "invalid":
        print(f"Report invalid: {clean(state.get('report_error', ''))}")
    elif report_state == "ok":
        parsed = state.get("report") or {}
        print(f"Report: {clean(parsed.get('summary', ''))}")
        if parsed.get("changed_files"):
            print(f"Changed files: {clean(', '.join(parsed['changed_files']))}")
        for item in parsed.get("commands_run") or []:
            print(f"Ran: {clean(item.get('command', ''))} (exit {item.get('exit_code')})")
        if parsed.get("residual_risks"):
            print(f"Risks: {clean('; '.join(parsed['residual_risks']))}")
        if parsed.get("question"):
            print(f"Question: {clean(parsed['question'])}")
    needs = state.get("needs_parent")
    if needs:
        print(f"Needs parent ({needs.get('kind')}): {clean(needs.get('question', ''))}")
        if state.get("reusable"):
            print(f"Answer: claive answer {state['id']} --message ...")
        else:
            print("Answering needs a reusable worker (open --session-id ...); this was a single-turn run.")
    print(f"Logs: {path}")
    result = path / "result.txt"
    if result.exists():
        print(result.read_text())
    sys.stdout.flush()


def pi_models_needed():
    """Models claive may ask Pi for: Pi-engine roles and Pi's remembered or built-in default."""
    from claivelib.engines.pi import PiEngine
    settings = PiEngine._settings()
    remembered = settings.get("defaultModel") if settings.get("defaultProvider") == provider.PROVIDER else None
    needed = [remembered or provider.FALLBACK_MODEL]
    for role in resolved_roles().values():
        if role.get("engine") == "pi" and role.get("model") and role["model"] not in needed:
            needed.append(role["model"])
    return needed


def live_ping(timeout=120):
    """One real read-only Pi turn with no session, the way claive launches Pi; return (ok, detail)."""
    from claivelib.engines.pi import PiEngine, default_binary
    engine = PiEngine()
    settings = engine._settings()
    remembered = settings.get("defaultModel") if settings.get("defaultProvider") == provider.PROVIDER else None
    model = remembered or provider.FALLBACK_MODEL
    env = dict(os.environ)
    overlay = provider.overlay_agent_dir(root())
    if overlay:
        env["PI_CODING_AGENT_DIR"] = overlay
    with tempfile.TemporaryDirectory(prefix="claive-ping-") as directory:
        prompt = Path(directory) / "ping.md"
        prompt.write_text("Reply with the single word OK. Do not use tools.\n")
        request = TurnRequest(binary=default_binary(), workspace=directory, prompt_file=str(prompt),
                              session_id=None, provider=provider.PROVIDER, model=model,
                              reasoning_effort="minimal", max_model_steps=None, read_only=True, web=False,
                              output_schema=None, session_logging=False, isolation={})
        started = time.monotonic()
        try:
            completed = subprocess.run(engine.build_command(request), cwd=directory, env=env, text=True,
                                       capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            return False, f"{model}: {error}"[:200]
        seconds = time.monotonic() - started
    outcome = None
    for line in completed.stdout.splitlines():
        try:
            event = json.loads(line)
            for normalized in engine.normalize_event(event) if isinstance(event, dict) else []:
                if normalized["type"].startswith("terminal_"):
                    outcome = normalized
        except ValueError:
            continue
    if completed.returncode == 0 and outcome and outcome["terminal"] == "completed":
        return True, f"{model} answered in {seconds:.1f}s" + (" via override" if overlay else "")
    reason = (outcome or {}).get("reason") or completed.stderr.strip()[-160:] or f"exit {completed.returncode}"
    return False, f"{model}: {reason}"[:200]


def doctor_checks(live=False):
    checks = []

    def add(name, ok, required, detail):
        checks.append(dict(name=name, ok=bool(ok), required=bool(required), detail=str(detail)))

    add("python", sys.version_info >= (3, 10), True, sys.version.split()[0])
    try:
        base = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
        directory = Path(os.environ.get("CLAIVE_DIR", str(base / "claive")))
        if not directory.is_absolute():
            add("state_dir", False, True, f"{directory} is not absolute")
        else:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, temporary = tempfile.mkstemp(prefix=".doctor-", dir=directory)
            try:
                with os.fdopen(fd, "w") as stream:
                    stream.write("ok")
                Path(temporary).unlink()
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            add("state_dir", True, True, str(directory))
    except Exception as error:
        add("state_dir", False, True, str(error)[:200])
    try:
        resolved_roles()
        engine = default_engine()
        source = str(config.config_path()) if config.config_path().is_file() else "built-in defaults"
        allowed = config.load().get("workspaces")
        scope = ("any workspace" if allowed is None else
                 f"workspaces allowlist: {len(allowed)} entries, {sum(1 for e in allowed if e.get('write'))} writable")
        add("config", True, True, f"{source}; default engine {engine}; {scope}")
    except ValueError as error:
        add("config", False, True, str(error)[:200])
    try:
        default = default_engine()
    except ValueError:
        default = None
    results = {}
    for name in ("muse", "pi", "claude", "codex"):
        try:
            binary = importlib.import_module(f"claivelib.engines.{name}").default_binary()
        except ImportError:
            results[name] = False
            add(name, False, name == default, "engine module not installed")
            continue
        ok = Path(binary).is_file() and os.access(binary, os.X_OK)
        detail = binary if ok else f"missing or not executable: {binary}"
        if ok and name == "pi":
            # Proves the launcher and its Node runtime work, not only that the file exists.
            try:
                version = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=20,
                                         stdin=subprocess.DEVNULL)
                ok = version.returncode == 0
                detail = (f"{binary} ({version.stdout.strip()[:40]})" if ok else
                          f"{binary} --version exited {version.returncode}: {version.stderr.strip()[:120]}")
            except (OSError, subprocess.TimeoutExpired) as error:
                ok, detail = False, f"{binary} --version failed: {error}"[:200]
        results[name] = ok
        add(name, ok, name == default, detail)
    available = [name for name, ok in results.items() if ok]
    add("engines", bool(available), True, ", ".join(available) + (" ok" if len(available) == 1 else " available")
        if available else "no engine binary found")
    entry = None
    try:
        _data, entry = provider.read_models()
        listed = provider.listed_models(entry)
        missing = [model for model in pi_models_needed() if model not in listed]
        detail = f"{provider.models_file()}: {len(listed)} models"
        if missing:
            detail += f"; not listed: {', '.join(missing)}"
        add("pi_provider", not missing, default == "pi", detail)
    except ValueError as error:
        add("pi_provider", False, default == "pi", str(error)[:200])
    if entry is None:
        add("opencode2api", False, False, "provider not configured")
    else:
        try:
            override = provider.override_url()
            parts, used = [], None
            for label, url in (("models.json", entry["baseUrl"]), ("override", override)):
                if not url:
                    continue
                result = provider.probe(url, entry)
                used = result
                parts.append(f"{label} {url}: " + (f"{result['ms']} ms, {len(result['ids'])} models"
                                                   if result["ok"] else result["error"]))
            missing = [name for name in PREFERRED_MODELS if name not in used["ids"]] if used["ok"] else []
            if missing:
                parts.append(f"missing: {', '.join(missing)}")
            if override:
                parts.append("Pi turns use the override")
            add("opencode2api", used["ok"], False, "; ".join(parts))
        except Exception as error:
            add("opencode2api", False, False, str(error)[:200])
    if live:
        ok, detail = live_ping()
        add("live", ok, True, detail)
    git = shutil.which("git")
    add("git", bool(git), False, git or "not on PATH")
    try:
        registry = root()
        now = time.time()
        found = None
        for child in registry.iterdir():
            if not child.is_dir() or not re.fullmatch(r"[0-9a-f]{12}", child.name):
                continue
            state_file = child / "state.json"
            if not state_file.is_file():
                continue
            try:
                record = json.loads(state_file.read_text())
            except (OSError, ValueError):
                continue
            if not record.get("quota_exhausted"):
                continue
            reset = record.get("quota_reset_at")
            if not isinstance(reset, str):
                continue
            try:
                moment = datetime.datetime.fromisoformat(reset.replace("Z", "+00:00") if reset.endswith("Z") else reset)
                if moment.tzinfo is None:
                    moment = moment.replace(tzinfo=datetime.timezone.utc)
            except ValueError:
                continue
            if moment.timestamp() > now and (found is None or moment.timestamp() > found[2]):
                found = (record.get("engine", "unknown"), reset, moment.timestamp())
        if found:
            add("quota", False, False,
                f"{found[0]} quota until {found[1]}; pre-approved fallback: Pi muse-spark-1.3-contributor-free")
        else:
            add("quota", True, False, "no exhausted quota")
    except Exception as error:
        add("quota", True, False, f"registry unreadable: {error}"[:200])
    try:
        registry = root()
        lost = []
        for child in registry.iterdir():
            if not child.is_dir() or not re.fullmatch(r"[0-9a-f]{12}", child.name):
                continue
            try:
                record = load(child)
            except (OSError, ValueError, KeyError):
                continue
            if record.get("status") == "interrupted":
                lost.append(record.get("id", child.name))
        add("interrupted", not lost, False,
            "no interrupted workers" if not lost else f"interrupted: {', '.join(sorted(lost))}")
    except Exception as error:
        add("interrupted", True, False, f"registry unreadable: {error}"[:200])
    return checks


def doctor(json_output=False, live=False):
    checks = doctor_checks(live)
    ok = all(item["ok"] for item in checks if item["required"])
    if json_output:
        print(json.dumps({"ok": ok, "checks": checks}, indent=2))
    else:
        for item in checks:
            label = "ok" if item["ok"] else "FAIL" if item["required"] else "warn"
            print(f"{label} {item['name']} {clean(item['detail'])}")
    return 0 if ok else 1


def tmux_view(arguments):
    if not shutil.which("tmux") or not shutil.which("codex"):
        raise ValueError("tmux and codex must be installed")
    session = "claive-" + uuid.uuid4().hex[:8]
    codex_args = arguments[1:] if arguments[:1] == ["--"] else arguments
    codex_command = "exec " + shlex.join(["env", f"CLAIVE_DIR={root()}",
                                         shutil.which("codex"), *codex_args])
    dashboard_command = "exec " + shlex.join([launcher_path(), "watch", "--compact"])
    environment = {"CLAIVE_DIR": str(root())}
    try:
        created = subprocess.run(["tmux", "new-session", "-d", "-P", "-F", "#{pane_id}",
                                  "-s", session, "-c", os.getcwd(), codex_command],
                                 check=True, capture_output=True, text=True)
        pane = created.stdout.strip()
        for key, value in environment.items():
            subprocess.run(["tmux", "set-environment", "-t", session, key, value], check=True)
        subprocess.run(["tmux", "set-option", "-t", session, "status", "on"], check=True)
        subprocess.run(["tmux", "set-option", "-t", session, "status-left", "Codex "], check=True)
        subprocess.run(["tmux", "set-option", "-t", session, "status-right-length", "90"], check=True)
        subprocess.run(["tmux", "set-option", "-t", session, "status-interval", "1"], check=True)
        status_command = shlex.join(["env", f"CLAIVE_DIR={root()}", launcher_path(), "status-line"])
        subprocess.run(["tmux", "set-option", "-t", session, "status-right",
                        "#[fg=cyan]#(" + status_command + ")#[default]"], check=True)
        dashboard = subprocess.run(["tmux", "split-window", "-v", "-l", "5", "-P", "-F",
                                    "#{pane_id}", "-t", pane, "-c", os.getcwd(), dashboard_command],
                                   check=True, capture_output=True, text=True).stdout.strip()
        for hook in ("client-attached", "client-resized", "after-resize-window"):
            subprocess.run(["tmux", "set-hook", "-t", session, hook + "[90]",
                            shlex.join(["resize-pane", "-t", dashboard, "-y", "5"])], check=True)
        subprocess.run(["tmux", "select-pane", "-t", pane], check=True)
    except subprocess.CalledProcessError:
        subprocess.run(["tmux", "kill-session", "-t", session], stderr=subprocess.DEVNULL)
        raise
    command = "switch-client" if os.environ.get("TMUX") else "attach-session"
    print(f"Worker view: {session}", flush=True)
    return subprocess.call(["tmux", command, "-t", session])


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="action", required=True)
    for action in ("run", "start", "open"):
        descriptions = {"run": "run one tracked worker turn", "start": "detach one tracked worker turn",
                        "open": "keep a worker available for related follow-up turns"}
        launch = commands.add_parser(action, help=descriptions[action])
        launch.add_argument("--workspace", required=True)
        launch.add_argument("--prompt-file", required=True)
        launch.add_argument("--label")
        launch.add_argument("--engine", default=None)
        launch.add_argument("--role", choices=sorted(ROLES))
        launch.add_argument("--report", action="store_true", help="request a structured claive-report block")
        launch.add_argument("--reasoning-effort", help="engine-specific effort; defaults to xhigh (Muse), max (Pi), max (Claude) or max (Codex)")
        launch.add_argument("--max-model-steps", type=int, help="engine step cap, when supported")
        launch.add_argument("--read-only", action="store_true")
        isolation = launch.add_mutually_exclusive_group()
        isolation.add_argument("--worktree", action="store_true")
        isolation.add_argument("--worktree-existing", help="resume a lane in its existing absolute worktree path")
        launch.add_argument("--worktree-base")
        launch.add_argument("--web", action="store_true", help="enable engine web tools")
        launch.add_argument("--model")
        launch.add_argument("--session-id", help="reuse this engine's session ID with the same workspace and policy")
        launch.add_argument("--output-schema")
        launch.add_argument("--no-session-log", action="store_true")
        launch.add_argument("--provider")
        launch.add_argument("--mission", help="link the new worker to a mission")
        launch.add_argument("--turn-timeout",
                            help="fail a turn that runs longer than SECONDS (positive number)")
        launch.add_argument("--fallback-models",
                            help="comma-separated fallback models for single-turn workers (run/start only)")
        if action == "open":
            launch.add_argument("--detach", action="store_true", help="run the reusable loop in the background")
    listing = commands.add_parser("list", help="list active workers and recent results")
    listing.add_argument("--json", action="store_true")
    for action in ("show", "logs", "cancel", "close", "usage"):
        command = commands.add_parser(action)
        command.add_argument("id")
        if action in {"show", "usage"}:
            command.add_argument("--json", action="store_true")
        if action == "logs":
            command.add_argument("--stderr", action="store_true")
            command.add_argument("--lines", type=int, default=20)
    waiting = commands.add_parser("wait")
    waiting.add_argument("id", nargs="+")
    waiting.add_argument("--any", action="store_true")
    waiting.add_argument("--timeout", type=float)
    inbox_cmd = commands.add_parser("inbox", help="show turn and batch events since the consumer cursor")
    inbox_cmd.add_argument("--consumer", default="default")
    inbox_cmd.add_argument("--peek", action="store_true")
    inbox_cmd.add_argument("--json", action="store_true")
    batch_cmd = commands.add_parser("batch", help="run multi-worker plans")
    batch_sub = batch_cmd.add_subparsers(dest="batch_action", required=True)
    batch_validate = batch_sub.add_parser("validate")
    batch_validate.add_argument("plan")
    batch_start = batch_sub.add_parser("start")
    batch_start.add_argument("plan")
    batch_start.add_argument("--json", action="store_true")
    batch_status = batch_sub.add_parser("status")
    batch_status.add_argument("id")
    batch_status.add_argument("--json", action="store_true")
    batch_wait = batch_sub.add_parser("wait")
    batch_wait.add_argument("id")
    batch_wait.add_argument("--timeout", type=float)
    batch_cancel = batch_sub.add_parser("cancel")
    batch_cancel.add_argument("id")
    batch_retry = batch_sub.add_parser("retry")
    batch_retry.add_argument("id")
    batch_list = batch_sub.add_parser("list")
    batch_list.add_argument("--json", action="store_true")
    mission_cmd = commands.add_parser("mission", help="track goals across workers and batches")
    mission_sub = mission_cmd.add_subparsers(dest="mission_action", required=True)
    mission_new = mission_sub.add_parser("new")
    mission_new.add_argument("--title", required=True)
    mission_goal = mission_new.add_mutually_exclusive_group(required=True)
    mission_goal.add_argument("--goal")
    mission_goal.add_argument("--goal-file")
    mission_note = mission_sub.add_parser("note")
    mission_note.add_argument("id")
    mission_note.add_argument("text")
    mission_link = mission_sub.add_parser("link")
    mission_link.add_argument("id")
    mission_target = mission_link.add_mutually_exclusive_group(required=True)
    mission_target.add_argument("--worker")
    mission_target.add_argument("--batch")
    mission_target.add_argument("--run")
    mission_show = mission_sub.add_parser("show")
    mission_show.add_argument("id")
    mission_show.add_argument("--json", action="store_true")
    mission_list = mission_sub.add_parser("list")
    mission_list.add_argument("--all", dest="all_missions", action="store_true")
    mission_list.add_argument("--json", action="store_true")
    mission_close = mission_sub.add_parser("close")
    mission_close.add_argument("id")
    goal_cmd = commands.add_parser("goal", help="durable goal queue worked by claive serve")
    goal_sub = goal_cmd.add_subparsers(dest="goal_action", required=True)
    goal_add = goal_sub.add_parser("add", help="queue a goal; read-only unless --write")
    goal_add.add_argument("--title", required=True)
    goal_add.add_argument("--prompt-file", required=True)
    goal_add.add_argument("--workspace", required=True)
    goal_add.add_argument("--role", choices=sorted(ROLES))
    goal_add.add_argument("--engine")
    goal_add.add_argument("--model")
    goal_add.add_argument("--write", action="store_true", help="let the worker edit the workspace")
    goal_add.add_argument("--budget", help="wall-clock budget: seconds, or 30m / 2h (default 2h)")
    goal_add.add_argument("--max-attempts", type=int, help="attempts before the goal fails (default 3)")
    goal_list = goal_sub.add_parser("list")
    goal_list.add_argument("--all", action="store_true", dest="all_goals", help="include finished goals")
    goal_list.add_argument("--json", action="store_true")
    goal_show = goal_sub.add_parser("show")
    goal_show.add_argument("id")
    goal_show.add_argument("--json", action="store_true")
    goal_answer = goal_sub.add_parser("answer", help="answer a parked goal's question")
    goal_answer.add_argument("id")
    goal_answer.add_argument("--message", required=True)
    for name in ("cancel", "retry"):
        goal_sub.add_parser(name).add_argument("id")
    serve_cmd = commands.add_parser("serve", help="work the goal queue in the foreground (for systemd)")
    serve_mode = serve_cmd.add_mutually_exclusive_group()
    serve_mode.add_argument("--stop", action="store_true", help="set the stop switch and return")
    serve_mode.add_argument("--resume", action="store_true", help="clear the stop switch and return")
    serve_mode.add_argument("--status", action="store_true", help="print serve, stop switch and queue state")
    serve_mode.add_argument("--once", action="store_true", help="run one pass and exit")
    serve_cmd.add_argument("--interval", type=float, default=5.0, help="seconds between passes (default 5)")
    serve_cmd.add_argument("--max-parallel", type=int, default=1, help="goals run at once (default 1)")
    followup = commands.add_parser("followup", help="submit related work to an existing reusable worker")
    followup.add_argument("id")
    followup.add_argument("--prompt-file", required=True)
    followup.add_argument("--label")
    followup.add_argument("--reasoning-effort", help="override this turn; otherwise use worker policy")
    followup.add_argument("--max-model-steps", type=int, help="override this turn's step cap")
    effort = commands.add_parser("effort", help="change a reusable worker's default effort for future turns")
    effort.add_argument("id")
    effort.add_argument("--reasoning-effort", required=True)
    effort.add_argument("--max-model-steps", type=int)
    watch = commands.add_parser("watch", help="live dashboard; Ctrl-C stops the view, not workers")
    watch.add_argument("--interval", type=float, default=1.0)
    watch.add_argument("--once", action="store_true")
    watch.add_argument("--no-color", action="store_true")
    watch.add_argument("--compact", action="store_true", help="at most five lines; one row per worker")
    commands.add_parser("status-line", help="one-line summary for tmux or shell status bars")
    examined = commands.add_parser("doctor", help="read-only health check; launches a model only with --live")
    examined.add_argument("--json", action="store_true")
    examined.add_argument("--live", action="store_true",
                          help="also send one tiny read-only Pi turn (no session) and time it")
    answer = commands.add_parser("answer", help="answer a reusable worker waiting for a parent decision")
    answer.add_argument("id")
    message = answer.add_mutually_exclusive_group(required=True)
    message.add_argument("--message")
    message.add_argument("--message-file")
    tmux = commands.add_parser("tmux", help="open Codex with a live worker pane and status bar")
    tmux.add_argument("codex_args", nargs=argparse.REMAINDER)
    return result


def main(launcher=None):
    if launcher is not None:
        set_launcher_path(launcher)
    if len(sys.argv) == 3 and sys.argv[1] == "_supervise":
        return run_supervisor(sys.argv[2], reusable=False)
    if len(sys.argv) == 3 and sys.argv[1] == "_reusable":
        return run_supervisor(sys.argv[2], reusable=True)
    if len(sys.argv) == 3 and sys.argv[1] == "_batch":
        from claivelib import batch as batch_mod
        return batch_mod.runner(sys.argv[2])
    args = parser().parse_args()
    if args.action in {"run", "start", "open"}:
        check_nested()
        path, state = create_job(args)
        print(f"Worker {state['id']} | {clean(state['label'])} | {path}", flush=True)
        if args.action == "open" and getattr(args, "detach", False):
            spawn_supervisor(path, state, "_reusable")
            print(f"Wait: claive wait {state['id']}\nFollow-up: claive followup {state['id']} --prompt-file ...\n"
                  f"Answer: claive answer {state['id']} --message ...\nClose: claive close {state['id']}")
            return 0
        if args.action in {"run", "open"}:
            code = run_supervisor(state["id"], reusable=args.action == "open")
            report(load(path))
            return code
        spawn_supervisor(path, state, "_supervise")
        print(f"Inspect: claive show {state['id']}\nCancel:  claive cancel {state['id']}")
        return 0
    if args.action == "list":
        records = jobs()
        print(json.dumps(records, indent=2) if args.json else render(records, sys.stdout.isatty()))
    elif args.action == "status-line":
        print(summary(jobs()))
    elif args.action == "watch":
        if args.interval < 0.2:
            raise ValueError("--interval must be at least 0.2 seconds")
        while True:
            if sys.stdout.isatty() and not args.once:
                print("\033[H\033[2J", end="")
            tty = sys.stdout.isatty()
            size = terminal_size()
            color = tty and not args.no_color
            if args.compact:
                view = compact_view(jobs(), color, size.columns, min(5, size.lines))
            else:
                view = render(jobs(), color, size.lines if tty else None)
            # A newline after the last pane row would scroll the summary away.
            print(view, end="" if tty and not args.once else "\n", flush=True)
            if args.once:
                break
            time.sleep(args.interval)
    elif args.action == "show":
        state = load(job_path(args.id))
        if state.get("reusable"):
            state["next_turn_defaults"] = json.loads((job_path(args.id) / "policy.json").read_text())
        print(json.dumps(state, indent=2)) if args.json else report(state)
    elif args.action == "usage":
        state = load(job_path(args.id))
        usage = get_engine(state.get("engine", DEFAULT_ENGINE)).session_usage(state)
        if args.json:
            print(json.dumps(usage, indent=2))
        elif not usage["model_calls"]:
            print("No provider token usage recorded; cache hits are unknown.")
        else:
            total = usage["totals"]
            print(f"Session usage: {usage['model_calls']} model calls | "
                  f"{total.get('input_tokens', 'unknown')} input | "
                  f"{total.get('cached_tokens', 'unknown')} cached | "
                  f"{total.get('output_tokens', 'unknown')} output")
            if usage["cache_hit_ratio"] is not None:
                print(f"Reported input served from cache: {usage['cache_hit_ratio']:.1%}")
            print("Provider counters across retained session; not context size or an invoice.")
    elif args.action == "logs":
        if args.lines < 1:
            raise ValueError("--lines must be positive")
        from collections import deque
        log = job_path(args.id) / ("stderr.log" if args.stderr else "events.jsonl")
        if not log.exists():
            print("No log output yet.")
        else:
            with log.open(errors="replace") as stream:
                print("".join(deque(stream, maxlen=args.lines)), end="")
    elif args.action == "wait":
        ids = args.id
        if len(ids) > 1 and not args.any:
            raise ValueError("use --any to wait for several workers")
        if args.timeout is not None and args.timeout <= 0:
            raise ValueError("--timeout must be > 0")
        paths = [job_path(job_id) for job_id in ids]
        start = time.monotonic()
        while True:
            for job_id, path in zip(ids, paths):
                state = load(path)
                if state["status"] not in ACTIVE or (state["status"] == "idle" and not list((path / "requests").glob("*.json"))):
                    report(state)
                    return outcome_code(state)
            if args.timeout is not None and time.monotonic() - start >= args.timeout:
                print(f"Timed out after {'%g' % args.timeout}s; still running: {' '.join(ids)}")
                return 124
            time.sleep(0.25)
    elif args.action == "followup":
        path = job_path(args.id)
        state = load(path)
        if not state.get("reusable") or state["status"] not in ACTIVE or (path / "close.request").exists():
            raise ValueError("worker is not available for follow-up; reopen its retained --session-id if appropriate")
        prompt = Path(args.prompt_file)
        if not prompt.is_absolute() or not prompt.is_file() or not prompt.stat().st_size:
            raise ValueError("--prompt-file must be an existing nonempty absolute file")
        if args.max_model_steps is not None and args.max_model_steps < 1:
            raise ValueError("--max-model-steps must be positive")
        policy = json.loads((path / "policy.json").read_text())
        get_engine(state.get("engine", DEFAULT_ENGINE)).validate_turn(turn_request(
            state, prompt_file=str(prompt),
            reasoning_effort=args.reasoning_effort or policy["reasoning_effort"],
            max_model_steps=args.max_model_steps if args.max_model_steps is not None else policy["max_model_steps"],
        ))
        request = f"{time.time_ns():020}-{uuid.uuid4().hex}.json"
        save(path / "requests" / request, dict(prompt_file=str(prompt), label=args.label,
                                               reasoning_effort=args.reasoning_effort,
                                               max_model_steps=args.max_model_steps))
        print(f"Follow-up queued for {args.id} in session {worker_session_id(state)}")
    elif args.action == "effort":
        path = job_path(args.id)
        state = load(path)
        if not state.get("reusable") or state["status"] not in ACTIVE or (path / "close.request").exists():
            raise ValueError("worker is not available for policy changes")
        if args.max_model_steps is not None and args.max_model_steps < 1:
            raise ValueError("--max-model-steps must be positive")
        policy = json.loads((path / "policy.json").read_text())
        get_engine(state.get("engine", DEFAULT_ENGINE)).validate_turn(turn_request(
            state, reasoning_effort=args.reasoning_effort,
            max_model_steps=args.max_model_steps if args.max_model_steps is not None else policy["max_model_steps"],
        ))
        policy["reasoning_effort"] = args.reasoning_effort
        if args.max_model_steps is not None:
            policy["max_model_steps"] = args.max_model_steps
        save(path / "policy.json", policy)
        print(f"Worker {args.id}: default effort {args.reasoning_effort} for future turns; current request unchanged.")
    elif args.action == "close":
        path = job_path(args.id)
        state = load(path)
        if not state.get("reusable") or state["status"] not in ACTIVE:
            print(f"Worker is already {state['status']} or is not reusable.")
            return 0
        save(path / "close.request", {"requested_at": time.time()})
        print(f"Worker {args.id} will close after its assigned turns; history is retained.")
    elif args.action == "cancel":
        path = job_path(args.id)
        state = load(path)
        if state["status"] == "interrupted":
            worker = state.get("worker_pid")
            if worker and identity(worker) == state.get("worker_identity"):
                os.killpg(worker, signal.SIGKILL)
                state.update(status="cancelled", ended_at=time.time(), phase="cancelled",
                             error="orphaned worker stopped after supervisor interruption")
                save(path / "state.json", state)
                print(f"Orphaned worker {args.id} stopped.")
                return 0
            raise ValueError("worker interrupted; no matching live process to cancel")
        if state["status"] not in ACTIVE:
            print(f"Worker is already {state['status']}.")
            return 0
        pid = state.get("supervisor_pid")
        if not pid:
            for _attempt in range(20):
                time.sleep(0.1)
                state = load(path)
                pid = state.get("supervisor_pid")
                if pid or state["status"] not in ACTIVE:
                    break
        if not pid or identity(pid) != state.get("supervisor_identity"):
            raise ValueError("no matching live supervisor; no signal sent")
        os.kill(pid, signal.SIGTERM)
        print(f"Cancellation requested for {args.id}. Check with claive wait {args.id}.")
    elif args.action == "doctor":
        return doctor(args.json, args.live)
    elif args.action == "answer":
        path = job_path(args.id)
        state = load(path)
        if (not state.get("reusable") or state["status"] not in ACTIVE
                or (path / "close.request").exists() or not state.get("needs_parent")):
            raise ValueError("worker is not waiting for a parent decision")
        if args.message is not None:
            decision = args.message
        else:
            prompt = Path(args.message_file)
            if not prompt.is_absolute() or not prompt.is_file() or not prompt.stat().st_size:
                raise ValueError("--message-file must be an existing nonempty absolute file")
            decision = prompt.read_text()
        question = state.get("needs_parent", {}).get("question", "")
        answer_file = path / f"answer-{time.time_ns()}.md"
        answer_file.write_text(f"Parent question was:\n{question}\n\nParent decision:\n{decision}\n\n"
                               "Continue the assignment with this decision.\n")
        policy = json.loads((path / "policy.json").read_text())
        get_engine(state.get("engine", DEFAULT_ENGINE)).validate_turn(turn_request(
            state, prompt_file=str(answer_file),
            reasoning_effort=policy["reasoning_effort"], max_model_steps=policy["max_model_steps"],
        ))
        request = f"{time.time_ns():020}-{uuid.uuid4().hex}.json"
        save(path / "requests" / request, dict(prompt_file=str(answer_file), label=None,
                                               reasoning_effort=None, max_model_steps=None))
        print(f"Answer queued for {args.id}")
    elif args.action == "tmux":
        return tmux_view(args.codex_args)
    elif args.action == "inbox":
        from claivelib import inbox as inbox_mod
        events = inbox_mod.read_new(args.consumer, peek=args.peek)
        print(json.dumps(events, indent=2) if args.json else inbox_mod.format_text(events))
    elif args.action == "batch":
        from claivelib import batch as batch_mod
        if args.batch_action == "validate":
            return batch_mod.cmd_validate(args.plan)
        if args.batch_action == "start":
            return batch_mod.cmd_start(args.plan, json_output=args.json)
        if args.batch_action == "status":
            return batch_mod.cmd_status(args.id, json_output=args.json)
        if args.batch_action == "wait":
            return batch_mod.cmd_wait(args.id, timeout=args.timeout)
        if args.batch_action == "cancel":
            return batch_mod.cmd_cancel(args.id)
        if args.batch_action == "retry":
            return batch_mod.cmd_retry(args.id)
        if args.batch_action == "list":
            return batch_mod.cmd_list(json_output=args.json)
    elif args.action == "goal":
        from claivelib import serve as serve_mod
        if args.goal_action == "add":
            return serve_mod.cmd_add(args)
        if args.goal_action == "list":
            return serve_mod.cmd_list(args.json, args.all_goals)
        if args.goal_action == "show":
            return serve_mod.cmd_show(args.id, args.json)
        if args.goal_action == "answer":
            return serve_mod.cmd_answer(args.id, args.message)
        if args.goal_action == "cancel":
            return serve_mod.cmd_cancel(args.id)
        if args.goal_action == "retry":
            return serve_mod.cmd_retry(args.id)
    elif args.action == "serve":
        from claivelib import serve as serve_mod
        return serve_mod.cmd_serve(args)
    elif args.action == "mission":
        from claivelib import mission as mission_mod
        if args.mission_action == "new":
            return mission_mod.cmd_new(args.title, goal=args.goal, goal_file=args.goal_file)
        if args.mission_action == "note":
            return mission_mod.cmd_note(args.id, args.text)
        if args.mission_action == "link":
            return mission_mod.cmd_link(args.id, worker=args.worker, batch=args.batch, run=args.run)
        if args.mission_action == "show":
            return mission_mod.cmd_show(args.id, json_output=args.json)
        if args.mission_action == "list":
            return mission_mod.cmd_list(all_missions=args.all_missions, json_output=args.json)
        if args.mission_action == "close":
            return mission_mod.cmd_close(args.id)
    return 0


def entrypoint(launcher):
    os.umask(0o077)
    try:
        return main(launcher)
    except KeyboardInterrupt:
        return 130
    except (ValueError, OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        print(f"claive: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(entrypoint(Path(sys.argv[0]).resolve()))
