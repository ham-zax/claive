#!/usr/bin/python3
"""Track Muse workers and display their progress beside Codex. No dependencies."""

import argparse
import datetime
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


ACTIVE = {"starting", "running", "cancelling", "idle"}
MODEL = "muse-spark-1.3-contributor"
EFFORTS = ["medium", "high", "xhigh", "max"]
SCRIPT = None
MUSE = os.environ.get("MUSE_WORKER_BINARY", str(Path.home() / ".local/bin/muse"))


def set_launcher_path(path):
    global SCRIPT
    SCRIPT = str(Path(path).resolve())


def launcher_path():
    if not SCRIPT:
        raise ValueError("codex-workers launcher path is not configured")
    return SCRIPT


def root():
    base = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
    path = Path(os.environ.get("CODEX_WORKERS_DIR", str(base / "codex-workers")))
    if not path.is_absolute():
        raise ValueError("CODEX_WORKERS_DIR must be absolute")
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
        elif not pid and time.time() - state["started_at"] > 5:
            state["status"] = "interrupted"
            state["error"] = "worker supervisor did not start"
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
    idle_text = f" | {idle} idle" if idle else ""
    return f"Muse: {running} running{idle_text} | {done} done | {failed} failed | {stopped} stopped"


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
        badge = badges.get(status, status.upper())
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
        lines.append(fit(hint + "codex-workers: show ID | logs ID | cancel ID | list"))
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
        badge = status.upper().ljust(12)
        if color:
            badge = f"\033[{palette.get(status, '0')}m{badge}\033[0m"
        label = clean(state["label"])
        if state.get("task_failures"):
            label += f" [!{state['task_failures']} task failures]"
        workspace = clean(Path(state["workspace"]).name)
        prefix = f"{status.upper():12} {state['id']}  {age:8}  {state.get('steps', 0):5}  "
        detail = f"{label} / {workspace}"[:max(8, width - len(prefix))]
        lines.append(f"{badge} {state['id']}  {age:8}  {state.get('steps', 0):5}  {detail}")
        if status in ACTIVE:
            idle = duration(time.time() - state.get("last_activity", state["started_at"]))
            lines.append(f"  {clean(state.get('phase', 'starting'))} | last event {idle} ago"[:width])
    if not records:
        lines.append("No tracked workers yet. Muse jobs launched with muse-worker appear here.")
    lines.extend([f"{hidden} more: codex-workers list" if hidden else "Active + 20 recent results; done = CLI completed, verify the work.",
                  "codex-workers: show ID | logs ID | cancel ID | wait ID"])
    return "\n".join(lines)


def event_update(state, event):
    payload = event.get("payload", {})
    if not isinstance(payload, dict):
        return
    kind = event.get("payload_type", "")
    if not isinstance(kind, str):
        return
    state["last_event"] = kind
    state["last_activity"] = time.time()
    detail = payload.get("event", {})
    if kind == "task.lifecycle.proposed" and isinstance(detail, dict):
        task_kind = str(detail.get("task_kind", ""))
        if task_kind.startswith("model."):
            state["steps"] += 1
            state["phase"] = "model working"
        elif "tool" in task_kind or "shell" in task_kind:
            state["phase"] = "tool: " + task_kind
    elif kind == "run.output.delta":
        state["phase"] = "producing answer"
    elif kind == "task.lifecycle.failed":
        state["task_failures"] += 1
        state["phase"] = "task failure reported"
        if isinstance(detail, dict):
            quota_update(state, detail.get("reason", ""))
    elif kind.startswith("run.terminal."):
        state["terminal"] = payload.get("terminal")
        state["terminal_reason"] = payload.get("reason")
        state["answer"] = payload.get("text", "")
        state["phase"] = "finishing"
        quota_update(state, payload.get("reason", ""))


def quota_update(state, reason):
    """A generic 429 can be transient; only identify explicit subscription exhaustion."""
    if not isinstance(reason, str) or "subscription quota exhausted" not in reason.lower():
        return
    state["quota_exhausted"] = True
    state["fallback_requires_user_approval"] = True
    reset = re.search(r"resets at\s+(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2}))", reason)
    if reset:
        state["quota_reset_at"] = reset.group(1)


