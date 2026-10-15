"""Session forks stay isolated and inherit the source worker's launch policy."""
import hermetic  # noqa: F401  (must run before claivelib reads the environment)
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "bin"))
from claivelib import cli

FIXTURE_CLI = str(REPO / "tests/fixture-workers")
FIXTURE_WORKER = str(REPO / "tests/fixture_worker.py")


class ForkChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fork-check-")
        self.root = Path(self.temp.name)
        self.registry = self.root / "registry"
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.prompt = self.root / "source.md"
        self.prompt.write_text("source session prompt\n")
        self.env = dict(os.environ, CLAIVE_DIR=str(self.registry), TERM="dumb",
                        FIXTURE_WORKER_BINARY=FIXTURE_WORKER, FIXTURE_MODE="success")
        self.processes = []

    def tearDown(self):
        if self.registry.exists():
            for path in self.registry.glob("*/state.json"):
                state = json.loads(path.read_text())
                if state.get("reusable") and state.get("status") not in {"completed", "failed", "cancelled"}:
                    subprocess.run([FIXTURE_CLI, "close", state["id"]], env=self.env,
                                   capture_output=True, timeout=10)
                pid = state.get("supervisor_pid")
                if pid and cli.identity(pid) == state.get("supervisor_identity"):
                    try:
                        os.killpg(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
        for process in self.processes:
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)
        self.temp.cleanup()

    def command(self, *args, env=None):
        return subprocess.run([FIXTURE_CLI, *args], env=dict(env or self.env), text=True,
                              capture_output=True, timeout=15)

    def state(self, worker_id):
        return json.loads((self.registry / worker_id / "state.json").read_text())

    def until_idle(self, worker_id, turn=1):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            state = self.state(worker_id)
            if state.get("status") == "idle" and state.get("turn") == turn:
                return state
            time.sleep(0.025)
        self.fail(f"worker {worker_id} did not become idle: {self.state(worker_id)}")

    def open_fixture(self, *options):
        before = {path.parent.name for path in self.registry.glob("*/state.json")}
        process = subprocess.Popen(
            [FIXTURE_CLI, "open", "--engine", "fixture", "--workspace", str(self.workspace),
             "--prompt-file", str(self.prompt), *options],
            env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True,
        )
        self.processes.append(process)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            for path in self.registry.glob("*/state.json"):
                if path.parent.name not in before:
                    state = self.until_idle(path.parent.name)
                    return process, state
            if process.poll() is not None:
                self.fail(f"fixture open exited: {process.communicate()}")
            time.sleep(0.025)
        self.fail("fixture reusable worker did not register")

    def test_fork_copies_fixture_session_and_inherits_launch_settings(self):
        source_process, source = self.open_fixture(
            "--label", "source", "--model", "test-model", "--reasoning-effort", "low",
            "--max-model-steps", "9", "--read-only",
        )
        source_session = Path(source["launch"]["session_dir"]) / "session.json"
        original_session = source_session.read_bytes()

        text_fork = self.command("fork", source["id"], "--label", "text-branch", "--prompt", "branch prompt")
        self.assertEqual(text_fork.returncode, 0, text_fork.stderr)
        text_id = re.search(r"Worker ([0-9a-f]{12})", text_fork.stdout).group(1)
        text_state = self.until_idle(text_id)

        prompt_file = self.root / "branch.md"
        prompt_file.write_text("file branch prompt\n")
        file_fork = self.command("fork", source["id"], "--prompt-file", str(prompt_file))
        self.assertEqual(file_fork.returncode, 0, file_fork.stderr)
        file_id = re.search(r"Worker ([0-9a-f]{12})", file_fork.stdout).group(1)
        file_state = self.until_idle(file_id)

        for state, label in ((text_state, "text-branch"), (file_state, "source fork")):
            with self.subTest(label=label):
                self.assertEqual(state["forked_from"], source["id"])
                self.assertEqual(state["label"], label)
                self.assertEqual(state["engine"], source["engine"])
                self.assertEqual(state["model"], source["model"])
                self.assertEqual(state["reasoning_effort"], source["reasoning_effort"])
                self.assertEqual(state["max_model_steps"], source["max_model_steps"])
                self.assertEqual(state["workspace"], source["workspace"])
                self.assertEqual(state["launch"]["read_only"], source["launch"]["read_only"])
                self.assertEqual(state["launch"]["steering"], source["launch"]["steering"])
                self.assertNotEqual(state["session_id"], source["session_id"])

        self.assertEqual(source_session.read_bytes(), original_session)
        for worker_id, expected in ((text_id, "branch prompt"), (file_id, "file branch prompt")):
            state = self.state(worker_id)
            session = Path(state["launch"]["session_dir"]) / "session.json"
            self.assertTrue(session.is_file(), f"{state}\n{(self.registry / worker_id / 'supervisor.log').read_text()}")
            messages = json.loads(session.read_text())["messages"]
            self.assertEqual(messages[0], "source session prompt")
            self.assertEqual(messages[-1], expected)
        self.assertIsNone(source_process.poll())

    def test_running_source_is_refused_without_creating_a_fork(self):
        _process, source = self.open_fixture()
        state_path = self.registry / source["id"] / "state.json"
        source_state = json.loads(state_path.read_text())
        source_state["status"] = "running"
        state_path.write_text(json.dumps(source_state))
        before = set(self.registry.glob("*/state.json"))

        result = self.command("fork", source["id"], "--prompt", "branch prompt")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"worker {source['id']} is running; fork works only on an idle worker", result.stderr)
        self.assertEqual(set(self.registry.glob("*/state.json")), before)

    def test_muse_refuses_without_starting_a_worker(self):
        source_id = "123456789abc"
        source_dir = self.registry / source_id
        source_dir.mkdir(parents=True)
        (source_dir / "requests").mkdir()
        (source_dir / "policy.json").write_text(json.dumps({"reasoning_effort": "xhigh", "max_model_steps": 100}))
        (source_dir / "state.json").write_text(json.dumps({
            "schema_version": 2, "id": source_id, "engine": "muse", "session_id": "muse-session",
            "status": "idle", "reusable": True, "turn": 1, "started_at": time.time(),
            "workspace": str(self.workspace), "label": "muse-source", "reasoning_effort": "xhigh",
            "max_model_steps": 100,
            "launch": {"binary": self.env.get("MUSE_WORKER_BINARY", "/usr/bin/muse"),
                       "model": "muse-spark-1.3-contributor", "provider": "meta",
                       "session_logging": True, "read_only": False, "steering": False,
                       "isolation": {"mode": "none"}},
        }))

        result = self.command("fork", source_id, "--prompt", "branch prompt")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("engine muse cannot fork a session; start a new worker", result.stderr)
        self.assertEqual(set(self.registry.glob("*/state.json")), {source_dir / "state.json"})

    def test_fork_rechecks_disabled_model_and_effort_policy(self):
        _process, source = self.open_fixture("--model", "test-model", "--reasoning-effort", "low")
        config_path = self.root / "config.json"
        self.env["CLAIVE_CONFIG"] = str(config_path)
        config_path.write_text(json.dumps({"engines": {"fixture": {
            "disabled_models": {"test-model": "paused"},
        }}}))

        disabled_model = self.command("fork", source["id"], "--prompt", "branch prompt")
        self.assertNotEqual(disabled_model.returncode, 0)
        self.assertIn("model test-model on worker engine fixture is explicitly disabled", disabled_model.stderr)

        config_path.write_text(json.dumps({"engines": {"fixture": {
            "disabled_reasoning_efforts": {"low": "paused"},
        }}}))
        disabled_effort = self.command("fork", source["id"], "--prompt", "branch prompt")
        self.assertNotEqual(disabled_effort.returncode, 0)
        self.assertIn("reasoning effort low on worker engine fixture is explicitly disabled", disabled_effort.stderr)

    def test_fork_rechecks_the_global_model_policy(self):
        _process, source = self.open_fixture("--model", "test-model")
        state_path = self.registry / source["id"] / "state.json"
        state = json.loads(state_path.read_text())
        state["model"] = "nemotron-test"
        state["launch"]["model"] = "nemotron-test"
        state_path.write_text(json.dumps(state))

        result = self.command("fork", source["id"], "--prompt", "branch prompt")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("model nemotron-test is disallowed by claive policy", result.stderr)


