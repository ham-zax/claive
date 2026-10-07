"""Supervised goal queue: `claive goal` writes goals, `claive serve` works them with no parent session.

claive is parent-driven: a Claude/Codex/Pi session launches workers, waits and answers questions.
On a server there is no parent, so `claive serve` (run by a systemd user unit) plays that role for
a durable queue in $CLAIVE_DIR/goals/<id>/goal.json:

- each goal runs as an ordinary reusable worker (`claive open --detach --report`), read-only unless
  the goal was added with --write, inside the claive config workspaces allowlist;
- a worker question (exit 3) parks the goal, records the question, posts a goal event to the inbox
  and the queue moves on; `claive goal answer` resumes the same session;
- each goal has a wall-clock budget; a goal over budget is cancelled and marked timed_out;
- provider failures (quota, protocol, launch) pause all launches with exponential backoff, and a
  goal is retried up to its attempt limit;
- $CLAIVE_DIR/supervisor/stop is checked between turns: serve cancels its workers, requeues
  running goals and exits 0 (`claive serve --stop`, undone by `--resume`);
- SIGTERM (systemctl stop/restart) exits without touching workers; they run in their own
  sessions and the next serve adopts or harvests them;
- logs go to $CLAIVE_DIR/supervisor/serve.log and stdout (the journal).
"""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import uuid

from claivelib import cli as cli_mod
from claivelib import config
from claivelib import inbox as inbox_mod

TERMINAL = {"done", "failed", "timed_out", "cancelled"}
PROVIDER_KINDS = {"quota", "protocol", "launch"}
BACKOFF_BASE = 30
BACKOFF_CAP = 1800
DEFAULT_BUDGET = 7200
DEFAULT_ATTEMPTS = 3


def goals_dir():
    path = cli_mod.root() / "goals"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def supervisor_dir():
    path = cli_mod.root() / "supervisor"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def stop_file():
    return supervisor_dir() / "stop"


def goal_path(goal_id):
    if not re.fullmatch(r"[0-9a-f]{12}", goal_id or ""):
        raise ValueError("invalid goal ID")
    path = goals_dir() / goal_id
    if not (path / "goal.json").is_file():
        raise ValueError(f"unknown goal: {goal_id}")
    return path


def load_goal(goal_id):
    return json.loads((goal_path(goal_id) / "goal.json").read_text())