def supervise(job_id, control=None):
    path = job_path(job_id)
    state = load(path, check_alive=False)
    for key in ("ended_at", "error", "terminal", "terminal_reason", "quota_exhausted",
                "quota_reset_at", "fallback_requires_user_approval"):
        state.pop(key, None)
    state["malformed_events"] = 0
    (path / "result.txt").unlink(missing_ok=True)
    state.update(supervisor_pid=os.getpid(), supervisor_identity=identity(os.getpid()),
                 status="running", phase="launching Muse")
    stopping = False
    child = None

    def stop_requested(_signum, _frame):
        nonlocal stopping
        stopping = True
        if control is not None:
            control["stopping"] = True

    signal.signal(signal.SIGTERM, stop_requested)
    signal.signal(signal.SIGINT, stop_requested)
    save(path / "state.json", state)
    try:
        log_mode = "a" if state.get("reusable") else "w"
        with (path / "events.jsonl").open(log_mode) as events, (path / "stderr.log").open(log_mode) as errors:
            child = subprocess.Popen(state["command"], stdout=subprocess.PIPE, stderr=errors,
                                     stdin=subprocess.DEVNULL, start_new_session=True,
                                     cwd=state["workspace"])
            state["worker_pid"] = child.pid
            state["worker_identity"] = identity(child.pid)
            state["phase"] = "waiting for first event"
            save(path / "state.json", state)
            pending = b""
            cancelled_at = None
            last_save = 0
            with selectors.DefaultSelector() as selector:
                selector.register(child.stdout, selectors.EVENT_READ)
                while selector.get_map() or child.poll() is None:
                    if stopping and cancelled_at is None:
                        state["status"] = "cancelling"
                        state["phase"] = "stopping worker"
                        save(path / "state.json", state)
                        cancelled_at = time.monotonic()
                        try:
                            os.killpg(child.pid, signal.SIGTERM)
                        except ProcessLookupError:
                            pass
                    if cancelled_at is not None and time.monotonic() - cancelled_at > 3:
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
                                    event_update(state, event)
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
                    event_update(state, event)
                except (ValueError, UnicodeDecodeError):
                    state["malformed_events"] += 1
            state["exit_code"] = child.wait()
            child.stdout.close()
        if "-w" in state["command"] and state["command"][state["command"].index("-w") + 1] == "create":
            matches = re.findall(r"^muse: workspace root: (.+)$", (path / "stderr.log").read_text(), re.M)
            actual = matches[-1].rsplit(" (", 1)[0] if matches else ""
            if actual and Path(actual).is_dir():
                state["actual_workspace"] = actual
        answer = state.pop("answer", "")
        (path / "result.txt").write_text(answer if isinstance(answer, str) else json.dumps(answer))
        if state.get("reusable"):
            (path / f"turn-{state['turn']:04}.txt").write_text(
                answer if isinstance(answer, str) else json.dumps(answer))
        if stopping:
            state["status"] = "cancelled"
        elif state["exit_code"] != 0 or state.get("terminal") != "completed" or state["malformed_events"]:
            state["status"] = "failed"
            state["error"] = (f"exit={state['exit_code']}; terminal={state.get('terminal', 'missing')}; "
                              f"malformed events={state['malformed_events']}")
        else:
            state["status"] = "completed"
    except Exception as error:
        state["status"] = "cancelled" if stopping else "failed"
        state["error"] = str(error)
        if child is not None:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait()
    state.pop("answer", None)
    state["ended_at"] = time.time()
    state["phase"] = state["status"]
    save(path / "state.json", state)
    return outcome_code(state)


def outcome_code(state):
    if state["status"] == "idle":
        state = dict(state, status=state.get("last_turn_status", "failed"))
    if state["status"] == "completed":
        return 0
    if state["status"] == "cancelled":
        return 130
    code = state.get("exit_code")
    return min(code, 255) if isinstance(code, int) and code > 0 else 1


