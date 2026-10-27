"""Checks for unconfirmed supervisor starts and launch limits."""
import hermetic  # noqa: F401
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from test_workers import FIXTURE_CLI, FIXTURE_WORKER, module
from claivelib import acceptance, config


class StartRobustnessChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="claive-start-")
        self.base = Path(self.temp.name)
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        self.registry = self.base / "registry"
        self.prompt = self.base / "task.md"
        self.prompt.write_text("fixture task")
        self.config = self.base / "config.json"
        self.config.write_text(json.dumps({"launch": {"start_timeout": 1}}))
        self.startup = self.base / "startup"
        self.startup.mkdir()
        self.env = dict(os.environ, CLAIVE_DIR=str(self.registry), CLAIVE_CONFIG=str(self.config),
                        FIXTURE_WORKER_BINARY=FIXTURE_WORKER, FIXTURE_MODE="success",
                        PYTHONPATH=str(self.startup))

    def tearDown(self):
        for path in self.registry.glob("*/state.json"):
            state = json.loads(path.read_text())
            for name in ("worker", "supervisor"):
                pid = state.get(name + "_pid")
                if pid and module.identity(pid) == state.get(name + "_identity"):
                    try:
                        os.killpg(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            spawn_path = path.parent / "spawn.json"
            if spawn_path.exists():
                spawn = json.loads(spawn_path.read_text())
                if module.identity(spawn["pid"]) == spawn["identity"] and spawn["identity"]:
                    try:
                        os.killpg(spawn["pid"], signal.SIGKILL)
                    except ProcessLookupError:
                        pass
        self.temp.cleanup()

    def cli(self, *args):
        return subprocess.run([FIXTURE_CLI, *args], env=self.env, text=True,
                              capture_output=True, timeout=15)

    def start(self, workspace=None, *extra):
        return self.cli("start", "--engine", "fixture", "--workspace", str(workspace or self.workspace),
                        "--prompt-file", str(self.prompt), *extra)

    def delay_supervisor(self, body):
        (self.startup / "sitecustomize.py").write_text(
            "import os, sys, time\n"
            "if len(sys.argv) > 1 and sys.argv[1] == '_supervise':\n"
            f"    {body}\n")

    def id_from(self, result):
        found = re.search(r"Worker ([0-9a-f]{12})", result.stdout)
        self.assertIsNotNone(found, result.stdout + result.stderr)
        return found.group(1)

    def state(self, job):
        result = self.cli("show", job, "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_slow_supervisor_warns_then_starts_and_logs_progress(self):
        self.delay_supervisor("time.sleep(2.5)")
        result = self.start()
        self.assertEqual(result.returncode, 0, result.stderr)
        job = self.id_from(result)
        self.assertIn("warning: start not confirmed after 1 s", result.stderr)
        self.assertIn(f"Run claive show {job} before any relaunch", result.stderr)
        self.assertEqual(self.state(job)["status"], "starting")
        deadline = time.monotonic() + 10
        seen = []
        while time.monotonic() < deadline:
            status = self.state(job)["status"]
            seen.append(status)
            self.assertNotEqual(status, "interrupted")
            if status == "completed":
                break
            time.sleep(0.1)
        self.assertIn("completed", seen)
        lines = (self.registry / job / "supervisor.log").read_text().splitlines()
        self.assertRegex(lines[0], r"^claive: spawn at .+ load [0-9.]+ [0-9.]+ [0-9.]+$")
        for step in ("supervisor pid", "supervisor entered", "state loaded", "acceptance ready",
                     "worker process started"):
            self.assertTrue(any(step in line for line in lines), step)

    def test_cancel_unconfirmed_supervisor_prevents_worker_launch(self):
        self.delay_supervisor("time.sleep(8)")
        result = self.start()
        self.assertEqual(result.returncode, 0, result.stderr)
        job = self.id_from(result)
        cancelled = self.cli("cancel", job)
        self.assertEqual(cancelled.returncode, 0, cancelled.stderr)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = self.state(job)
            if state["status"] == "cancelled":
                break
            time.sleep(0.05)
        self.assertEqual(state["status"], "cancelled")
        self.assertFalse((self.registry / job / "events.jsonl").exists())

    def test_dead_spawn_fails_and_load_reports_interrupted(self):
        self.delay_supervisor("time.sleep(0.3); os._exit(23)")
        result = self.start()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("exited with code 23", result.stderr)
        job = self.id_from(result)
        self.assertEqual(self.state(job)["status"], "interrupted")

    def test_live_spawn_without_supervisor_pid_remains_starting(self):
        path = self.base / "job"
        path.mkdir()
        module.save(path / "state.json", {"status": "starting", "started_at": time.time() - 1000})
        module.save(path / "spawn.json", {"pid": os.getpid(), "identity": module.identity(os.getpid()),
                                          "spawned_at": time.time() - 1000})
        self.assertEqual(module.load(path)["status"], "starting")
        (path / "spawn.json").unlink()
        self.assertEqual(module.load(path)["status"], "interrupted")

    def test_dead_spawn_with_missing_identity_is_interrupted(self):
        path = self.base / "job"
        path.mkdir()
        module.save(path / "state.json", {"status": "starting", "started_at": time.time() - 1000})
        module.save(path / "spawn.json", {"pid": 99999999, "identity": None, "spawned_at": time.time() - 1000})
        self.assertEqual(module.load(path)["status"], "interrupted")

    def test_config_launch_validation(self):
        with mock.patch.dict(os.environ, {"CLAIVE_CONFIG": str(self.config)}):
            self.config.write_text(json.dumps({"launch": {"start_timeout": 1.5, "git_timeout": 600}}))
            self.assertEqual(config.launch_setting("start_timeout", 5), 1.5)
            self.assertEqual(config.launch_setting("missing", 7), 7)
            for launch in ([], None, {"start_timeout": True}, {"git_timeout": "3"},
                           {"start_timeout": 0}, {"git_timeout": 601}, {"unknown": 1}):
                with self.subTest(launch=launch):
                    self.config.write_text(json.dumps({"launch": launch}))
                    with self.assertRaises(ValueError):
                        config.load()

    def test_git_timeout_reports_subcommand_and_workspace(self):
        with mock.patch.dict(os.environ, {"CLAIVE_CONFIG": str(self.config)}), \
             mock.patch.object(acceptance.subprocess, "run", side_effect=subprocess.TimeoutExpired("git", 30)), \
             mock.patch.object(acceptance.os, "getloadavg", return_value=(2.5, 0, 0)):
            with self.assertRaisesRegex(ValueError, r"git ls-files timed out after 30 s in .*\(load 2.50\)"):
                acceptance.snapshot(self.workspace)

    def test_unconfirmed_duplicate_refused_with_exceptions(self):
        self.delay_supervisor("time.sleep(5)")
        first = self.start()
        self.assertEqual(first.returncode, 0, first.stderr)
        job = self.id_from(first)
        duplicate = self.start()
        self.assertNotEqual(duplicate.returncode, 0)
        self.assertIn(f"worker {job} (label task) has not confirmed its start", duplicate.stderr)
        readonly = self.start(None, "--read-only")
        self.assertEqual(readonly.returncode, 0, readonly.stderr)
        other = self.base / "other"
        other.mkdir()
        separate = self.start(other)
        self.assertEqual(separate.returncode, 0, separate.stderr)
