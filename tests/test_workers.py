import hermetic  # noqa: F401  (must run before claivelib reads the environment)
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import time
import unittest
import sys
import uuid
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
CLI = str(REPO_ROOT / "bin/claive")
FIXTURE_CLI = str(REPO_ROOT / "tests/fixture-workers")
FIXTURE_WORKER = str(REPO_ROOT / "tests/fixture_worker.py")
sys.path.insert(0, str(REPO_ROOT / "bin"))
from claivelib import cli as module
from claivelib.state import launch_config, normalized
module.set_launcher_path(CLI)
MUSE_MODEL = "muse-spark-1.3-contributor"

FAKE = """#!/usr/bin/python3
import json, os, signal, subprocess, sys, time
mode = os.environ.get("MUSE_TEST_MODE", "success")
def emit(kind, **payload):
    print(json.dumps({"payload_type": kind, "payload": payload}), flush=True)
emit("task.lifecycle.proposed", event={"task_kind": "model.meta.response"})
if mode == "brief":
    time.sleep(0.3)
if mode == "worktree":
    print("muse: workspace root: " + os.environ["MUSE_TEST_WORKTREE"] + " (worktree)", file=sys.stderr)
if mode == "slow":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    child = subprocess.Popen([sys.executable, "-c", "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"])
    print(json.dumps({"payload_type": "test.child", "payload": {"pid": child.pid}}), flush=True)
    time.sleep(60)
if mode == "malformed":
    print("NOT_JSON", flush=True)
if mode == "quota":
    reason = "API error 429: Subscription quota exhausted. Your usage window resets at 2099-10-02T14:51:16Z. (rate_limit_error)"
    emit("task.lifecycle.failed", event={"reason": reason})
    emit("run.terminal.failed", terminal="failed", reason=reason, text="")
    sys.exit(1)
if mode != "missing":
    emit("run.terminal.completed", terminal="completed", text="fixture result")
sys.exit(7 if mode == "exit-failure" else 0)
"""


class WorkerChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="workers-check-")
        self.path = Path(self.temp.name)
        self.registry = self.path / "registry"
        self.fake = self.path / "fake muse"
        self.fake.write_text(FAKE)
        self.fake.chmod(0o700)
        self.prompt = self.path / "task;$(touch INJECTED).md"
        self.prompt.write_text("A narrow fixture task.")
        self.env = dict(os.environ, CLAIVE_DIR=str(self.registry),
                        MUSE_WORKER_BINARY=str(self.fake), TERM="xterm-256color")
        self.open_processes = []

    def tearDown(self):
        if self.registry.exists():
            for folder in self.registry.iterdir():
                if (folder / "state.json").exists():
                    state = json.loads((folder / "state.json").read_text())
                    for field in ("worker", "supervisor"):
                        pid = state.get(field + "_pid")
                        if pid and module.identity(pid) == state.get(field + "_identity"):
                            try:
                                os.killpg(pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
        for process in self.open_processes:
            process.communicate(timeout=5)
        self.temp.cleanup()

    def cli(self, *args, mode="success"):
        env = dict(self.env, MUSE_TEST_MODE=mode)
        return subprocess.run([CLI, *args], env=env, text=True, capture_output=True, timeout=12)

    def fixture_cli(self, *args, mode="success", timeout=12):
        env = dict(self.env, FIXTURE_WORKER_BINARY=FIXTURE_WORKER, FIXTURE_MODE=mode)
        return subprocess.run([FIXTURE_CLI, *args], env=env, text=True,
                              capture_output=True, timeout=timeout)

    def fixture_launch(self, action="run", mode="success", extra=()):
        completed = self.fixture_cli(action, "--engine", "fixture",
                                     "--workspace", str(self.path),
                                     "--prompt-file", str(self.prompt), "--label", "fixture-engine",
                                     *extra, mode=mode)
        worker_id = re.search(r"Worker ([0-9a-f]{12})", completed.stdout).group(1)
        state = json.loads((self.registry / worker_id / "state.json").read_text())
        return completed, state

    def launch(self, action="run", mode="success", extra=()):
        completed = self.cli(action, "--workspace", str(self.path),
                             "--prompt-file", str(self.prompt), "--label", "fixture",
                             *extra, mode=mode)
        worker_id = re.search(r"Worker ([0-9a-f]{12})", completed.stdout).group(1)
        state = json.loads((self.registry / worker_id / "state.json").read_text())
        return completed, state

    def until_running(self, job):
        for _ in range(80):
            state = json.loads((self.registry / job / "state.json").read_text())
            if state.get("worker_pid"):
                return state
            time.sleep(0.025)
        self.fail("worker did not start")

    def open_worker(self, extra=(), mode="success", env=None):
        before = set(self.registry.glob("*/state.json"))
        process = subprocess.Popen([CLI, "open", "--workspace", str(self.path),
                                    "--prompt-file", str(self.prompt), *extra],
                                   env=dict(env or self.env, MUSE_TEST_MODE=mode),
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, start_new_session=True)
        self.open_processes.append(process)
        for _ in range(400):
            new = set(self.registry.glob("*/state.json")) - before
            if new:
                state = json.loads(next(iter(new)).read_text())
                return process, state
            if process.poll() is not None:
                self.fail(str(process.communicate()))
            time.sleep(0.025)
        self.fail("reusable worker did not register")

    def until_idle(self, job, turn=1):
        for _ in range(600):
            state = json.loads((self.registry / job / "state.json").read_text())
            if state["status"] == "idle" and state["turn"] == turn:
                return state
            time.sleep(0.025)
        self.fail("worker did not become idle after assigned turn")

    def test_pinned_model_xhigh_defaults_and_rejected_overrides(self):
        result, state = self.launch()
        self.assertEqual(result.returncode, 0)
        command = state["command"]
        self.assertEqual(command[command.index("--model") + 1], "muse-spark-1.3-contributor")
        self.assertEqual(command[command.index("--reasoning-effort") + 1], "xhigh")
        self.assertEqual(command[command.index("--max-model-steps") + 1], "100")
        for option, value in [("--model", "other-model"), ("--reasoning-effort", "low")]:
            result = self.cli("run", "--workspace", str(self.path), "--prompt-file", str(self.prompt), option, value)
            self.assertNotEqual(result.returncode, 0)

    def test_new_jobs_persist_structured_v2_launch_config(self):
        schema = self.path / "schema.json"
        schema.write_text("{}")
        existing = self.path / "existing-worktree"
        existing.mkdir()
        result, state = self.launch(extra=("--read-only", "--web", "--output-schema", str(schema),
                                           "--worktree-existing", str(existing)))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(state["schema_version"], 2)
        self.assertEqual(state["engine"], "muse")
        self.assertTrue(state["session_id"])
        launch = state["launch"]
        self.assertEqual(launch["binary"], str(self.fake))
        self.assertEqual(launch["provider"], "meta")
        self.assertEqual(launch["model"], MUSE_MODEL)
        self.assertTrue(launch["read_only"])
        self.assertTrue(launch["web"])
        self.assertEqual(launch["output_schema"], str(schema))
        self.assertTrue(launch["session_logging"])
        self.assertEqual(launch["isolation"],
                         {"mode": "existing", "base": None, "existing_path": str(existing)})

    def test_legacy_muse_command_decodes_without_rewriting_state(self):
        legacy = {
            "id": "a1b2c3d4e5f6",
            "muse_session_id": "2c3db702-57c9-46ba-b2a1-f2a3b1e3e09d",
            "command": [
                str(self.fake), "exec", "--workspace", str(self.path), "--trust-workspace",
                "--disable-approval", "--json", "--provider", "meta", "--max-model-steps", "17",
                "--user-input-auto-resolve", "--prompt-file", str(self.prompt), "--session-id",
                "2c3db702-57c9-46ba-b2a1-f2a3b1e3e09d", "--model", MUSE_MODEL,
                "--reasoning-effort", "xhigh", "--disable-write", "--disable-shell",
                "--worktree-existing", str(self.path), "-w", "existing", "--worktree-base", "HEAD~1",
                "--output-schema", str(self.prompt), "--no-session-log"
            ],
        }
        decoded = launch_config(legacy)
        self.assertEqual(decoded["binary"], str(self.fake.resolve()))
        self.assertEqual(decoded["provider"], "meta")
        self.assertEqual(decoded["model"], MUSE_MODEL)
        self.assertTrue(decoded["read_only"])
        self.assertTrue(decoded["web"])
        self.assertEqual(decoded["output_schema"], str(self.prompt))
        self.assertFalse(decoded["session_logging"])
        self.assertEqual(decoded["isolation"],
                         {"mode": "existing", "base": "HEAD~1", "existing_path": str(self.path)})
        view = normalized(legacy)
        self.assertEqual(view["engine"], "muse")
        self.assertEqual(view["session_id"], legacy["muse_session_id"])
        self.assertEqual(view["source_schema_version"], 1)
        self.assertNotIn("schema_version", legacy)
        self.assertNotIn("launch", legacy)

    def test_new_cli_preserves_v1_followup_policy_and_close_protocol(self):
        job = "a1b2c3d4e5f6"
        path = self.registry / job
        (path / "requests").mkdir(parents=True)
        legacy = {
            "id": job, "label": "legacy", "workspace": str(self.path), "log_dir": str(path),
            "prompt_file": str(self.prompt), "command": [str(self.fake), "exec", "--workspace", str(self.path),
            "--provider", "meta", "--max-model-steps", "100", "--prompt-file", str(self.prompt),
            "--session-id", "2c3db702-57c9-46ba-b2a1-f2a3b1e3e09d", "--model", MUSE_MODEL,
            "--reasoning-effort", "high"], "status": "idle", "phase": "ready",
            "started_at": time.time(), "steps": 0, "task_failures": 0, "malformed_events": 0,
            "model": MUSE_MODEL, "reasoning_effort": "high",
            "muse_session_id": "2c3db702-57c9-46ba-b2a1-f2a3b1e3e09d",
            "reusable": True, "turn": 1
        }
        module.save(path / "state.json", legacy)
        module.save(path / "policy.json", {"reasoning_effort": "high", "max_model_steps": 100})
        follow = self.cli("followup", job, "--prompt-file", str(self.prompt),
                          "--reasoning-effort", "medium", "--max-model-steps", "3")
        self.assertEqual(follow.returncode, 0, follow.stderr)
        request = json.loads(next((path / "requests").glob("*.json")).read_text())
        self.assertEqual(set(request), {"prompt_file", "label", "reasoning_effort", "max_model_steps"})
        self.assertEqual(request["reasoning_effort"], "medium")
        effort = self.cli("effort", job, "--reasoning-effort", "xhigh", "--max-model-steps", "9")
        self.assertEqual(effort.returncode, 0, effort.stderr)
        self.assertEqual(json.loads((path / "policy.json").read_text()),
                         {"reasoning_effort": "xhigh", "max_model_steps": 9})
        close = self.cli("close", job)
        self.assertEqual(close.returncode, 0, close.stderr)
        self.assertTrue((path / "close.request").is_file())

    def test_reusable_turns_effort_history_and_graceful_close(self):
        process, initial = self.open_worker()
        job = initial["id"]
        first = self.until_idle(job)
        self.assertIsNone(process.poll())
        self.assertEqual(self.cli("wait", job).returncode, 0)
        self.assertIn("1 idle", self.cli("status-line").stdout)
        self.assertEqual(self.cli("effort", job, "--reasoning-effort", "max", "--max-model-steps", "200").returncode, 0)
        self.assertEqual(self.cli("followup", job, "--prompt-file", str(self.prompt),
                                  "--reasoning-effort", "medium", "--max-model-steps", "3").returncode, 0)
        second = self.until_idle(job, 2)
        self.assertEqual(second["reasoning_effort"], "medium")
        self.assertEqual(second["command"][second["command"].index("--max-model-steps") + 1], "3")
        self.assertEqual(self.cli("followup", job, "--prompt-file", str(self.prompt)).returncode, 0)
        third = self.until_idle(job, 3)
        self.assertEqual(third["reasoning_effort"], "max")
        self.assertEqual(third["command"][third["command"].index("--max-model-steps") + 1], "200")
        self.assertEqual(third["supervisor_pid"], first["supervisor_pid"])
        self.assertEqual(third["session_id"], first["session_id"])
        self.assertEqual(third["command"][third["command"].index("--session-id") + 1], first["session_id"])
        for turn in range(1, 4):
            self.assertEqual((self.registry / job / f"turn-{turn:04}.txt").read_text(), "fixture result")
        self.assertEqual(len((self.registry / job / "events.jsonl").read_text().splitlines()), 6)
        self.assertEqual(self.cli("close", job).returncode, 0)
        process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0)
        self.assertEqual(self.cli("wait", job).returncode, 0)
        self.assertEqual(json.loads(self.cli("show", job, "--json").stdout)["status"], "completed")
        self.assertIsNone(module.identity(third["worker_pid"]))

    def test_effort_change_during_turn_and_close_drains_queue(self):
        process, initial = self.open_worker(mode="brief")
        job = initial["id"]
        self.until_running(job)
        self.assertEqual(self.cli("effort", job, "--reasoning-effort", "max").returncode, 0)
        self.assertEqual(json.loads(self.cli("show", job, "--json").stdout)["reasoning_effort"], "xhigh")
        for _ in range(2):
            self.assertEqual(self.cli("followup", job, "--prompt-file", str(self.prompt)).returncode, 0)
        self.assertEqual(self.cli("close", job).returncode, 0)
        process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0)
        final = json.loads(self.cli("show", job, "--json").stdout)
        self.assertEqual(final["turn"], 3)
        self.assertEqual(final["reasoning_effort"], "max")
        self.assertNotEqual(self.cli("followup", job, "--prompt-file", str(self.prompt)).returncode, 0)

    def test_reusable_failure_and_idle_cancellation(self):
        process, initial = self.open_worker(mode="missing")
        idle = self.until_idle(initial["id"])
        self.assertEqual(idle["last_turn_status"], "failed")
        self.assertNotEqual(self.cli("wait", initial["id"]).returncode, 0)
        self.assertEqual(self.cli("cancel", initial["id"]).returncode, 0)
        process.communicate(timeout=5)
        self.assertEqual(process.returncode, 130)

    def test_reusable_requires_durable_logging(self):
        result = self.cli("open", "--workspace", str(self.path), "--prompt-file", str(self.prompt), "--no-session-log")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("session logging", result.stderr)

    def test_quota_reports_reset_and_requires_fallback_approval(self):
        result, state = self.launch(mode="quota")
        self.assertEqual(result.returncode, 1)
        self.assertTrue(state["quota_exhausted"])
        self.assertTrue(state["fallback_requires_user_approval"])
        self.assertEqual(state["quota_reset_at"], "2099-10-02T14:51:16Z")
        self.assertIn("muse-spark-1.3-contributor-free", result.stdout)
        self.assertIn("never switches automatically", result.stdout)
        self.assertEqual(state["model"], MUSE_MODEL)
        generic = {"task_failures": 0}
        native = {"payload_type": "task.lifecycle.failed",
                  "payload": {"event": {"reason": "API error 429: too many requests"}}}
        for event in module.get_engine("muse").normalize_event(native):
            module.event_update(generic, event)
        self.assertEqual(generic["task_failures"], 1)
        self.assertNotIn("quota_exhausted", generic)
        self.assertEqual(generic["task_failure_reasons"], ["API error 429: too many requests"])
        for number in range(7):
            module.event_update(generic, {"type": "task_warning", "tool": "read", "reason": f"ENOENT\n{number}"})
        self.assertEqual(generic["task_failures"], 8)
        self.assertEqual(generic["task_failure_reasons"], [f"read: ENOENT {n}" for n in range(2, 7)])

    def test_followup_reuses_created_worktree(self):
        worktree = self.path / "isolated tree"
        worktree.mkdir()
        process, initial = self.open_worker(extra=("--worktree", "--worktree-base", "HEAD"),
                                             mode="worktree", env=dict(self.env, MUSE_TEST_WORKTREE=str(worktree)))
        job = initial["id"]
        first = self.until_idle(job)
        self.assertEqual(first["actual_workspace"], str(worktree))
        self.assertEqual(self.cli("followup", job, "--prompt-file", str(self.prompt)).returncode, 0)
        second = self.until_idle(job, 2)
        command = second["command"]
        self.assertEqual(command[command.index("-w") + 1], "existing")
        self.assertEqual(command[command.index("--worktree-existing") + 1], str(worktree))
        self.assertNotIn("--worktree-base", command)
        self.cli("close", job)
        process.communicate(timeout=5)

    def test_reopen_retained_session_in_existing_worktree(self):
        worktree = self.path / "existing worktree"
        worktree.mkdir()
        session = "2c3db702-57c9-46ba-b2a1-f2a3b1e3e09d"
        result, state = self.launch(extra=("--session-id", session, "--worktree-existing", str(worktree)))
        self.assertEqual(result.returncode, 0)
        command = state["command"]
        self.assertEqual(state["session_id"], session)
        self.assertEqual(state["actual_workspace"], str(worktree))
        self.assertEqual(command[command.index("-w") + 1], "existing")
        self.assertNotIn("create", command)

    def test_usage_counts_completions_once_and_unknown_stays_unknown(self):
        def record(kind, **data):
            return {"envelope": {"payload": {"event": dict(kind=kind, **data)}}}
        usage = dict(input_tokens=100, cached_tokens=80, output_tokens=15, reasoning_tokens=10)
        exported = {"events": [record("goal_usage_attribution", usage=usage),
                               record("model_completed", model=MUSE_MODEL, usage=usage),
                               record("model_completed", model=MUSE_MODEL,
                                      usage=dict(input_tokens=50, cached_tokens=0, output_tokens=5))]}
        muse_engine = module.get_engine("muse")
        result = muse_engine.summarize_usage(exported)
        self.assertEqual(result["model_calls"], 2)
        self.assertEqual(result["totals"]["input_tokens"], 150)
        self.assertEqual(result["totals"]["cached_tokens"], 80)
        self.assertAlmostEqual(result["cache_hit_ratio"], 80 / 150)
        self.assertEqual(muse_engine.summarize_usage({"events": []})["totals"], {})
        incomplete = muse_engine.summarize_usage({"events": [record("model_completed", usage={"input_tokens": 10})]})
        self.assertIsNone(incomplete["cache_hit_ratio"])
        self.assertNotIn("cached_tokens", incomplete["totals"])

    def test_historical_inspection_works_without_muse_binary(self):
        result, state = self.launch()
        self.assertEqual(result.returncode, 0)
        path = self.registry / state["id"]
        saved = json.loads((path / "state.json").read_text())
        saved["launch"]["binary"] = str(self.path / "missing-muse")
        module.save(path / "state.json", saved)

        for args in (("list", "--json"), ("show", state["id"], "--json"),
                     ("logs", state["id"]), ("status-line",), ("watch", "--once", "--no-color")):
            checked = self.cli(*args)
            self.assertEqual(checked.returncode, 0, f"{args}: {checked.stderr}")

        usage = self.cli("usage", state["id"])
        self.assertNotEqual(usage.returncode, 0)
        self.assertIn("Muse engine is unavailable", usage.stderr)

    def test_success_and_safe_arguments(self):
        result, state = self.launch(extra=("--read-only", "--worktree", "--worktree-base", "HEAD"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["steps"], 1)
        self.assertIn("--disable-write", state["command"])
        self.assertIn("--disable-shell", state["command"])
        self.assertIn("--disable-approval", state["command"])
        self.assertIn("--disable-web-tools", state["command"])
        self.assertNotIn("--yolo", state["command"])
        self.assertIn(str(self.prompt), state["command"])
        self.assertFalse((self.path / "INJECTED").exists())
        self.assertEqual((self.registry / state["id"] / "result.txt").read_text(), "fixture result")
        self.assertEqual((self.registry / state["id"] / "state.json").stat().st_mode & 0o777, 0o600)

    def test_missing_terminal_is_failure(self):
        result, state = self.launch(mode="missing")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(state["status"], "failed")

    def test_exit_failure_despite_completed_terminal(self):
        result, state = self.launch(mode="exit-failure")
        self.assertEqual(result.returncode, 7)
        self.assertEqual(state["status"], "failed")

    def test_malformed_stream_is_failure(self):
        result, state = self.launch(mode="malformed")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(state["malformed_events"], 1)

    def test_background_and_process_group_cancellation(self):
        result, initial = self.launch(action="start", mode="slow")
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.until_running(initial["id"])
        child = None
        for _ in range(80):
            events = self.registry / state["id"] / "events.jsonl"
            if events.exists():
                for line in events.read_text().splitlines():
                    event = json.loads(line)
                    if event["payload_type"] == "test.child":
                        child = event["payload"]["pid"]
            if child:
                break
            time.sleep(0.025)
        self.assertIsNotNone(child)
        self.assertIn("1 running", self.cli("status-line").stdout)
        self.assertEqual(self.cli("cancel", state["id"]).returncode, 0)
        waited = self.cli("wait", state["id"])
        self.assertEqual(waited.returncode, 130, waited.stdout + waited.stderr)
        self.assertIsNone(module.identity(state["worker_pid"]))
        self.assertIsNone(module.identity(child))

    def test_pid_reuse_does_not_signal_unrelated_process(self):
        result, state = self.launch()
        state.update(status="running", supervisor_pid=os.getpid(),
                     supervisor_identity="different-process")
        path = self.registry / state["id"]
        (path / "state.json").write_text(json.dumps(state))
        shown = self.cli("show", state["id"], "--json")
        self.assertEqual(json.loads(shown.stdout)["status"], "interrupted")
        self.assertNotEqual(self.cli("cancel", state["id"]).returncode, 0)

    def test_multiple_jobs_and_bounded_viewport(self):
        for _ in range(3):
            result = self.cli("run", "--workspace", str(self.path),
                              "--prompt-file", str(self.prompt))
            self.assertEqual(result.returncode, 0)
        listed = json.loads(self.cli("list", "--json").stdout)
        self.assertEqual(len(listed), 3)
        self.assertIn("3 done", self.cli("status-line").stdout)
        viewed = module.render(listed * 10, height=10)
        self.assertLessEqual(len(viewed.splitlines()), 10)
        self.assertIn("more:", viewed)

    def test_compact_view_fits_five_lines(self):
        result, state = self.launch()
        self.assertEqual(result.returncode, 0)
        state["label"] = "A longer task label that remains readable beyond column eighty"
        records = [dict(state, id=f"{index:012x}") for index in range(5)]
        view = module.compact_view(records, width=150, height=5)
        self.assertEqual(len(view.splitlines()), 5)
        self.assertIn(state["label"], view)
        self.assertIn("+2 more", view)
        narrow = module.compact_view(records, width=45, height=5)
        self.assertTrue(all(len(line) <= 45 for line in narrow.splitlines()))

    def test_live_terminal_size_overrides_stale_environment(self):
        with mock.patch.dict(os.environ, {"COLUMNS": "80", "LINES": "24"}):
            with mock.patch.object(module.os, "get_terminal_size", return_value=os.terminal_size((180, 5))):
                self.assertEqual(module.terminal_size(), os.terminal_size((180, 5)))

    def test_concurrent_read_only_workers(self):
        active = []
        for _ in range(3):
            result, initial = self.launch(action="start", mode="slow", extra=("--read-only",))
            self.assertEqual(result.returncode, 0)
            active.append(initial["id"])
        for job in active:
            self.until_running(job)
        self.assertIn("3 running", self.cli("status-line").stdout)
        for job in active:
            self.assertEqual(self.cli("cancel", job).returncode, 0)
        for job in active:
            self.assertEqual(self.cli("wait", job).returncode, 130)
        self.assertIn("0 running", self.cli("status-line").stdout)

    def test_fixture_engine_exercises_generic_lifecycle(self):
        result, state = self.fixture_launch()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(state["engine"], "fixture")
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["steps"], 1)
        self.assertEqual((self.registry / state["id"] / "result.txt").read_text(), "fixture result")

        warning_result, warning = self.fixture_launch(mode="warning")
        self.assertEqual(warning_result.returncode, 0)
        self.assertEqual(warning["status"], "completed")
        self.assertEqual(warning["task_failures"], 1)
        self.assertIn("Task failures reported: 1", warning_result.stdout)
        self.assertIn("  - fixture task warning", warning_result.stdout)

        for mode in ("terminal-failure", "exit-failure", "malformed", "missing"):
            failed_result, failed = self.fixture_launch(mode=mode)
            self.assertNotEqual(failed_result.returncode, 0, mode)
            self.assertEqual(failed["status"], "failed", mode)

        before = set(self.registry.glob("*/state.json"))
        env = dict(self.env, FIXTURE_WORKER_BINARY=FIXTURE_WORKER, FIXTURE_MODE="success")
        process = subprocess.Popen([
            FIXTURE_CLI, "open", "--engine", "fixture", "--workspace", str(self.path),
            "--prompt-file", str(self.prompt), "--label", "fixture-reusable"
        ], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        self.open_processes.append(process)
        reusable = None
        for _ in range(400):
            new = set(self.registry.glob("*/state.json")) - before
            if new:
                reusable = json.loads(next(iter(new)).read_text())
                break
            time.sleep(0.025)
        self.assertIsNotNone(reusable)
        job = reusable["id"]
        self.until_idle(job)
        follow = self.fixture_cli("followup", job, "--prompt-file", str(self.prompt),
                                  "--reasoning-effort", "medium", "--max-model-steps", "3")
        self.assertEqual(follow.returncode, 0, follow.stderr)
        second = self.until_idle(job, 2)
        self.assertEqual(second["reasoning_effort"], "medium")
        self.assertEqual(second["max_model_steps"], 3)
        self.assertEqual(self.fixture_cli("close", job).returncode, 0)
        process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0)

        detached = self.fixture_cli("start", "--engine", "fixture",
                                    "--workspace", str(self.path), "--prompt-file", str(self.prompt),
                                    mode="slow")
        self.assertEqual(detached.returncode, 0, detached.stderr)
        detached_id = re.search(r"Worker ([0-9a-f]{12})", detached.stdout).group(1)
        self.until_running(detached_id)
        cancel = self.fixture_cli("cancel", detached_id, mode="slow")
        self.assertEqual(cancel.returncode, 0, cancel.stderr)
        waited = self.fixture_cli("wait", detached_id, mode="slow", timeout=15)
        self.assertEqual(waited.returncode, 130, waited.stdout + waited.stderr)
        cancelled = json.loads((self.registry / detached_id / "state.json").read_text())
        self.assertEqual(cancelled["status"], "cancelled")

    def test_actual_muse_worktree_characterization(self):
        muse = hermetic.HOST_HOME / ".local/bin/muse"
        if not hermetic.LIVE or not muse.is_file():
            self.skipTest("live Muse check; set CLAIVE_LIVE_TESTS=1 with Muse installed")

        repo = self.path / "worktree-repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        (repo / "file.txt").write_text("one\n")
        subprocess.run(["git", "add", "file.txt"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "first"], cwd=repo, check=True)
        (repo / "file.txt").write_text("two\n")
        subprocess.run(["git", "commit", "-qam", "second"], cwd=repo, check=True)
        base = subprocess.check_output(["git", "rev-parse", "HEAD~1"], cwd=repo, text=True).strip()

        prompt = self.path / "worktree-prompt.md"
        prompt.write_text("echo worktree probe\n")
        session = str(uuid.uuid4())
        stdout_path = self.path / "worktree-out.jsonl"
        stderr_path = self.path / "worktree-err.log"
        command = [
            str(muse), "exec", "--workspace", str(repo), "--trust-workspace",
            "--disable-approval", "--json", "--provider", "echo", "--max-model-steps", "1",
            "--user-input-auto-resolve", "--prompt-file", str(prompt), "--session-id", session,
            "--disable-write", "--disable-shell", "--disable-web-tools",
            "-w", "create", "--worktree-base", base,
        ]
        with stdout_path.open("w") as out, stderr_path.open("w") as err:
            process = subprocess.Popen(command, stdout=out, stderr=err, text=True)
            reported = None
            for _ in range(2000):
                text = stderr_path.read_text(errors="replace")
                match = re.search(r"^muse: workspace root: (.+) \(explicit\)$", text, re.M)
                if match and Path(match.group(1)).is_dir():
                    reported = Path(match.group(1))
                    break
                if process.poll() is not None:
                    break
                time.sleep(0.002)
            self.assertIsNotNone(reported, stderr_path.read_text(errors="replace"))
            self.assertEqual(reported.parent, repo / ".muse/worktrees")
            self.assertTrue((reported / ".git").is_file())
            self.assertEqual(subprocess.check_output(["git", "rev-parse", "HEAD"],
                                                    cwd=reported, text=True).strip(), base)
            self.assertEqual(subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                                                    cwd=reported, text=True).strip(),
                             f"muse/session-{session}")
            listed = subprocess.check_output(["git", "worktree", "list", "--porcelain"],
                                             cwd=repo, text=True)
            self.assertIn(f"worktree {reported}", listed)
            self.assertEqual(process.wait(timeout=15), 0)

        self.assertFalse(reported.exists())
        listed_after = subprocess.check_output(["git", "worktree", "list", "--porcelain"],
                                               cwd=repo, text=True)
        self.assertNotIn(str(reported), listed_after)
        branches = subprocess.check_output(["git", "branch", "--format=%(refname:short)"],
                                           cwd=repo, text=True).splitlines()
        self.assertNotIn(f"muse/session-{session}", branches)

    def test_actual_muse_echo_transport(self):
        muse = hermetic.HOST_HOME / ".local/bin/muse"
        if not hermetic.LIVE or not muse.is_file():
            self.skipTest("live Muse check; set CLAIVE_LIVE_TESTS=1 with Muse installed")
        env = dict(self.env, MUSE_WORKER_BINARY=str(muse))
        result = subprocess.run([CLI, "run", "--workspace", str(self.path),
                                 "--prompt-file", str(self.prompt), "--read-only",
                                 "--provider", "echo", "--max-model-steps", "1",
                                 "--no-session-log"], env=env, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        records = json.loads(subprocess.run([CLI, "list", "--json"], env=env,
                             text=True, capture_output=True, check=True).stdout)
        self.assertEqual(records[0]["terminal"], "completed")
        self.assertIn("echo:", (self.registry / records[0]["id"] / "result.txt").read_text())

    def test_install_dry_run_backups_and_preserved_global(self):
        bin_dir = self.path / "installed-bin"
        codex_home = self.path / "codex-home"
        state_home = self.path / "state-home"
        codex_home.mkdir()
        global_file = codex_home / "AGENTS.md"
        global_file.write_text("Existing personal instructions.\n")
        claude_home = self.path / "claude-home"
        claude_routing = claude_home / "skills/subagent-routing/SKILL.md"
        claude_routing.parent.mkdir(parents=True)
        claude_routing.write_text("Claude's own routing skill.\n")
        env = dict(self.env, CLAIVE_BIN_DIR=str(bin_dir), CODEX_HOME=str(codex_home),
                   CLAUDE_HOME=str(claude_home), XDG_STATE_HOME=str(state_home))
        install = str(REPO_ROOT / "install.sh")
        dry = subprocess.run([install, "--dry-run"], env=env, capture_output=True, text=True)
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertFalse(bin_dir.exists())
        bin_dir.mkdir()
        previous = bin_dir / "claive-worker"
        previous.write_text("previous launcher\n")
        legacy = bin_dir / "muse-worker"
        legacy.write_text('#!/usr/bin/env bash\n# Keep a Muse worker available for follow-ups with visible progress.\n'
                          'set -euo pipefail\nscript_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)\n'
                          'exec "$script_dir/codex-workers" open "$@"\n')
        old_orch = bin_dir / "codex-orch"
        old_orch.write_text("#!/usr/bin/env python3\nfrom codex_workers.orch import main\n")
        unrelated = bin_dir / "codex-workers"
        unrelated.write_text("someone else's tool\n")
        old_package = bin_dir / "codex_workers"
        old_package.mkdir()
        (old_package / "cli.py").write_text("")
        (old_package / "engine.py").write_text("")
        done = subprocess.run([install], env=env, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual((bin_dir / "claive").read_bytes(), Path(CLI).read_bytes())
        self.assertEqual((bin_dir / "claive").stat().st_mode & 0o777, 0o755)
        self.assertEqual((bin_dir / "claivelib/cli.py").read_bytes(),
                         (REPO_ROOT / "bin/claivelib/cli.py").read_bytes())
        outside = self.path / "outside"
        outside.mkdir()
        installed = subprocess.run([str(bin_dir / "claive"), "status-line"], env=env,
                                   cwd=outside, capture_output=True, text=True, timeout=12)
        self.assertEqual(installed.returncode, 0, installed.stderr)
        self.assertIn("Workers:", installed.stdout)
        backups = list((state_home / "claive-install/backups").glob("*/claive-worker"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), "previous launcher\n")
        self.assertFalse(legacy.exists())
        self.assertEqual(len(list((state_home / "claive-install/backups").glob("*/muse-worker"))), 1)
        self.assertFalse(old_orch.exists())
        self.assertFalse(old_package.exists())
        self.assertEqual(unrelated.read_text(), "someone else's tool\n")
        self.assertEqual(len(list((state_home / "claive-install/backups").glob("*/codex-orch"))), 1)
        launcher = subprocess.run([str(bin_dir / "claive-worker"), "--help"], env=env,
                                  cwd=outside, capture_output=True, text=True, timeout=12)
        self.assertEqual(launcher.returncode, 0, launcher.stderr)
        self.assertIn("--engine", launcher.stdout)
        self.assertEqual(global_file.read_text(), "Existing personal instructions.\n")
        self.assertEqual(claude_routing.read_bytes(),
                         (REPO_ROOT / "skills/claude-subagent-routing/SKILL.md").read_bytes())
        saved = list((state_home / "claive-install/backups").glob("*/*subagent-routing.SKILL.md"))
        self.assertEqual([path.read_text() for path in saved], ["Claude's own routing skill.\n"])
        for home in (codex_home, claude_home):
            for skill in ("worker-orchestration", "ttc-experiment"):
                self.assertEqual((home / "skills" / skill / "SKILL.md").read_bytes(),
                                 (REPO_ROOT / "skills" / skill / "SKILL.md").read_bytes())
        arms = subprocess.run([str(bin_dir / "claive-orch"), "arms"], env=env, cwd=outside,
                              capture_output=True, text=True, timeout=12)
        self.assertEqual(arms.returncode, 0, arms.stderr)
        self.assertIn("D", arms.stdout)
        again = subprocess.run([install], env=env, capture_output=True, text=True)
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(len(list((state_home / "claive-install/backups").iterdir())), 1)

        # Historical inspection does not require the default Muse adapter to import.
        (bin_dir / "claivelib/engines/muse.py").unlink()
        no_muse = subprocess.run([str(bin_dir / "claive"), "status-line"], env=env,
                                 cwd=outside, capture_output=True, text=True, timeout=12)
        self.assertEqual(no_muse.returncode, 0, no_muse.stderr)
        self.assertIn("Workers:", no_muse.stdout)

    def test_install_rejects_symlink_destination(self):
        bin_dir = self.path / "installed-bin"
        bin_dir.mkdir()
        protected = self.path / "protected"
        protected.write_text("keep me")
        (bin_dir / "claive").symlink_to(protected)
        env = dict(self.env, CLAIVE_BIN_DIR=str(bin_dir), CODEX_HOME=str(self.path / "codex-home"),
                   CLAUDE_HOME=str(self.path / "claude-home"), XDG_STATE_HOME=str(self.path / "state-home"))
        result = subprocess.run([str(REPO_ROOT / "install.sh")], env=env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(protected.read_text(), "keep me")
        self.assertFalse((bin_dir / "claive-worker").exists())

    def test_tmux_session_with_nondefault_pane_indices(self):
        import shutil
        if not shutil.which("tmux"):
            self.skipTest("tmux is not installed")
        bin_dir = self.path / "bin"
        bin_dir.mkdir()
        codex = bin_dir / "codex"
        codex.write_text("#!/usr/bin/python3\nimport time\ntime.sleep(60)\n")
        codex.chmod(0o700)
        socket_dir = self.path / "tmux"
        socket_dir.mkdir()
        env = dict(self.env, TMUX_TMPDIR=str(socket_dir),
                   PATH=str(bin_dir) + os.pathsep + os.environ["PATH"])
        env.pop("TMUX", None)
        with mock.patch.dict(os.environ, env, clear=True):
            subprocess.run(["tmux", "-f", "/dev/null", "new-session", "-d", "-s", "fixture"],
                           check=True, capture_output=True)
            try:
                subprocess.run(["tmux", "set-option", "-g", "base-index", "1"], check=True)
                subprocess.run(["tmux", "set-option", "-g", "pane-base-index", "1"], check=True)
                with mock.patch.object(module.subprocess, "call", return_value=0):
                    self.assertEqual(module.tmux_view(["--", "--no-alt-screen"]), 0)
                sessions = subprocess.check_output(["tmux", "list-sessions", "-F", "#{session_name}"],
                                                   text=True).splitlines()
                target = next(value for value in sessions if value.startswith("claive-"))
                panes = subprocess.check_output(["tmux", "list-panes", "-t", target,
                                                  "-F", "#{pane_id}"], text=True).splitlines()
                self.assertEqual(len(panes), 2)
                subprocess.run(["tmux", "resize-window", "-t", target, "-x", "180", "-y", "54"], check=True)
                time.sleep(0.1)
                height = subprocess.check_output(["tmux", "display-message", "-p", "-t", panes[1],
                                                  "#{pane_height}"], text=True).strip()
                self.assertEqual(height, "5")
                right = subprocess.check_output(["tmux", "show-option", "-t", target,
                                                  "-v", "status-right"], text=True)
                self.assertIn("claive status-line", right)
                self.assertIn(str(self.registry), right)
                time.sleep(0.3)
                captured = subprocess.check_output(["tmux", "capture-pane", "-p", "-t", panes[1]], text=True)
                self.assertIn("Workers:", captured)
            finally:
                subprocess.run(["tmux", "kill-server"], check=False, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    unittest.main(verbosity=2)
