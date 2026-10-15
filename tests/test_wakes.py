"""Scheduled wake parsing, management and delivery from serve/reusable workers."""
import hermetic  # noqa: F401
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from tests import test_messaging
from claivelib import cli, messaging, wakes
from claivelib.acceptance import atomic


class WakeMathChecks(unittest.TestCase):
    def test_duration_time_and_missed_recurrence_math(self):
        self.assertEqual(wakes.parse_duration("1h 30m"), 5400)
        self.assertIsNone(wakes.parse_duration("60"))
        self.assertIsNone(wakes.parse_duration("0s"))
        self.assertEqual(wakes.parse_at("+30m", now=100), 1900)
        self.assertEqual(wakes.parse_at("1970-01-01T00:01:40Z", now=0), 100)
        self.assertEqual(wakes.advance(100, 60, 250), (280, 2))
        self.assertEqual(wakes.advance(100, None, 250), (None, 0))

    def test_due_recurring_wake_advances_and_skips_missed_occurrences(self):
        with tempfile.TemporaryDirectory(prefix="wake-schedule-check-") as temp:
            path = Path(temp) / ("a" * 12)
            atomic(path / "state.json", dict(id=path.name, status="idle", reusable=True))
            state = dict(id=path.name, status="idle", reusable=True)
            with (mock.patch("claivelib.wakes.time.time", return_value=100),
                  mock.patch.object(cli, "load", return_value=state)):
                record = wakes.create(path, "Check in", "+30m", "1m")
            record["next_at"] = 100
            atomic(path / "messages" / "schedules" / f"{record['id']}.json", record)
            receipt = dict(id="b" * 32, status="queued")
            with (mock.patch.object(cli, "load", return_value=state),
                  mock.patch.object(messaging, "send", return_value=(receipt, False)) as send):
                result = wakes.deliver_due(path, now=250)
            self.assertEqual(len(result["delivered"]), 1)
            send.assert_called_once_with(path, "Check in", "wake", mock.ANY, kind="scheduled_wake")
            saved = wakes.list_schedules(path)[0]
            self.assertEqual(saved["status"], "scheduled")
            self.assertEqual(saved["next_at"], 280)
            self.assertEqual(saved["fired_count"], 1)
            self.assertEqual(saved["skipped_count"], 2)


class WakeCLIChecks(unittest.TestCase):
    setUp = test_messaging.MessagingChecks.setUp
    cli = test_messaging.MessagingChecks.cli
    open_worker = test_messaging.MessagingChecks.open_worker
    until_idle = test_messaging.MessagingChecks.until_idle
    until_running = test_messaging.MessagingChecks.until_running
    stop_workers = test_messaging.MessagingChecks.stop_workers

    def test_send_schedule_list_cancel_and_validation(self):
        _process, worker_id = self.open_worker()
        self.until_idle(worker_id)
        result = self.cli("send", worker_id, "--message", "Check the deployment.",
                          "--at", "+30m", "--every", "1h", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads(result.stdout)
        self.assertEqual(record["status"], "scheduled")
        self.assertEqual(record["every"], 3600)
        self.assertEqual(list((self.registry / worker_id / "messages/receipts").glob("*.json")), [])

        listed = self.cli("schedule", "list", worker_id, "--json")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual(json.loads(listed.stdout), [record])
        cancelled = self.cli("schedule", "cancel", worker_id, record["id"], "--json")
        self.assertEqual(cancelled.returncode, 0, cancelled.stderr)
        self.assertEqual(json.loads(cancelled.stdout)["status"], "cancelled")
        self.assertIn("--every requires --at",
                      self.cli("send", worker_id, "--message", "Invalid schedule.", "--every", "1h").stderr)

    def test_reusable_idle_loop_delivers_due_wake(self):
        _process, worker_id = self.open_worker()
        self.until_idle(worker_id)
        result = self.cli("send", worker_id, "--message", "Review the latest run.",
                          "--at", "+1s", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        schedule = json.loads(result.stdout)
        state = self.until_idle(worker_id, 2)
        self.assertEqual(state["turn"], 2)
        saved = json.loads((self.registry / worker_id / "messages" / "schedules" /
                            f"{schedule['id']}.json").read_text())
        self.assertEqual(saved["status"], "delivered")
        receipt = json.loads((self.registry / worker_id / "messages" / "receipts" /
                              f"{saved['last_delivery_id']}.json").read_text())
        self.assertEqual(receipt["mode"], "wake")

    def test_serve_delivers_due_wake_for_busy_reusable_worker(self):
        _process, worker_id = self.open_worker(mode="slow")
        self.until_running(worker_id)
        result = self.cli("send", worker_id, "--message", "Run the next scheduled check.",
                          "--at", "+1s", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        schedule = json.loads(result.stdout)
        time.sleep(1.1)
        served = self.cli("serve", "--once")
        self.assertEqual(served.returncode, 0, served.stderr)
        saved = json.loads((self.registry / worker_id / "messages" / "schedules" /
                            f"{schedule['id']}.json").read_text())
        self.assertEqual(saved["status"], "delivered")
        receipt = json.loads((self.registry / worker_id / "messages" / "receipts" /
                              f"{saved['last_delivery_id']}.json").read_text())
        self.assertEqual(receipt["mode"], "wake")
        self.assertEqual(receipt["status"], "queued")


if __name__ == "__main__":
    unittest.main()