def reusable_worker(job_id):
    """Keep one supervisor and durable Muse session available between turns."""
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
              f"idle in Muse session {state['muse_session_id']}", flush=True)
        report(state)
        while not control["stopping"]:
            pending = sorted((path / "requests").glob("*.json"))
            if pending:
                request = json.loads(pending[0].read_text())
                if not Path(request["prompt_file"]).is_file():
                    state["phase"] = "follow-up prompt missing; ready for another assignment"
                    state.update(last_turn_status="failed", error="queued follow-up prompt disappeared")
                    save(path / "state.json", state)
                    pending[0].unlink()
                    print(f"Worker {job_id}: rejected missing prompt {clean(request['prompt_file'])}", flush=True)
                    continue
                policy = json.loads((path / "policy.json").read_text())
                effort = request.get("reasoning_effort") or policy["reasoning_effort"]
                steps = request.get("max_model_steps") or policy["max_model_steps"]
                command = state["command"]
                command[command.index("--prompt-file") + 1] = request["prompt_file"]
                if "--reasoning-effort" in command:
                    command[command.index("--reasoning-effort") + 1] = effort
                command[command.index("--max-model-steps") + 1] = str(steps)
                if "-w" in command and command[command.index("-w") + 1] == "create":
                    actual = state.get("actual_workspace", "")
                    if not actual or not Path(actual).is_dir():
                        state.update(phase="cannot identify isolated worktree; follow-up rejected",
                                     last_turn_status="failed", error="actual worktree is unknown")
                        save(path / "state.json", state)
                        pending[0].unlink()
                        print(f"Worker {job_id}: follow-up rejected; actual worktree is unknown", flush=True)
                        continue
                    command[command.index("-w") + 1] = "existing"
                    command += ["--worktree-existing", actual]
                    if "--worktree-base" in command:
                        index = command.index("--worktree-base")
                        del command[index:index + 2]
                    state["actual_workspace"] = actual
                state.update(prompt_file=request["prompt_file"], reasoning_effort=effort,
                             label=request.get("label") or state["label"], turn=state["turn"] + 1,
                             status="running", phase="starting related follow-up")
                save(path / "state.json", state)
                pending[0].unlink()
                break
            if (path / "close.request").exists():
                state.update(status=state["last_turn_status"], phase="session closed", ended_at=time.time())
                save(path / "state.json", state)
                print(f"Worker {job_id} closed; durable Muse session {state['muse_session_id']} retained", flush=True)
                return outcome_code(state)
            time.sleep(0.2)
        if control["stopping"]:
            state.update(status="cancelled", phase="cancelled", ended_at=time.time())
            save(path / "state.json", state)
            return 130


def create_job(args):
    workspace, prompt = Path(args.workspace), Path(args.prompt_file)
    if not workspace.is_absolute() or not workspace.is_dir():
        raise ValueError("--workspace must be an existing absolute directory")
    if not prompt.is_absolute() or not prompt.is_file() or not prompt.stat().st_size:
        raise ValueError("--prompt-file must be an existing nonempty absolute file")
    if args.max_model_steps < 1:
        raise ValueError("--max-model-steps must be positive")
    if not Path(MUSE).is_absolute() or not os.access(MUSE, os.X_OK):
        raise ValueError("Muse binary must be an absolute executable path")
    if args.action == "open" and args.no_session_log:
        raise ValueError("reusable workers require session logging to preserve follow-up history")
    if args.session_id and args.no_session_log:
        raise ValueError("--session-id requires retained session logging")
    session_id = None if args.no_session_log else str(uuid.UUID(args.session_id)) if args.session_id else str(uuid.uuid4())
    command = [MUSE, "exec", "--workspace", str(workspace), "--trust-workspace",
               "--disable-approval", "--json", "--provider", args.provider or "meta",
               "--max-model-steps", str(args.max_model_steps), "--user-input-auto-resolve",
               "--prompt-file", str(prompt)]
    if session_id:
        command += ["--session-id", session_id]
    if args.provider != "echo":
        command += ["--model", MODEL, "--reasoning-effort", args.reasoning_effort]
    if args.read_only:
        command += ["--disable-write", "--disable-shell"]
    if not args.web:
        command += ["--disable-web-tools"]
    if args.worktree:
        command += ["-w", "create"]
    if args.worktree_existing:
        actual = Path(args.worktree_existing)
        if not actual.is_absolute() or not actual.is_dir():
            raise ValueError("--worktree-existing must be an existing absolute directory")
        command += ["-w", "existing", "--worktree-existing", str(actual)]
    if args.worktree_base:
        if not args.worktree:
            raise ValueError("--worktree-base requires --worktree")
        command += ["--worktree-base", args.worktree_base]
    if args.output_schema:
        schema = Path(args.output_schema)
        if not schema.is_absolute() or not schema.is_file():
            raise ValueError("--output-schema must be an existing absolute file")
        command += ["--output-schema", str(schema)]
    if args.no_session_log:
        command += ["--no-session-log"]
    job_id = uuid.uuid4().hex[:12]
    path = root() / job_id
    path.mkdir(mode=0o700)
    (path / "requests").mkdir(mode=0o700)
    save(path / "policy.json", dict(reasoning_effort=args.reasoning_effort,
                                   max_model_steps=args.max_model_steps))
    state = dict(id=job_id, label=args.label or prompt.stem, workspace=str(workspace), log_dir=str(path),
                 prompt_file=str(prompt), command=command, status="starting", phase="starting",
                 started_at=time.time(), steps=0, task_failures=0, malformed_events=0,
                 model=MODEL, reasoning_effort=args.reasoning_effort, muse_session_id=session_id,
                 reusable=args.action == "open", turn=1)
    if args.worktree_existing:
        state["actual_workspace"] = str(Path(args.worktree_existing))
    save(path / "state.json", state)
    return path, state


