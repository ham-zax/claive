import hermetic  # noqa: F401  (must run before claivelib reads the environment)
"""claive goal + claive serve: the supervised queue that replaces a parent session on a server."""
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from test_workers import FIXTURE_CLI, FIXTURE_WORKER, module
from claivelib import serve


class ServeChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="claive-serve-")
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        self.config = self.root / "config.json"
        self.env = dict(os.environ, CLAIVE_DIR=str(self.state), CLAIVE_CONFIG=str(self.config),
                        FIXTURE_WORKER_BINARY=FIXTURE_WORKER, FIXTURE_MODE="success", TERM="dumb")
        self.servers = []

    def tearDown(self):
        for process in self.servers:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
        for folder in self.state.iterdir() if self.state.exists() else ():
            if (folder / "state.json").exists():
                state = json.loads((folder / "state.json").read_text())
                for field in ("worker", "supervisor"):
                    pid = state.get(field + "_pid")
                    if pid and module.identity(pid) == state.get(field + "_identity"):
                        try:
                            os.killpg(pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
        self.temp.cleanup()

    def claive(self, *args):
        return subprocess.run([FIXTURE_CLI, *args], env=self.env, text=True, capture_output=True, timeout=60)

    def add(self, title, mode="success", *extra):
        prompt = self.root / f"{title}.md"
        prompt.write_text(f"goal {title}\nFIXTURE_MODE={mode}\n")
        result = self.claive("goal", "add", "--title", title, "--prompt-file", str(prompt),
                             "--workspace", str(self.workspace), "--engine", "fixture", *extra)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.split()[1]

    def goal(self, goal_id):
        return json.loads((self.state / "goals" / goal_id / "goal.json").read_text())

    def worker(self, job_id):
        return json.loads((self.state / job_id / "state.json").read_text())

    def until(self, goal_id, predicate, what, serve=True, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if serve:
                self.claive("serve", "--once")
            goal = self.goal(goal_id)
            if predicate(goal):
                return goal
            time.sleep(0.3)
        self.fail(f"timed out waiting for {what}: {goal}")

    def test_goals_run_read_only_park_on_questions_and_resume_on_answer(self):
        done = self.add("plain")
        asking = self.add("asking", "ask")
        # A goal result is saved before its asynchronous close request is drained.
        goal = self.until(done, lambda g: g["status"] == "done" and
                          self.worker(g["worker"])["status"] == "completed", "first goal and worker closure")
        self.assertTrue(self.worker(goal["worker"])["launch"]["read_only"])
        self.assertEqual(self.worker(goal["worker"])["status"], "completed")  # closed after its turn
        parked = self.until(asking, lambda g: g["status"] == "parked", "question")
        self.assertEqual(parked["question"], "Design A or B?")
        listing = self.claive("goal", "list").stdout
        self.assertIn("question: Design A or B?", listing)
        self.assertNotIn(done, listing)  # finished goals only with --all
        inbox = self.claive("inbox", "--consumer", "test").stdout
        self.assertIn(f"goal {asking} ASK", inbox)
        self.assertIn(f"goal {done} DONE", inbox)

        refused = self.claive("goal", "answer", done, "--message", "x")
        self.assertNotEqual(refused.returncode, 0)
        self.assertEqual(self.claive("goal", "answer", asking, "--message", "B").returncode, 0)
        self.assertEqual(self.goal(asking)["status"], "running")
        goal = self.until(asking, lambda g: g["status"] == "done", "answered goal")
        self.assertEqual(goal["worker"], parked["worker"])  # same worker and session
        self.assertEqual(self.worker(goal["worker"])["turn"], 2)
        log = (self.state / "supervisor/serve.log").read_text()
        self.assertIn(f"goal {asking}: parked", log)

    def test_write_goals_and_the_workspace_allowlist(self):
        self.config.write_text(json.dumps({"workspaces": [{"path": str(self.workspace), "write": False}]}))
        prompt = self.root / "p.md"
        prompt.write_text("x")
        refused = self.claive("goal", "add", "--title", "w", "--prompt-file", str(prompt),
                              "--workspace", str(self.workspace), "--engine", "fixture", "--write")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("read-only in the claive config workspaces allowlist", refused.stderr)
        outside = self.root / "outside"
        outside.mkdir()
        refused = self.claive("goal", "add", "--title", "o", "--prompt-file", str(prompt),
                              "--workspace", str(outside), "--engine", "fixture")
        self.assertIn("outside the claive config workspaces allowlist", refused.stderr)

        # Tightening the allowlist after a goal was queued fails it at launch without retries.
        self.config.write_text("{}")
        goal_id = self.add("writer", "success", "--write")
        self.config.write_text(json.dumps({"workspaces": [{"path": str(outside), "write": True}]}))
        goal = self.until(goal_id, lambda g: g["status"] == "failed", "refused launch")
        self.assertIn("refused:", goal["error"])
        self.assertEqual(goal["attempts"], 1)
        self.config.write_text("{}")
        self.assertEqual(self.claive("goal", "retry", goal_id).returncode, 0)
        goal = self.until(goal_id, lambda g: g["status"] == "done", "writable goal")
        self.assertFalse(self.worker(goal["worker"])["launch"]["read_only"])

    def test_workers_cannot_queue_goals(self):
        prompt = self.root / "p.md"
        prompt.write_text("x")
        self.env["CLAIVE_WORKER_ID"] = "abc"
        refused = self.claive("goal", "add", "--title", "n", "--prompt-file", str(prompt),
                              "--workspace", str(self.workspace), "--engine", "fixture")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("may not launch workers", refused.stderr)

    def test_budget_cancels_a_goal_that_runs_too_long(self):
        goal_id = self.add("slow", "slow", "--budget", "1")
        goal = self.until(goal_id, lambda g: g["status"] == "timed_out", "budget")
        self.assertIn("budget 1s exceeded", goal["error"])
        self.assertEqual(self.worker(goal["worker"])["status"], "cancelled")
        self.assertIn(f"goal {goal_id} TIMED_OUT", self.claive("inbox", "--consumer", "t").stdout)

    def test_provider_failures_back_off_and_goals_fail_after_their_attempts(self):
        first = self.add("broken", "malformed", "--max-attempts", "2")
        second = self.add("waiting")
        goal = self.until(first, lambda g: g["status"] == "queued" and g["attempts"] == 1, "first failure")
        self.assertIn("protocol:", goal["error"])
        backoff = json.loads((self.state / "supervisor/state.json").read_text())
        self.assertEqual(backoff["level"], 1)
        self.assertGreater(backoff["until"], time.time() + 20)
        self.claive("serve", "--once")
        self.assertEqual(self.goal(second)["status"], "queued")  # launches paused for every goal
        self.assertIn("backoff 3", self.claive("serve", "--status").stdout)

        for goal_id in (first,):  # expire both backoffs instead of sleeping through them
            path = self.state / "goals" / goal_id / "goal.json"
            data = json.loads(path.read_text())
            path.write_text(json.dumps(dict(data, not_before=0)))
        (self.state / "supervisor/state.json").write_text(json.dumps(dict(backoff, until=0)))
        goal = self.until(first, lambda g: g["status"] == "failed", "attempt limit")
        self.assertEqual(goal["attempts"], 2)
        (self.state / "supervisor/state.json").write_text(json.dumps(dict(level=2, until=0)))
        self.until(second, lambda g: g["status"] == "done", "next goal")
        self.assertEqual(json.loads((self.state / "supervisor/state.json").read_text())["level"], 0)

    def test_stop_switch_cancels_workers_and_answers_resume_the_session(self):
        slow = self.add("slow", "slow")
        asking = self.add("asking", "ask")
        server = subprocess.Popen([FIXTURE_CLI, "serve", "--interval", "0.3", "--max-parallel", "2"],
                                  env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.servers.append(server)
        self.until(slow, lambda g: g["status"] == "running", "slow running", serve=False)
        parked = self.until(asking, lambda g: g["status"] == "parked", "parked", serve=False)
        second = self.claive("serve", "--once")
        self.assertIn("another claive serve is running", second.stderr)

        running = self.goal(slow)["worker"]
        self.assertEqual(self.claive("serve", "--stop").returncode, 0)
        self.assertEqual(server.wait(timeout=20), 0)
        self.assertEqual(self.worker(running)["status"], "cancelled")
        self.assertEqual(self.worker(parked["worker"])["status"], "cancelled")
        goal = self.goal(slow)
        self.assertEqual((goal["status"], goal["worker"]), ("queued", None))
        self.assertEqual(self.goal(asking)["status"], "parked")
        self.assertIn("stop switch", (self.state / "supervisor/serve.log").read_text())
        self.claive("serve", "--once")
        self.assertEqual(self.goal(slow)["status"], "queued")  # nothing launches while the switch is set

        # Answering a goal whose worker was stopped resumes its session in a new worker.
        self.assertEqual(self.claive("goal", "cancel", slow).returncode, 0)
        self.assertEqual(self.claive("goal", "answer", asking, "--message", "B").returncode, 0)
        self.assertEqual(self.claive("serve", "--resume").returncode, 0)
        goal = self.until(asking, lambda g: g["status"] == "done", "resumed goal")
        self.assertNotEqual(goal["worker"], parked["worker"])
        self.assertEqual(self.worker(goal["worker"])["session_id"], self.worker(parked["worker"])["session_id"])
        self.assertEqual(self.goal(slow)["status"], "cancelled")

    def test_restart_leaves_workers_running_and_the_next_serve_adopts_them(self):
        goal_id = self.add("brief", "slow", "--budget", "2")
        server = subprocess.Popen([FIXTURE_CLI, "serve", "--interval", "0.3"], env=self.env, text=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.servers.append(server)
        goal = self.until(goal_id, lambda g: g["status"] == "running", "running", serve=False)
        server.send_signal(signal.SIGTERM)
        self.assertEqual(server.wait(timeout=10), 0)
        self.assertEqual(self.worker(goal["worker"])["status"], "running")  # survived serve
        self.assertIn("left running", (self.state / "supervisor/serve.log").read_text())
        goal = self.until(goal_id, lambda g: g["status"] == "timed_out", "adopted goal")
        self.assertEqual(self.worker(goal["worker"])["status"], "cancelled")


    def test_a_dead_supervisor_is_not_retried_while_its_worker_still_runs(self):
        goal_id = self.add("orphan", "slow", "--max-attempts", "2")
        goal = self.until(goal_id, lambda g: g["status"] == "running" and g.get("worker"), "running")
        self.until(goal_id, lambda g: self.worker(g["worker"]).get("worker_pid"), "worker pid", serve=False)
        worker = self.worker(goal["worker"])
        os.kill(worker["supervisor_pid"], signal.SIGKILL)  # the worker process itself survives
        goal = self.until(goal_id, lambda g: g["status"] != "running", "orphan handled")
        self.assertIsNone(module.identity(worker["worker_pid"]), "orphaned worker must be killed before a retry")
        self.assertIn("orphaned worker", json.dumps(goal))


class ServeInstallRestartChecks(unittest.TestCase):
    def test_fingerprint_errors_are_logged_once_and_skipped(self):
        temp = tempfile.TemporaryDirectory(prefix="claive-install-error-")
        self.addCleanup(temp.cleanup)
        supervisor = Path(temp.name) / "supervisor"
        supervisor.mkdir()
        fingerprint = mock.Mock(side_effect=[OSError("install gap"), ValueError("partial tree"),
                                             {"serve.py": "stable"}])
        with mock.patch.object(serve, "supervisor_dir", return_value=supervisor):
            server = serve.Server(fingerprint_fn=fingerprint)
        server.log = mock.Mock()
        clock = [0.0]
        failures = []

        def attempt(action):
            try:
                action()
            except (OSError, ValueError) as error:
                failures.append(error)

        with (mock.patch.object(serve.time, "monotonic", side_effect=lambda: clock[0]),
              mock.patch.object(serve, "all_goals", return_value=[]),
              mock.patch.object(serve.os, "execv") as execv):
            attempt(server.capture_install_fingerprint)
            clock[0] = 30
            attempt(server.check_install_fingerprint)
            clock[0] = 60
            attempt(server.check_install_fingerprint)

        self.assertEqual(failures, [])
        self.assertEqual(fingerprint.call_count, 3)
        server.log.assert_called_once()
        self.assertIn("install fingerprint", server.log.call_args.args[0])
        self.assertEqual(server.install_fingerprint, {"serve.py": "stable"})
        execv.assert_not_called()

    def test_restart_waits_for_two_consecutive_matching_new_fingerprints(self):
        temp = tempfile.TemporaryDirectory(prefix="claive-install-stable-")
        self.addCleanup(temp.cleanup)
        supervisor = Path(temp.name) / "supervisor"
        supervisor.mkdir()
        fingerprint = mock.Mock(side_effect=[{"serve.py": "old"}, {"serve.py": "new-a"},
                                             {"serve.py": "new-b"}, {"serve.py": "new-b"}])
        with mock.patch.object(serve, "supervisor_dir", return_value=supervisor):
            server = serve.Server(fingerprint_fn=fingerprint)
        clock = [0.0]
        with (mock.patch.object(serve.time, "monotonic", side_effect=lambda: clock[0]),
              mock.patch.object(serve, "all_goals", return_value=[]),
              mock.patch.object(serve.os, "execv") as execv,
              mock.patch("builtins.print")):
            server.capture_install_fingerprint()
            for instant in (30, 60):
                clock[0] = instant
                server.check_install_fingerprint()
                execv.assert_not_called()
            clock[0] = 90
            server.check_install_fingerprint()

        execv.assert_called_once()

    def test_default_fingerprint_uses_the_running_package_directory(self):
        package = Path("/runtime/claivelib")
        expected = {"serve.py": "sha256"}
        with (mock.patch.object(serve.runtime, "package_dir", return_value=package),
              mock.patch.object(serve.runtime, "fingerprint_tree", return_value=expected) as tree):
            self.assertEqual(serve.install_fingerprint(), expected)
        tree.assert_called_once_with(package)

    def run_until_install_check(self, fingerprints, goals, goals_after_first_read=None, stop_at=30.5):
        temp = tempfile.TemporaryDirectory(prefix="claive-install-watch-")
        self.addCleanup(temp.cleanup)
        supervisor = Path(temp.name) / "supervisor"
        supervisor.mkdir()
        stop = supervisor / "stop"
        clock = [0.0]
        fingerprint_times = []

        def fingerprint():
            if fingerprint_times:
                fingerprint_times.append(clock[0])
            else:
                fingerprint_times.append("startup")
            return next(fingerprints)

        with mock.patch.object(serve, "supervisor_dir", return_value=supervisor):
            server = serve.Server(interval=120, fingerprint_fn=fingerprint)

        def sleep(seconds):
            clock[0] += seconds
            if clock[0] >= stop_at:
                server.terminating = True

        opened = []
        real_open = open

        def tracked_open(*args, **kwargs):
            stream = real_open(*args, **kwargs)
            opened.append(stream)
            return stream

        goal_reads = 0

        def goal_states():
            nonlocal goal_reads
            goal_reads += 1
            if goal_reads > 1 and goals_after_first_read is not None:
                return goals_after_first_read
            return goals

        argv = ["claive", "serve", "--interval", "120"]
        python = "/usr/bin/python3"
        launcher = "/worktree/bin/claive"
        with (mock.patch.object(serve, "supervisor_dir", return_value=supervisor),
              mock.patch.object(serve, "stop_file", return_value=stop),
              mock.patch.object(server, "tick", return_value=True),
              mock.patch.object(server, "log"),
              mock.patch.object(serve, "all_goals", side_effect=goal_states),
              mock.patch.object(serve.signal, "signal"),
              mock.patch.object(serve.time, "monotonic", side_effect=lambda: clock[0]),
              mock.patch.object(serve.time, "sleep", side_effect=sleep),
              mock.patch.object(serve.sys, "argv", argv),
              mock.patch.object(serve.sys, "executable", python),
              mock.patch.object(serve.os.path, "abspath", return_value=launcher),
              mock.patch.object(serve.os, "execv") as execv,
              mock.patch("builtins.open", side_effect=tracked_open),
              mock.patch("builtins.print") as output):
            def restart(path, args):
                server.terminating = True

            execv.side_effect = restart
            result = server.run()
        for stream in opened:
            stream.close()
        return result, execv, output, argv, fingerprint_times, python, launcher

    def test_changed_install_restarts_python_with_absolute_launcher_and_same_arguments(self):
        result, execv, output, argv, fingerprint_times, python, launcher = self.run_until_install_check(
            iter(({"serve.py": "old"}, {"serve.py": "new"}, {"serve.py": "new"})), [], stop_at=60.5)

        self.assertEqual(result, 0)
        execv.assert_called_once_with(python, [python, launcher, *argv[1:]])
        output.assert_called_once_with("claive serve: install changed; restarting", flush=True)
        self.assertGreaterEqual(fingerprint_times[1], 30)
        self.assertGreaterEqual(fingerprint_times[2], 60)

    def test_install_change_is_not_checked_while_a_goal_runs(self):
        running = [{"status": "running", "worker": "abc123"}]
        result, execv, _output, _argv, fingerprint_times, _python, _launcher = self.run_until_install_check(
            iter(({"serve.py": "old"}, {"serve.py": "new"})), running)

        self.assertEqual(result, 0)
        execv.assert_not_called()
        self.assertEqual(fingerprint_times, ["startup"])

    def test_goal_starting_during_fingerprint_check_prevents_restart(self):
        running = [{"status": "running", "worker": "abc123"}]
        result, execv, output, _argv, _fingerprint_times, _python, _launcher = self.run_until_install_check(
            iter(({"serve.py": "old"}, {"serve.py": "new"})), [], running)

        self.assertEqual(result, 0)
        execv.assert_not_called()
        output.assert_not_called()

    def test_unchanged_install_does_not_restart(self):
        result, execv, output, _argv, fingerprint_times, _python, _launcher = self.run_until_install_check(
            iter(({"serve.py": "same"}, {"serve.py": "same"})), [])

        self.assertEqual(result, 0)
        execv.assert_not_called()
        output.assert_not_called()
        self.assertEqual(len(fingerprint_times), 2)
        if len(fingerprint_times) > 1:
            self.assertGreaterEqual(fingerprint_times[1], 30)

    def test_each_serve_tick_runs_due_monitors(self):
        temporary = tempfile.TemporaryDirectory(prefix="claive-serve-monitors-")
        self.addCleanup(temporary.cleanup)
        supervisor = Path(temporary.name) / "supervisor"
        supervisor.mkdir()
        stop = supervisor / "stop"
        with (mock.patch.object(serve, "supervisor_dir", return_value=supervisor),
              mock.patch.object(serve, "stop_file", return_value=stop),
              mock.patch.object(serve, "all_goals", return_value=[]),
              mock.patch.object(serve.cli_mod, "run_due_monitors") as run_due):
            server = serve.Server()
            with mock.patch.object(server, "launch_ready"):
                self.assertTrue(server.tick())
        run_due.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
