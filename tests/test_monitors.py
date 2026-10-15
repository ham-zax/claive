"""Parent-owned monitor schedules, checks, and message delivery."""
import hermetic  # noqa: F401  (must run before claivelib reads the environment)
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
CLI = str(REPO / "tests/fixture-workers")
sys.path.insert(0, str(REPO / "bin"))
from claivelib import acceptance, messaging, monitor_runner, monitors


class MonitorMathChecks(unittest.TestCase):
    def test_interval_bounds_and_due_math(self):
        self.assertEqual(monitors.parse_interval("30s"), 30)
        self.assertEqual(monitors.parse_interval("24h"), 86_400)
        self.assertEqual(monitors.next_due_at(100, 30), 130)
        self.assertTrue(monitors.is_due({"next_run_at": 100}, 100))
        self.assertFalse(monitors.is_due({"next_run_at": 101}, 100))
        for value in ("29s", "24h1s", "0m", "tomorrow", "30"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                monitors.parse_interval(value)

    def test_normalization_hash_and_unified_diff_are_stable_and_bounded(self):
        old = monitors.normalize_output("alpha  \r\nbeta\t\n")
        self.assertEqual(old, "alpha\nbeta\n")
        self.assertEqual(monitors.content_hash(old), monitors.content_hash(old))
        delta = monitors.render_unified_diff(old, "alpha\ngamma\n")
        self.assertIn("-beta", delta)
        self.assertIn("+gamma", delta)
        many = "\n".join(f"old-{n}" for n in range(100))
        changed = "\n".join(f"new-{n}" for n in range(100))
        self.assertLessEqual(len(monitors.render_unified_diff(many, changed).splitlines()), 40)


class MonitorRunnerChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="claive-monitors-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.registry = self.root / "registry"
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.worker_id = "a" * 12
        self.worker = self.registry / self.worker_id
        self.worker.mkdir(parents=True)
        (self.worker / "requests").mkdir()
        self.write_worker()
        self.env = dict(os.environ, CLAIVE_DIR=str(self.registry), TERM="dumb")
        environment = mock.patch.dict(os.environ, self.env)
        environment.start()
        self.addCleanup(environment.stop)
        self.sent = []

    def write_worker(self, **changes):
        state = {
            "schema_version": 2, "id": self.worker_id, "status": "idle", "phase": "idle",
            "reusable": True, "started_at": time.time(), "workspace": str(self.workspace),
            "label": "watched worker", "engine": "muse", "model": "muse-spark-1.3-contributor",
        }
        state.update(changes)
        (self.worker / "state.json").write_text(json.dumps(state))
        return state

    def command(self, *args, env=None):
        return subprocess.run([CLI, *args], env=dict(env or self.env), text=True,
                              capture_output=True, timeout=10)

    def send(self, path, message, mode):
        self.sent.append((Path(path), message, mode))
        return ({"status": "queued"}, False)

    def add_monitor(self, command="cat watched.txt", label="watch label"):
        result = monitor_runner.add(self.worker_id, command, "30s", label)
        self.assertEqual(result["label"], label)
        return self.worker / "monitors" / f"{result['id']}.json"

    def force_due(self, path):
        monitor = json.loads(path.read_text())
        monitor["next_run_at"] = 0
        acceptance.atomic(path, monitor)

    def run_due(self, now=None):
        return monitor_runner.run_due(now=now or time.time() + 1)

    def test_add_list_cancel_and_closed_worker_refusal(self):
        (self.workspace / "watched.txt").write_text("initial\n")
        added = self.command("monitor", "add", self.worker_id, "--command", "cat watched.txt",
                             "--every", "30s", "--label", "watch label")
        self.assertEqual(added.returncode, 0, added.stderr)
        monitor_id = added.stdout.split()[1]
        record = self.worker / "monitors" / f"{monitor_id}.json"
        self.assertTrue(record.is_file())
        saved = json.loads(record.read_text())
        self.assertEqual(saved["command"], "cat watched.txt")
        self.assertEqual(saved["interval_seconds"], 30)
        listing = self.command("monitor", "list", self.worker_id, "--json")
        self.assertEqual(json.loads(listing.stdout)[0]["id"], monitor_id)
        all_listing = self.command("monitor", "list", "--json")
        self.assertEqual(json.loads(all_listing.stdout)[0]["id"], monitor_id)
        cancelled = self.command("monitor", "cancel", monitor_id)
        self.assertEqual(cancelled.returncode, 0, cancelled.stderr)
        self.assertFalse(record.exists())

        self.write_worker(status="completed", phase="session closed")
        closed = self.command("monitor", "add", self.worker_id, "--command", "cat watched.txt",
                              "--every", "30s")
        self.assertNotEqual(closed.returncode, 0)
        self.assertIn("is closed", closed.stderr)

    def test_monitor_commands_are_parent_only_and_help_requires_read_only_commands(self):
        path = self.add_monitor()
        monitor_id = path.stem
        worker_env = dict(self.env, CLAIVE_WORKER_ID=self.worker_id)
        commands = (
            ("add", self.worker_id, "--command", "true", "--every", "30s"),
            ("cancel", monitor_id),
            ("run-due",),
        )
        for args in commands:
            with self.subTest(args=args):
                result = self.command("monitor", *args, env=worker_env)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("may not launch workers", result.stderr)
        help_result = self.command("monitor", "add", "--help")
        self.assertIn("read-only", help_result.stdout.lower())

    def test_first_check_is_baseline_then_changes_deliver_wake_and_note(self):
        (self.workspace / "watched.txt").write_text("first  \n")
        path = self.add_monitor()
        with mock.patch.object(messaging, "send", side_effect=self.send):
            self.run_due()
            self.assertEqual(self.sent, [])
            baseline = json.loads(path.read_text())
            self.assertEqual(baseline["last_snapshot"], "first\n")
            self.assertTrue(baseline["last_snapshot_hash"])

            (self.workspace / "watched.txt").write_text("\x1b[31msecond\x1b[0m\n")
            self.force_due(path)
            self.run_due()
            self.assertEqual(self.sent[-1][2], "wake")
            self.assertIn("watch label", self.sent[-1][1])
            self.assertIn("-first", self.sent[-1][1])
            self.assertIn("+second", self.sent[-1][1])
            self.assertNotIn("\x1b", self.sent[-1][1])
            self.assertLessEqual(len(self.sent[-1][1].splitlines()), 40)

            self.write_worker(status="running")
            (self.workspace / "watched.txt").write_text("third\n")
            self.force_due(path)
            self.run_due()
            self.assertEqual(self.sent[-1][2], "note")

    def test_failures_are_sanitized_and_notified_once_until_success(self):
        path = self.add_monitor("printf '\\033[31mboom\\033[0m' >&2; exit 2")
        with mock.patch.object(messaging, "send", side_effect=self.send):
            self.run_due()
            self.assertEqual(len(self.sent), 1)
            self.assertIn("boom", self.sent[0][1])
            self.assertNotIn("\x1b", self.sent[0][1])
            self.force_due(path)
            self.run_due()
            self.assertEqual(len(self.sent), 1)

            monitor = json.loads(path.read_text())
            monitor["command"] = "printf 'recovered\\n'"
            monitor["next_run_at"] = 0
            acceptance.atomic(path, monitor)
            self.run_due()
            self.assertFalse(json.loads(path.read_text())["failure_notified"])

            monitor = json.loads(path.read_text())
            monitor["command"] = "printf '\\033[31mboom\\033[0m' >&2; exit 2"
            monitor["next_run_at"] = 0
            acceptance.atomic(path, monitor)
            self.run_due()
            self.assertEqual(len(self.sent), 2)

    def test_runner_caps_output_times_out_and_skips_closed_workers(self):
        capped = monitor_runner.run_command("head -c 100000 /dev/zero", self.workspace)
        self.assertEqual(len(capped["stdout"]) + len(capped["stderr"]), 64 * 1024)
        self.assertTrue(capped["truncated"])
        timed = monitor_runner.run_command("sleep 0.2", self.workspace, timeout=0.03)
        self.assertTrue(timed["timed_out"])

        touch = self.add_monitor("touch ran")
        self.write_worker(status="completed", phase="session closed")
        self.run_due()
        self.assertFalse((self.workspace / "ran").exists())
        self.assertTrue(touch.exists())


if __name__ == "__main__":
    unittest.main()