def report(state):
    path = job_path(state["id"])
    print(f"{state['id']} {state['status']} | {clean(state['label'])}")
    if state.get("muse_session_id"):
        print(f"Muse session: {state['muse_session_id']} | {state['model']} | {state['reasoning_effort']}")
    if state.get("error"):
        print(clean(state["error"]))
    if state.get("task_failures"):
        print(f"Task failures reported: {state['task_failures']} (inspect logs before accepting work)")
    if state.get("quota_exhausted"):
        print(f"Muse subscription quota exhausted. Reset: {state.get('quota_reset_at', 'not reported')}.")
        print("Ask the user to approve a specific fallback subagent/model or wait for reset; no automatic switch.")
    print(f"Logs: {path}")
    result = path / "result.txt"
    if result.exists():
        print(result.read_text())
    sys.stdout.flush()


def summarize_usage(export):
    """Read model usage once per completion; attribution events repeat these totals."""
    calls = []
    for item in export.get("events", []):
        envelope = item.get("envelope", {})
        event = envelope.get("payload", {}).get("event", {})
        if event.get("kind") != "model_completed":
            continue
        usage = event.get("usage")
        if not isinstance(usage, dict):
            continue
        calls.append(dict(model=event.get("model"), **{
            key: value for key, value in usage.items()
            if key in {"input_tokens", "output_tokens", "cached_tokens", "reasoning_tokens",
                       "cache_read_tokens", "cache_write_tokens"}
            and isinstance(value, int) and not isinstance(value, bool) and value >= 0}))
    totals = {key: sum(call[key] for call in calls if key in call)
              for key in {"input_tokens", "output_tokens", "cached_tokens", "reasoning_tokens"}
              if any(key in call for call in calls)}
    complete = bool(calls) and all("input_tokens" in c and "cached_tokens" in c for c in calls)
    inputs = totals.get("input_tokens", 0)
    ratio = totals["cached_tokens"] / inputs if complete and inputs else None
    return dict(model_calls=len(calls), calls=calls, totals=totals, cache_hit_ratio=ratio)


def session_usage(state):
    if not state.get("muse_session_id") or "--no-session-log" in state["command"]:
        raise ValueError("cache usage needs a retained Muse session log")
    with tempfile.TemporaryDirectory(prefix="muse-usage-") as temporary:
        target = Path(temporary) / "session.json"
        result = subprocess.run([MUSE, "export", "--session", state["muse_session_id"],
                                 "--out", str(target), "--redacted"],
                                capture_output=True, text=True, timeout=15)
        if result.returncode:
            raise ValueError("Muse usage export failed: " + clean(result.stderr))
        return summarize_usage(json.loads(target.read_text()))


