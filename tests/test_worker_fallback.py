"""Acceptance tests: per-turn timeout, fallback models and the failure reason in list."""
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

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_CLI = str(REPO_ROOT / "tests/fixture-workers")
FIXTURE_WORKER = str(REPO_ROOT / "tests/fixture_worker.py")


class WorkerFallback(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="workers-fallback-")
        self.path = Path(self.temp.name)
        self.registry = self.path / "registry"
        self.prompt = self.path / "task.md"
        self.prompt.write_text("A narrow fixture task.")
        self.env = dict(os.environ, CLAIVE_DIR=str(self.registry), FIXTURE_WORKER_BINARY=FIXTURE_WORKER,
                        FIXTURE_MODE="success")

    def tearDown(self):
        if self.registry.exists():
            for folder in self.registry.iterdir():
                state_file = folder / "state.json"
                if not state_file.exists():
                    continue
                state = json.loads(state_file.read_text())
                for field in ("worker_pid", "supervisor_pid"):
                    if state.get(field) and state.get("status") in ("running", "cancelling", "starting"):
                        try:
                            os.killpg(state[field], signal.SIGKILL)
                        except (ProcessLookupError, PermissionError):
                            pass
        self.temp.cleanup()

    def cli(self, *args, modes=None, timeout=40):
        env = dict(self.env)
        if modes:
            env["FIXTURE_MODEL_MODES"] = json.dumps(modes)
        return subprocess.run([FIXTURE_CLI, *args], env=env, text=True, capture_output=True, timeout=timeout)

    def launch(self, *extra, action="run", modes=None, timeout=40):
        result = self.cli(action, "--engine", "fixture", "--workspace", str(self.path),
                          "--prompt-file", str(self.prompt), "--label", "fallback", *extra,
                          modes=modes, timeout=timeout)
        match = re.search(r"Worker ([0-9a-f]{12})", result.stdout)
        return result, match and match.group(1)

    def state(self, worker_id):
        return json.loads((self.registry / worker_id / "state.json").read_text())

    def worker_count(self):
        if not self.registry.exists():
            return 0
        return sum(1 for folder in self.registry.iterdir() if (folder / "state.json").exists())

    # ------------------------------------------------------------------ timeout

    def test_turn_timeout_fails_with_timeout_kind(self):
        started = time.monotonic()
        result, worker_id = self.launch("--model", "m1", "--turn-timeout", "1", modes={"m1": "slow"})
        self.assertNotEqual(result.returncode, 0)
        self.assertLess(time.monotonic() - started, 20)
        state = self.state(worker_id)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["failure_kind"], "timeout")
        self.assertIn("turn timed out after 1s", state["error"])
        self.assertEqual(state["turn_timeout"], 1)

    def test_turn_timeout_must_be_positive(self):
        for value in ("0", "-2", "soon"):
            result, _ = self.launch("--turn-timeout", value)
            self.assertNotEqual(result.returncode, 0, value)
        self.assertEqual(self.worker_count(), 0)

    def test_turn_timeout_not_hit_completes(self):
        result, worker_id = self.launch("--model", "m1", "--turn-timeout", "30")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.state(worker_id)["status"], "completed")

    # ----------------------------------------------------------------- fallback

    def test_fallback_after_worker_failure(self):
        result, worker_id = self.launch("--model", "m1", "--fallback-models", "m2,m3",
                                        modes={"m1": "terminal-failure"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        state = self.state(worker_id)
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["model"], "m2")
        self.assertEqual(state["fallback_models"], ["m2", "m3"])
        self.assertEqual([f["model"] for f in state["fallbacks"]], ["m1"])
        self.assertEqual(state["fallbacks"][0]["failure_kind"], "worker")
        self.assertIn("--model", state["command"])
        self.assertEqual(state["command"][state["command"].index("--model") + 1], "m2")
        self.assertEqual((self.registry / worker_id / "result.txt").read_text(), "fixture result")
        events = (self.registry / worker_id / "events.jsonl").read_text()
        self.assertIn("fixture terminal failure", events)
        shown = self.cli("show", worker_id).stdout
        self.assertIn("Fallback from m1: worker", shown)

    def test_timeout_then_fallback(self):
        result, worker_id = self.launch("--model", "m1", "--turn-timeout", "1", "--fallback-models", "m2",
                                        modes={"m1": "slow"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        state = self.state(worker_id)
        self.assertEqual((state["status"], state["model"]), ("completed", "m2"))
        self.assertEqual(state["fallbacks"][0]["failure_kind"], "timeout")
        self.assertIn("timed out", state["fallbacks"][0]["error"])

    def test_all_models_fail(self):
        result, worker_id = self.launch("--model", "m1", "--fallback-models", "m2",
                                        modes={"m1": "terminal-failure", "m2": "exit-failure"})
        self.assertNotEqual(result.returncode, 0)
        state = self.state(worker_id)
        self.assertEqual((state["status"], state["model"]), ("failed", "m2"))
        self.assertEqual([f["model"] for f in state["fallbacks"]], ["m1"])
        self.assertIn(state["failure_kind"], ("worker", "protocol"))

    def test_fallback_with_start_and_wait(self):
        result, worker_id = self.launch("--model", "m1", "--fallback-models", "m2", action="start",
                                        modes={"m1": "exit-failure"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        waited = self.cli("wait", worker_id, modes={"m1": "exit-failure"})
        self.assertEqual(waited.returncode, 0, waited.stdout + waited.stderr)
        self.assertEqual(self.state(worker_id)["model"], "m2")

    def test_no_fallback_on_success(self):
        result, worker_id = self.launch("--model", "m1", "--fallback-models", "m2")
        self.assertEqual(result.returncode, 0)
        state = self.state(worker_id)
        self.assertEqual(state["model"], "m1")
        self.assertFalse(state.get("fallbacks"))

    def test_fallback_refused_for_open(self):
        result, _ = self.launch("--model", "m1", "--fallback-models", "m2", action="open")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--fallback-models", result.stderr)
        self.assertEqual(self.worker_count(), 0)

    def test_disallowed_fallback_model_refused(self):
        result, _ = self.launch("--model", "m1", "--fallback-models", "m2,nemotron-3-super-free")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("nemotron", result.stderr)
        self.assertEqual(self.worker_count(), 0)

    # ------------------------------------------------------------- list reason

    def test_list_shows_last_task_failure_reason(self):
        result, worker_id = self.launch("--model", "m1", modes={"m1": "warning"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        listing = self.cli("list").stdout
        self.assertIn("[!1 task failures: fixture task warning]", listing)


if __name__ == "__main__":
    unittest.main()