@contextmanager
def updating(goal_id):
    path = goal_path(goal_id)
    with open(path / "lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = json.loads((path / "goal.json").read_text())
        yield data
        cli_mod.save(path / "goal.json", data)


def all_goals():
    goals = []
    for path in sorted(goals_dir().iterdir()):
        if (path / "goal.json").is_file():
            try:
                goals.append(json.loads((path / "goal.json").read_text()))
            except ValueError:
                continue
    return sorted(goals, key=lambda goal: goal["created_at"])


def parse_duration(text):
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smh]?)\s*", str(text))
    if not match or float(match.group(1)) <= 0:
        raise ValueError("--budget must be a positive duration such as 900, 30m or 2h")
    return float(match.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[match.group(2)]


def note(goal, text):
    goal.setdefault("history", []).append(dict(at=time.time(), text=text))
    goal["history"] = goal["history"][-50:]


def used(goal, now=None):
    running = (now or time.time()) - goal["running_since"] if goal.get("running_since") else 0
    return goal.get("budget_used", 0) + running


def pause_clock(goal, now=None):
    goal["budget_used"] = used(goal, now)
    goal["running_since"] = None


def post(goal, status, needs_parent=None):
    event = dict(type="goal", seq=time.time_ns(), at=inbox_mod._now_iso(), id=goal["id"],
                 label=goal["title"], status=status, worker=goal.get("worker"),
                 needs_parent=needs_parent, code=None)
    try:
        inbox_mod.append(event)
    except OSError:
        pass


def claive_command(*args):
    """Run claive in a child; serve is not a worker, so the recursion guard must not trip."""
    env = {key: value for key, value in os.environ.items() if key != "CLAIVE_WORKER_ID"}
    return subprocess.run([sys.executable, cli_mod.launcher_path(), *args], env=env, text=True,
                          capture_output=True, timeout=120, stdin=subprocess.DEVNULL)


# --- claive goal -------------------------------------------------------------------------------

def cmd_add(args):
    prompt = Path(args.prompt_file)
    if not prompt.is_absolute() or not prompt.is_file() or not prompt.stat().st_size:
        raise ValueError("--prompt-file must be an existing nonempty absolute file")
    workspace = Path(args.workspace).expanduser()
    if not workspace.is_absolute() or not workspace.is_dir():
        raise ValueError("--workspace must be an existing absolute directory")
    budget = parse_duration(args.budget) if args.budget is not None else DEFAULT_BUDGET
    if args.max_attempts is not None and args.max_attempts < 1:
        raise ValueError("--max-attempts must be positive")
    config.check_workspace([workspace], read_only=not args.write)
    goal_id = uuid.uuid4().hex[:12]
    path = goals_dir() / goal_id
    path.mkdir(mode=0o700)
    (path / "prompt.md").write_text(prompt.read_text())
    goal = dict(id=goal_id, title=args.title, workspace=str(workspace), role=args.role,
                engine=args.engine, model=args.model, write=bool(args.write), budget=budget,
                max_attempts=args.max_attempts or DEFAULT_ATTEMPTS, status="queued",
                created_at=time.time(), attempts=0, worker=None, session_id=None,
                running_since=None, budget_used=0, not_before=0, question=None,
                resume_prompt=None, result=None, error=None, history=[])
    note(goal, "added")
    cli_mod.save(path / "goal.json", goal)
    print(f"Goal {goal_id} | {args.title} | {'write' if args.write else 'read-only'} | "
          f"budget {round(budget)}s")
    return 0


def render(goals):
    if not goals:
        return "No goals"
    lines = []
    for goal in goals:
        line = (f"{goal['id']} {goal['status'].upper():9} attempts={goal['attempts']}/{goal['max_attempts']} "
                f"used={round(used(goal))}s/{round(goal['budget'])}s worker={goal.get('worker') or '-'} "
                f"{goal['title']}")
        if goal["status"] == "parked" and goal.get("question"):
            line += f"\n    question: {goal['question']}\n    answer: claive goal answer {goal['id']} --message ..."
        elif goal.get("error") and goal["status"] in {"failed", "timed_out", "queued"}:
            line += f"\n    {goal['error']}"
        lines.append(line)
    return "\n".join(lines)


def cmd_list(json_output=False, all_goals_flag=False):
    goals = [goal for goal in all_goals() if all_goals_flag or goal["status"] not in TERMINAL]
    print(json.dumps(goals, indent=2) if json_output else render(goals))
    return 0


def cmd_show(goal_id, json_output=False):
    goal = load_goal(goal_id)
    if json_output:
        print(json.dumps(goal, indent=2))
        return 0
    print(render([goal]))
    if goal.get("result"):
        print(f"Result: {goal['result'].get('summary') or ''}\nResult file: {goal['result'].get('file')}")
    for entry in goal.get("history", [])[-10:]:
        print(f"  {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(entry['at']))} {entry['text']}")
    return 0


def cmd_answer(goal_id, message):
    with updating(goal_id) as goal:
        if goal["status"] != "parked":
            raise ValueError(f"goal {goal_id} is {goal['status']}, not waiting for an answer")
        worker = goal.get("worker")
        live = False
        if worker:
            try:
                state = cli_mod.load(cli_mod.job_path(worker))
                live = state["status"] == "idle" and bool(state.get("needs_parent"))
            except ValueError:
                live = False
        if live:
            result = claive_command("answer", worker, "--message", message)
            if result.returncode:
                raise ValueError(f"answer failed: {(result.stderr or result.stdout).strip()}")
            goal.update(status="running", running_since=time.time(), question=None)
            note(goal, "answered; worker resumed")
        else:
            # The worker was stopped (stop switch or restart); resume its session in a new worker.
            answer = goal_path(goal_id) / f"answer-{time.time_ns()}.md"
            answer.write_text(f"Parent question was:\n{goal.get('question') or ''}\n\nParent decision:\n{message}\n\n"
                              "Continue the assignment with this decision.\n")
            goal.update(status="queued", resume_prompt=str(answer), question=None, worker=None, not_before=0)
            note(goal, "answered; queued to resume the session")
    print(f"Answer recorded for goal {goal_id}")
    return 0


def cancel_worker(worker):
    if not worker:
        return
    try:
        state = cli_mod.load(cli_mod.job_path(worker))
    except ValueError:
        return
    if state["status"] in cli_mod.ACTIVE:
        claive_command("cancel", worker)


def cmd_cancel(goal_id):
    with updating(goal_id) as goal:
        if goal["status"] in TERMINAL:
            print(f"Goal {goal_id} is already {goal['status']}")
            return 0
        cancel_worker(goal.get("worker"))
        pause_clock(goal)
        goal.update(status="cancelled")
        note(goal, "cancelled by operator")
    print(f"Goal {goal_id} cancelled")
    return 0


def cmd_retry(goal_id):
    with updating(goal_id) as goal:
        if goal["status"] not in {"failed", "timed_out", "cancelled"}:
            raise ValueError(f"goal {goal_id} is {goal['status']}; only failed, timed_out or cancelled goals retry")
        goal.update(status="queued", attempts=0, worker=None, budget_used=0, running_since=None,
                    not_before=0, error=None, resume_prompt=None)
        note(goal, "requeued by operator")
    print(f"Goal {goal_id} queued")
    return 0


# --- claive serve ------------------------------------------------------------------------------

class Server:
    def __init__(self, interval=5.0, max_parallel=1):
        self.interval = interval
        self.max_parallel = max_parallel
        self.terminating = False
        self.state_path = supervisor_dir() / "state.json"

    def log(self, text):
        line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {text}"
        print(line, flush=True)
        with open(supervisor_dir() / "serve.log", "a") as stream:
            stream.write(line + "\n")

    def backoff(self):
        try:
            return json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return dict(level=0, until=0)

    def set_backoff(self, data):
        cli_mod.save(self.state_path, data)

    def provider_failed(self, kind):
        data = self.backoff()
        data["level"] = data.get("level", 0) + 1
        delay = min(BACKOFF_BASE * 2 ** (data["level"] - 1), BACKOFF_CAP)
        data.update(until=time.time() + delay, kind=kind)
        self.set_backoff(data)
        self.log(f"provider failure ({kind}); launches paused {delay}s (level {data['level']})")

    def provider_ok(self):
        if self.backoff().get("level"):
            self.set_backoff(dict(level=0, until=0))
            self.log("provider recovered; backoff cleared")

    # One pass: stop switch, then reconcile workers, then launch.
    def tick(self):
        if stop_file().exists():
            self.stop_all()
            return False
        for goal in all_goals():
            if goal["status"] in {"running", "parked"}:
                with updating(goal["id"]) as current:
                    if current["status"] in {"running", "parked"}:
                        self.reconcile(current)
        self.launch_ready()
        return True

    def stop_all(self):
        for goal in all_goals():
            if goal["status"] not in {"running", "parked"}:
                continue
            with updating(goal["id"]) as current:
                if current["status"] not in {"running", "parked"}:
                    continue
                worker = current.get("worker")
                cancel_worker(worker)
                self.wait_stopped(worker)
                pause_clock(current)
                if current["status"] == "running":
                    current.update(status="queued", worker=None)
                    note(current, "worker cancelled by stop switch; requeued")
                else:
                    note(current, "idle worker stopped by stop switch; still waiting for an answer")
                self.log(f"goal {current['id']}: stop switch cancelled worker {worker or '-'}")
        self.log(f"stop switch {stop_file()} present; exiting (remove with claive serve --resume)")

    def wait_stopped(self, worker, timeout=15):
        if not worker:
            return
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                state = cli_mod.load(cli_mod.job_path(worker))
            except ValueError:
                return
            if state["status"] not in cli_mod.ACTIVE:
                return
            time.sleep(0.2)

    def reconcile(self, goal):
        try:
            path = cli_mod.job_path(goal["worker"])
            state = cli_mod.load(path)
        except (ValueError, TypeError):
            if goal["status"] == "parked":
                return  # its worker is gone; `claive goal answer` resumes the session
            return self.failed(goal, "launch", "worker record missing")
        goal["session_id"] = state.get("session_id") or goal.get("session_id")
        pending = list((path / "requests").glob("*.json")) if (path / "requests").is_dir() else []
        turn_over = state["status"] not in cli_mod.ACTIVE or (state["status"] == "idle" and not pending)
        if goal["status"] == "parked":
            if not turn_over:  # answered directly with `claive answer`
                goal.update(status="running", running_since=time.time(), question=None)
                note(goal, "worker resumed by a direct claive answer")
            return
        if not turn_over:
            if used(goal) > goal["budget"]:
                cancel_worker(goal["worker"])
                self.wait_stopped(goal["worker"])
                pause_clock(goal)
                goal.update(status="timed_out", error=f"wall-clock budget {round(goal['budget'])}s exceeded")
                note(goal, goal["error"])
                self.log(f"goal {goal['id']}: {goal['error']}; worker {goal['worker']} cancelled")
                post(goal, "timed_out")
            return
        turn_status = state.get("last_turn_status", "failed") if state["status"] == "idle" else state["status"]
        if state.get("needs_parent"):
            pause_clock(goal)
            question = state["needs_parent"].get("question", "")
            goal.update(status="parked", question=question)
            note(goal, f"parked: {question}")
            self.log(f"goal {goal['id']}: parked on a question from worker {goal['worker']}")
            self.provider_ok()
            post(goal, "parked", state["needs_parent"])
            return
        if turn_status == "completed":
            pause_clock(goal)
            report = state.get("report") or {}
            goal.update(status="done", error=None, result=dict(
                summary=report.get("summary"), report_status=report.get("status"),
                file=str(path / "result.txt")))
            note(goal, "done")
            self.close(goal["worker"], state)
            self.provider_ok()
            self.log(f"goal {goal['id']}: done (worker {goal['worker']})")
            post(goal, "done")
            return
        kind = state.get("failure_kind") or ("interrupted" if turn_status == "interrupted" else "worker")
        self.close(goal["worker"], state)
        self.failed(goal, kind, state.get("error") or turn_status)

    def close(self, worker, state):
        if state["status"] == "idle":
            claive_command("close", worker)

    def failed(self, goal, kind, error):
        pause_clock(goal)
        if kind in PROVIDER_KINDS:
            self.provider_failed(kind)
        goal["error"] = f"{kind}: {error}"[:300]
        if kind == "rejected" or goal["attempts"] >= goal["max_attempts"]:
            goal.update(status="failed")
            note(goal, f"failed after {goal['attempts']} attempts: {goal['error']}")
            self.log(f"goal {goal['id']}: failed ({goal['error']})")
            post(goal, "failed")
            return
        delay = min(BACKOFF_BASE * 2 ** (goal["attempts"] - 1), BACKOFF_CAP)
        goal.update(status="queued", worker=None, not_before=time.time() + delay)
        note(goal, f"attempt {goal['attempts']} failed ({goal['error']}); retry in {delay}s")
        self.log(f"goal {goal['id']}: attempt {goal['attempts']} failed ({goal['error']}); retry in {delay}s")

    def launch_ready(self):
        now = time.time()
        goals = all_goals()
        running = sum(goal["status"] == "running" for goal in goals)
        if self.backoff().get("until", 0) > now:
            return
        for goal in goals:
            if running >= self.max_parallel or self.terminating or stop_file().exists():
                return
            if goal["status"] != "queued" or goal.get("not_before", 0) > now:
                continue
            with updating(goal["id"]) as current:
                if current["status"] != "queued":
                    continue
                if used(current) > current["budget"]:
                    current.update(status="timed_out", error=f"wall-clock budget {round(current['budget'])}s exceeded")
                    post(current, "timed_out")
                    continue
                self.launch(current)
                running += current["status"] == "running"
            if self.backoff().get("until", 0) > time.time():
                return

    def launch(self, goal):
        path = goal_path(goal["id"])
        resume = goal.get("resume_prompt")
        command = ["open", "--detach", "--report", "--workspace", goal["workspace"],
                   "--prompt-file", resume or str(path / "prompt.md"),
                   "--label", f"goal {goal['id']}: {goal['title']}"[:120]]
        if not goal.get("write"):
            command.append("--read-only")
        for flag, key in (("--role", "role"), ("--engine", "engine"), ("--model", "model")):
            if goal.get(key):
                command += [flag, goal[key]]
        if resume and goal.get("session_id"):
            command += ["--session-id", goal["session_id"]]
        goal["attempts"] += 1
        result = claive_command(*command)
        match = re.search(r"^Worker ([0-9a-f]{12}) ", result.stdout, re.M)
        if result.returncode or not match:
            error = (result.stderr or result.stdout).strip().splitlines()
            error = error[-1] if error else f"claive open exited {result.returncode}"
            if match is None and "claive:" in (result.stderr or ""):
                # Refused before launch (allowlist, config, bad options): retrying cannot help.
                goal.update(status="failed", error=f"refused: {error}"[:300])
                note(goal, goal["error"])
                self.log(f"goal {goal['id']}: {goal['error']}")
                post(goal, "failed")
                return
            return self.failed(goal, "launch", error)
        goal.update(status="running", worker=match.group(1), running_since=time.time(), resume_prompt=None)
        note(goal, f"attempt {goal['attempts']}: worker {goal['worker']}{' resuming session' if resume else ''}")
        self.log(f"goal {goal['id']}: launched worker {goal['worker']} (attempt {goal['attempts']}, "
                 f"{'write' if goal.get('write') else 'read-only'})")

    def run(self, once=False):
        lock = open(supervisor_dir() / "serve.lock", "a+")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.seek(0)
            raise ValueError(f"another claive serve is running (pid {lock.read().strip() or '?'})")
        lock.seek(0)
        lock.truncate()
        lock.write(str(os.getpid()))
        lock.flush()

        def terminate(signum, _frame):
            self.terminating = True
        signal.signal(signal.SIGTERM, terminate)
        signal.signal(signal.SIGINT, terminate)
        if not once:
            self.log(f"serve started (pid {os.getpid()}, interval {self.interval:g}s, "
                     f"max parallel {self.max_parallel})")
        while True:
            if not self.tick():
                return 0
            if once:
                return 0
            deadline = time.monotonic() + self.interval
            while time.monotonic() < deadline and not self.terminating and not stop_file().exists():
                time.sleep(0.2)
            if self.terminating:
                left = [goal["worker"] for goal in all_goals() if goal["status"] == "running"]
                self.log(f"serve exiting on signal; {len(left)} worker(s) left running for the next serve")
                return 0


def cmd_serve(args):
    if args.stop:
        stop_file().write_text(f"requested at {time.strftime('%Y-%m-%dT%H:%M:%S%z')}\n")
        print(f"Stop switch set: {stop_file()}. serve cancels its workers at its next check and exits.")
        return 0
    if args.resume:
        stop_file().unlink(missing_ok=True)
        print("Stop switch cleared; start serve again (systemctl --user start claive-serve).")
        return 0
    if args.status:
        backoff = Server().backoff()
        counts = {}
        for goal in all_goals():
            counts[goal["status"]] = counts.get(goal["status"], 0) + 1
        lock = supervisor_dir() / "serve.lock"
        pid = lock.read_text().strip() if lock.exists() else ""
        alive = bool(pid) and Path(f"/proc/{pid}").exists()
        print(f"serve: {'running pid ' + pid if alive else 'not running'}; "
              f"stop switch {'SET' if stop_file().exists() else 'clear'}; "
              f"backoff {max(0, round(backoff.get('until', 0) - time.time()))}s; "
              + (", ".join(f"{count} {status}" for status, count in sorted(counts.items())) or "no goals"))
        return 0
    if args.interval <= 0 or args.max_parallel < 1:
        raise ValueError("--interval must be positive and --max-parallel at least 1")
    cli_mod.check_nested()
    return Server(args.interval, args.max_parallel).run(once=args.once)
