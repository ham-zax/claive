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

from test_workers import FIXTURE_CLI, FIXTURE_WORKER, module


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


if __name__ == "__main__":
    unittest.main()