def tmux_view(arguments):
    if not shutil.which("tmux") or not shutil.which("codex"):
        raise ValueError("tmux and codex must be installed")
    session = "codex-workers-" + uuid.uuid4().hex[:8]
    codex_args = arguments[1:] if arguments[:1] == ["--"] else arguments
    codex_command = "exec " + shlex.join(["env", f"CODEX_WORKERS_DIR={root()}",
                                         shutil.which("codex"), *codex_args])
    dashboard_command = "exec " + shlex.join([launcher_path(), "watch", "--compact"])
    environment = {"CODEX_WORKERS_DIR": str(root())}
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
        status_command = shlex.join(["env", f"CODEX_WORKERS_DIR={root()}", launcher_path(), "status-line"])
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
        descriptions = {"run": "run one tracked Muse turn", "start": "detach one tracked Muse turn",
                        "open": "keep a Muse worker available for related follow-up turns"}
        launch = commands.add_parser(action, help=descriptions[action])
        launch.add_argument("--workspace", required=True)
        launch.add_argument("--prompt-file", required=True)
        launch.add_argument("--label")
        launch.add_argument("--reasoning-effort", default="high", choices=EFFORTS)
        launch.add_argument("--max-model-steps", type=int, default=100)
        launch.add_argument("--read-only", action="store_true")
        isolation = launch.add_mutually_exclusive_group()
        isolation.add_argument("--worktree", action="store_true")
        isolation.add_argument("--worktree-existing", help="resume a lane in its existing absolute worktree path")
        launch.add_argument("--worktree-base")
        launch.add_argument("--web", action="store_true", help="enable Muse web tools")
        launch.add_argument("--model", default=MODEL, choices=[MODEL])
        launch.add_argument("--session-id", help="reuse this durable Muse session UUID with the same workspace and policy")
        launch.add_argument("--output-schema")
        launch.add_argument("--no-session-log", action="store_true")
        launch.add_argument("--provider", choices=["meta", "echo"])
    listing = commands.add_parser("list", help="list active workers and recent results")
    listing.add_argument("--json", action="store_true")
    for action in ("show", "logs", "wait", "cancel", "close", "usage"):
        command = commands.add_parser(action)
        command.add_argument("id")
        if action in {"show", "usage"}:
            command.add_argument("--json", action="store_true")
        if action == "logs":
            command.add_argument("--stderr", action="store_true")
            command.add_argument("--lines", type=int, default=20)
    followup = commands.add_parser("followup", help="submit related work to an existing reusable worker")
    followup.add_argument("id")
    followup.add_argument("--prompt-file", required=True)
    followup.add_argument("--label")
    followup.add_argument("--reasoning-effort", choices=EFFORTS, help="override this turn; otherwise use worker policy")
    followup.add_argument("--max-model-steps", type=int, help="override this turn's step cap")
    effort = commands.add_parser("effort", help="change a reusable worker's default effort for future turns")
    effort.add_argument("id")
    effort.add_argument("--reasoning-effort", required=True, choices=EFFORTS)
    effort.add_argument("--max-model-steps", type=int)
    watch = commands.add_parser("watch", help="live dashboard; Ctrl-C stops the view, not workers")
    watch.add_argument("--interval", type=float, default=1.0)
    watch.add_argument("--once", action="store_true")
    watch.add_argument("--no-color", action="store_true")
    watch.add_argument("--compact", action="store_true", help="at most five lines; one row per worker")
    commands.add_parser("status-line", help="one-line summary for tmux or shell status bars")
    tmux = commands.add_parser("tmux", help="open Codex with a live worker pane and status bar")
    tmux.add_argument("codex_args", nargs=argparse.REMAINDER)
    return result


def main(launcher=None):
    if launcher is not None:
        set_launcher_path(launcher)
    if len(sys.argv) == 3 and sys.argv[1] == "_supervise":
        return supervise(sys.argv[2])
    args = parser().parse_args()
    if args.action in {"run", "start", "open"}:
        path, state = create_job(args)
        print(f"Worker {state['id']} | {clean(state['label'])} | {path}", flush=True)
        if args.action in {"run", "open"}:
            code = reusable_worker(state["id"]) if args.action == "open" else supervise(state["id"])
            report(load(path))
            return code
        with (path / "supervisor.log").open("w") as log:
            subprocess.Popen([sys.executable, launcher_path(), "_supervise", state["id"]],
                             stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        print(f"Inspect: codex-workers show {state['id']}\nCancel:  codex-workers cancel {state['id']}")
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
        usage = session_usage(load(job_path(args.id)))
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
        path = job_path(args.id)
        while True:
            state = load(path)
            if state["status"] not in ACTIVE or (state["status"] == "idle" and not list((path / "requests").glob("*.json"))):
                report(state)
                return outcome_code(state)
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
        request = f"{time.time_ns():020}-{uuid.uuid4().hex}.json"
        save(path / "requests" / request, dict(prompt_file=str(prompt), label=args.label,
                                               reasoning_effort=args.reasoning_effort,
                                               max_model_steps=args.max_model_steps))
        print(f"Follow-up queued for {args.id} in Muse session {state['muse_session_id']}")
    elif args.action == "effort":
        path = job_path(args.id)
        state = load(path)
        if not state.get("reusable") or state["status"] not in ACTIVE or (path / "close.request").exists():
            raise ValueError("worker is not available for policy changes")
        if args.max_model_steps is not None and args.max_model_steps < 1:
            raise ValueError("--max-model-steps must be positive")
        policy = json.loads((path / "policy.json").read_text())
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
        print(f"Cancellation requested for {args.id}. Check with codex-workers wait {args.id}.")
    elif args.action == "tmux":
        return tmux_view(args.codex_args)
    return 0


def entrypoint(launcher):
    os.umask(0o077)
    try:
        return main(launcher)
    except KeyboardInterrupt:
        return 130
    except (ValueError, OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        print(f"codex-workers: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(entrypoint(Path(sys.argv[0]).resolve()))