class ClaudeBudgetForkChecks(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="claude-budget-fork-")
        self.addCleanup(temp.cleanup)
        self.workspace = Path(temp.name)
        self.config = self.workspace / "config.json"
        env = patch.dict(os.environ, CLAIVE_DIR=str(self.workspace / "registry"),
                         CLAIVE_CONFIG=str(self.config), CLAUDE_WORKER_BINARY=FIXTURE_WORKER,
                         CLAUDE_CONFIG_DIR=str(self.workspace / "claude-config"))
        env.start()
        self.addCleanup(env.stop)
        self.prompt = self.workspace / "source.md"
        self.prompt.write_text("source assignment")

    def source(self):
        args = cli.parser().parse_args(["open", "--engine", "claude", "--workspace", str(self.workspace),
                                        "--prompt-file", str(self.prompt)])
        path, state = cli.create_job(args)
        state["status"] = "idle"
        cli.save(path / "state.json", state)
        return path, state

    def fork(self, source):
        args = cli.parser().parse_args(["fork", source["id"], "--prompt", "branch assignment"])
        return cli.create_fork(args)[1]

    def test_fork_inherits_budget_after_config_disables_it(self):
        self.config.write_text(json.dumps({"engines": {"claude": {"budget": {"models": {
            "claude-haiku-5-5": {"warn": 90000, "stop": 100000, "max_handoffs": 2}}}}}}))
        path, source = self.source()
        original = (path / "state.json").read_bytes()
        self.config.write_text(json.dumps({"engines": {"claude": {"budget": False}}}))
        fork = self.fork(source)
        self.assertEqual(fork["launch"]["budget"], source["launch"]["budget"])
        self.assertEqual(fork["launch"]["budget_off_reason"], source["launch"]["budget_off_reason"])
        self.assertTrue(fork["launch"]["steering"])
        self.assertEqual(fork["launch"]["budget"]["stop"], 100000)
        self.assertEqual(fork["command"][fork["command"].index("--autocompact") + 1], "143k")
        self.assertEqual(fork["command"][fork["command"].index("--resume") + 1], source["session_id"])
        self.assertIn("--fork-session", fork["command"])
        self.assertNotEqual(fork["session_id"], source["session_id"])
        self.assertEqual((path / "state.json").read_bytes(), original)

    def test_fork_inherits_disabled_budget_and_reason_after_config_enables_it(self):
        self.config.write_text(json.dumps({"engines": {"claude": {"budget": False}}}))
        _path, source = self.source()
        self.config.write_text(json.dumps({"engines": {"claude": {"budget": True}}}))
        fork = self.fork(source)
        self.assertIsNone(fork["launch"]["budget"])
        self.assertEqual(fork["launch"]["budget_off_reason"], "config")
        self.assertFalse(fork["launch"]["steering"])

    def test_fork_of_pre_budget_worker_resolves_current_config(self):
        path, source = self.source()
        source["launch"].pop("budget")
        source["launch"].pop("budget_off_reason")
        source["launch"]["steering"] = False
        cli.save(path / "state.json", source)
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                self.config.write_text(json.dumps({"engines": {"claude": {"budget": enabled}}}))
                fork = self.fork(source)
                if enabled:
                    self.assertEqual(fork["launch"]["budget"]["stop"], 95000)
                    self.assertIsNone(fork["launch"]["budget_off_reason"])
                else:
                    self.assertIsNone(fork["launch"]["budget"])
                    self.assertEqual(fork["launch"]["budget_off_reason"], "config")
                self.assertEqual(fork["launch"]["steering"], enabled)


if __name__ == "__main__":
    unittest.main()
