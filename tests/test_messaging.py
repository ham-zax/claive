"""Parent-to-worker note, wake, and in-flight message delivery."""
import hermetic  # noqa: F401
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
CLI = str(REPO_ROOT / "tests/fixture-workers")
sys.path.insert(0, str(REPO_ROOT / "bin"))
from claivelib import messaging


class EnvelopeChecks(unittest.TestCase):
    def setUp(self):
        self.note = dict(id="a" * 32, sender="parent (codex)", kind="send",
                         message="Keep this body.\n\nIncluding its blank line.\n")
        self.ask = dict(self.note, id="b" * 32, kind="ask", expires_at=0)

    def test_exact_note_and_ask_envelopes(self):
        note_instruction = "This is a note from the parent, not a question and not a change of assignment. No reply is needed."
        ask_instruction = (
            'Answer it now with `"$CLAIVE_BIN" reply ' + "b" * 32 + ' --message "..."` '
            '(a short, direct reply), then carry on with what you were doing. '
            'Do not restructure your work around the question. If you cannot run '
            'that command, give the answer in your final message. If you read this '
            'after 1970-01-01T00:00:00Z, ignore it.')
        for record, instruction in ((self.note, note_instruction), (self.ask, ask_instruction)):
            with self.subTest(kind=record["kind"]):
                self.assertEqual(messaging.delivered_text(record),
                    f"<parent-message>\n{instruction}\n\n"
                    f"[Message ID: {record['id']} | from: parent (codex)]\n"
                    f"{record['message']}\n</parent-message>")

    def test_extraction_round_trips_and_rejects_other_text(self):
        for record, kind in ((self.note, "note"), (self.ask, "ask")):
            envelope = messaging.delivered_text(record)
            self.assertEqual(messaging.extract_parent_message(envelope),
                             (kind, record["id"], record["message"]))
            for text in ("quoted:\n" + envelope, envelope + "\nextra", envelope + "\n",
                         envelope.replace("No reply is needed.", "Please reply.") if kind == "note"
                         else envelope.replace("Answer it now", "Answer it later"),
                         envelope + "\n\n" + envelope, "plain text"):
                with self.subTest(kind=kind, text=text):
                    self.assertIsNone(messaging.extract_parent_message(text))
        envelope = messaging.delivered_text(self.ask)
        self.assertIsNone(messaging.extract_parent_message(
            envelope.replace('"$CLAIVE_BIN" reply ' + self.ask["id"], '"$CLAIVE_BIN" reply ' + "c" * 32)))

    def test_format_block_joins_visible_envelopes_without_heading(self):
        messages = [(None, self.note), (None, self.ask)]
        self.assertEqual(messaging.format_block(messages),
                         messaging.delivered_text(self.note) + "\n\n" + messaging.delivered_text(self.ask))
        self.assertEqual(messaging.format_block(messages, [self.note["id"]]),
                         messaging.delivered_text(self.ask))
        self.assertEqual(messaging.format_block(messages, [self.note["id"], self.ask["id"]]), "")


class MessagingChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="messaging-check-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.registry = self.root / "registry"
        self.prompt = self.root / "base-prompt.md"
        self.prompt.write_text("Continue the existing assignment.")
        self.prompt_log = self.root / "prompts.log"
        self.fixture = REPO_ROOT / "tests/fixture_worker.py"
        self.env = dict(os.environ, CLAIVE_DIR=str(self.registry),
                        FIXTURE_WORKER_BINARY=str(self.fixture),
                        FIXTURE_PROMPT_OUT=str(self.prompt_log), FIXTURE_MODE="success")
        self.ids = []
        self.open_processes = []
        self.addCleanup(self.stop_workers)

    def cli(self, *args, mode=None, timeout=15):
        env = dict(self.env)
        if mode is not None:
            env["FIXTURE_MODE"] = mode
        return subprocess.run([CLI, *args], env=env, text=True,
                              capture_output=True, timeout=timeout)

    def open_worker(self, mode="success"):
        before = set(self.registry.glob("*/state.json")) if self.registry.exists() else set()
        process = subprocess.Popen(
            [CLI, "open", "--engine", "fixture", "--workspace", str(self.root),
             "--prompt-file", str(self.prompt)],
            env=dict(self.env, FIXTURE_MODE=mode), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, start_new_session=True,
        )
        self.open_processes.append(process)
        for _ in range(400):
            matches = (set(self.registry.glob("*/state.json")) - before
                       if self.registry.exists() else set())
            if matches:
                state = json.loads(next(iter(matches)).read_text())
                worker_id = state["id"]
                self.ids.append(worker_id)
                return process, worker_id
            if process.poll() is not None:
                self.fail(str(process.communicate()))
            time.sleep(0.025)
        self.fail("reusable worker did not register")

    def until_idle(self, worker_id, turn=1):
        state_file = self.registry / worker_id / "state.json"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            state = json.loads(state_file.read_text())
            if state["status"] == "idle" and state["turn"] == turn:
                return state
            time.sleep(0.025)
        self.fail(f"worker did not become idle after turn {turn}")

    def until_running(self, worker_id):
        state_file = self.registry / worker_id / "state.json"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = json.loads(state_file.read_text())
            if state["status"] == "running" and state.get("worker_pid"):
                return state
            time.sleep(0.01)
        self.fail("worker did not enter a running turn")

    def prompt_for_turn(self, turn_index):
        chunks = self.prompt_log.read_text().split("\n----\n")
        self.assertGreater(len(chunks), turn_index)
        return chunks[turn_index]

    def stop_workers(self):
        for worker_id in self.ids:
            state_file = self.registry / worker_id / "state.json"
            if not state_file.is_file():
                continue
            try:
                state = json.loads(state_file.read_text())
            except (OSError, ValueError):
                continue
            if state.get("status") in {"starting", "running", "cancelling", "idle"}:
                self.cli("cancel", worker_id)
        for process in self.open_processes:
            if process.poll() is None:
                try:
                    process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate(timeout=5)
        for worker_id in self.ids:
            self.cli("wait", worker_id, "--timeout", "5")

    def test_note_is_prepended_to_next_followup_exactly_once(self):
        _process, worker_id = self.open_worker()
        self.until_idle(worker_id)
        self.assertEqual(self.cli("alias", "set", "builder", worker_id).returncode, 0)
        message_file = self.root / "parent-note.md"
        message_file.write_text("Please preserve the existing API.")
        message_id = "a" * 32
        sent = self.cli("send", "@builder", "--message-file", str(message_file),
                        "--id", message_id, "--json")
        self.assertEqual(sent.returncode, 0, sent.stderr)
        self.assertEqual(json.loads(sent.stdout)["result"], "queued for next turn")

        self.assertEqual(self.cli("followup", worker_id, "--prompt-file", str(self.prompt)).returncode, 0)
        self.until_idle(worker_id, 2)
        prompt = self.prompt_for_turn(1)
        self.assertEqual(prompt.count("<parent-message>"), 1)
        self.assertEqual(prompt.count("</parent-message>"), 1)
        self.assertIn(messaging.delivered_text(dict(id=message_id, message=message_file.read_text(),
                                                   sender=json.loads(sent.stdout)["sender"])), prompt)
        self.assertLess(prompt.index("[Message ID: " + message_id + " | from: "), prompt.index("Continue the existing assignment."))
        self.assertEqual(prompt.count("Please preserve the existing API."), 1)
        receipt = json.loads((self.registry / worker_id / "messages/receipts" / f"{message_id}.json").read_text())
        self.assertEqual(receipt["status"], "delivered")
        self.assertEqual(receipt["turn"], 2)
        self.assertEqual(len(list((self.registry / worker_id / "messages/delivered").glob("*.json"))), 1)
        self.assertEqual(list((self.registry / worker_id / "messages/pending").glob("*.json")), [])

    def test_wake_starts_turn_on_idle_worker(self):
        _process, worker_id = self.open_worker()
        self.until_idle(worker_id)
        message = "Please check the newest test output."
        sent = self.cli("send", worker_id, "--mode", "wake", "--message", message, "--json")
        self.assertEqual(sent.returncode, 0, sent.stderr)
        self.assertEqual(json.loads(sent.stdout)["result"], "delivered and started a turn")
        self.until_idle(worker_id, 2)
        prompt = self.prompt_for_turn(1)
        self.assertEqual(prompt.count(message), 1)
        sent_record = json.loads(sent.stdout)
        envelope = messaging.delivered_text(dict(id=sent_record["id"], message=message,
                                                sender=sent_record["sender"]))
        self.assertEqual(prompt, envelope + "\n")
        self.assertEqual((self.registry / worker_id / "messages/wake-prompts" /
                          f"{sent_record['id']}.md").read_text(), envelope + "\n")
        receipt = json.loads((self.registry / worker_id / "messages/receipts" /
                              f"{json.loads(sent.stdout)['id']}.json").read_text())
        self.assertEqual(receipt["status"], "delivered")
        self.assertEqual(receipt["turn"], 2)

    def test_wake_on_busy_worker_only_queues_note(self):
        _process, worker_id = self.open_worker(mode="brief")
        self.until_running(worker_id)
        sent = self.cli("send", worker_id, "--mode", "wake", "--message", "After this turn, inspect lint.", "--json")
        self.assertEqual(sent.returncode, 0, sent.stderr)
        receipt = json.loads(sent.stdout)
        self.assertEqual(receipt["result"], "queued for next turn")
        self.until_idle(worker_id)
        self.assertEqual(list((self.registry / worker_id / "requests").glob("*.json")), [])
        self.assertEqual(json.loads((self.registry / worker_id / "state.json").read_text())["turn"], 1)
        self.assertEqual(self.cli("followup", worker_id, "--prompt-file", str(self.prompt)).returncode, 0)
        self.until_idle(worker_id, 2)
        self.assertEqual(self.prompt_for_turn(1).count("After this turn, inspect lint."), 1)

    def test_now_falls_back_to_wake_for_engine_without_steering(self):
        _process, worker_id = self.open_worker()
        self.until_idle(worker_id)
        sent = self.cli("send", worker_id, "--mode", "now", "--message", "Run the final check.", "--json")
        self.assertEqual(sent.returncode, 0, sent.stderr)
        receipt = json.loads(sent.stdout)
        self.assertEqual(receipt["requested_mode"], "now")
        self.assertEqual(receipt["mode"], "wake")
        self.assertIn("fallback to wake: the fixture engine has no in-flight steering", receipt["result"])
        self.assertIn("delivered and started a turn", receipt["result"])
        self.until_idle(worker_id, 2)
        self.assertEqual(self.prompt_for_turn(1).count("Run the final check."), 1)

    def test_now_uses_steering_receipt_when_running_engine_supports_it(self):
        path = self.registry / "synthetic-worker"
        path.mkdir(parents=True)
        worker_state = dict(engine="fake", status="running", terminal=False,
                            worker_pid=123, turn=4)
        engine = mock.Mock()
        engine.steering_capability.return_value = SimpleNamespace(supported=True, reason="")
        steering_receipt = {"id": "c" * 32, "turn": 4, "status": "pending"}
        with (mock.patch("claivelib.cli.load", return_value=worker_state),
              mock.patch("claivelib.engines.get_engine", return_value=engine),
              mock.patch("claivelib.state.launch_config", return_value={"steering": True}),
              mock.patch("claivelib.steering.submit", return_value=steering_receipt) as submit):
            receipt, duplicate = messaging.send(path, "Continue the live turn.", "now", "d" * 32)
        self.assertFalse(duplicate)
        submit.assert_called_once_with(path,
            messaging.delivered_text(dict(id="d" * 32, message="Continue the live turn.",
                                          sender=receipt["sender"])))
        self.assertIn("delivered mid-turn", receipt["result"])
        self.assertIn(steering_receipt["id"], receipt["result"])
        self.assertEqual(receipt["steering_receipt_id"], steering_receipt["id"])
        self.assertEqual(receipt["turn"], 4)

    def test_mid_turn_ask_deadline_is_stored_in_steering_request(self):
        path = self.registry / "expiring-steered-worker"
        path.mkdir(parents=True)
        state = dict(id=path.name, engine="fake", status="running", terminal=False,
                     worker_pid=123, turn=4)
        engine = mock.Mock()
        engine.steering_capability.return_value = SimpleNamespace(supported=True, reason="")
        deadline = time.time() + 30

        with (mock.patch("claivelib.cli.load", return_value=state),
              mock.patch("claivelib.engines.get_engine", return_value=engine),
              mock.patch("claivelib.steering.get_engine", return_value=engine),
              mock.patch("claivelib.state.launch_config", return_value={"steering": True}),
              mock.patch("claivelib.steering.launch_config", return_value={"steering": True})):
            receipt, duplicate = messaging.send(path, "Question?", "now", "2" * 32,
                                                kind="ask", expires_at=deadline)

        self.assertFalse(duplicate)
        request_file = next((path / "steering/requests").glob("*.json"))
        self.assertEqual(json.loads(request_file.read_text())["expires_at"], deadline)
        self.assertEqual(receipt["steering_receipt_id"], json.loads(request_file.read_text())["id"])

    def test_duplicate_id_returns_receipt_only_for_same_message(self):
        _process, worker_id = self.open_worker()
        self.until_idle(worker_id)
        message_id = "b" * 32
        first = self.cli("send", worker_id, "--message", "Original content.", "--id", message_id, "--json")
        same_retry = self.cli("send", worker_id, "--message", "Original content.", "--id", message_id, "--json")
        changed_retry = self.cli("send", worker_id, "--message", "Changed retry content.",
                                 "--id", message_id, "--json")
        empty_retry = self.cli("send", worker_id, "--message", "", "--id", message_id, "--json")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(same_retry.returncode, 0, same_retry.stderr)
        self.assertEqual(changed_retry.returncode, 1)
        self.assertIn(f"message ID {message_id} was already used for a different message",
                      changed_retry.stderr)
        self.assertEqual(empty_retry.returncode, 1)
        self.assertIn(f"message ID {message_id} was already used for a different message",
                      empty_retry.stderr)
        original = json.loads(first.stdout)
        duplicate = json.loads(same_retry.stdout)
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(duplicate["notice"], "duplicate: original receipt returned")
        self.assertEqual(duplicate["result"], original["result"])
        self.assertEqual(len(list((self.registry / worker_id / "messages/pending").glob("*.json"))), 1)
        self.assertEqual(self.cli("followup", worker_id, "--prompt-file", str(self.prompt)).returncode, 0)
        self.until_idle(worker_id, 2)
        prompt = self.prompt_for_turn(1)
        self.assertEqual(prompt.count("Original content."), 1)
        self.assertNotIn("Changed retry content.", prompt)

    def test_legacy_receipt_without_digest_returns_stored_result(self):
        path = self.registry / "legacy-worker"
        path.mkdir(parents=True)
        identifier = "e" * 32
        receipt = dict(id=identifier, status="delivered", result="legacy result")
        messaging._store_receipt(path, receipt)

        duplicate, is_duplicate = messaging.send(path, "retry content", identifier=identifier)

        self.assertTrue(is_duplicate)
        self.assertEqual(duplicate, receipt)

    def test_steering_acknowledgement_updates_message_receipt(self):
        path = self.registry / "steered-worker"
        path.mkdir(parents=True)
        identifier = "f" * 32
        steering_id = "1" * 32
        message = "same content"
        receipt = dict(id=identifier, status="pending", turn=4, steering_receipt_id=steering_id,
                       message_digest=hashlib.sha256(message.encode("utf-8")).hexdigest())
        messaging._store_receipt(path, receipt)
        steering_file = path / "steering/receipts" / f"{steering_id}.json"
        steering_file.parent.mkdir(parents=True)
        steering_file.write_text(json.dumps(dict(id=steering_id, status="acknowledged", turn=4)))

        current, duplicate = messaging.send(path, message, identifier=identifier)

        self.assertTrue(duplicate)
        self.assertEqual(current["status"], "delivered")
        self.assertEqual(current["turn"], 4)
        self.assertEqual(json.loads(messaging._receipt_path(path, identifier).read_text())["status"], "delivered")

    def test_refuses_invalid_messages_ids_and_worker_states(self):
        _process, worker_id = self.open_worker()
        self.until_idle(worker_id)
        empty = self.cli("send", worker_id, "--message", "")
        self.assertNotEqual(empty.returncode, 0)
        self.assertIn("message must not be empty", empty.stderr)

        too_large = self.root / "too-large.md"
        too_large.write_text("x" * (128 * 1024 + 1))
        oversized = self.cli("send", worker_id, "--message-file", str(too_large))
        self.assertNotEqual(oversized.returncode, 0)
        self.assertIn("128 KiB", oversized.stderr)

        for supplied_id in ("not-an-id", ""):
            invalid_id = self.cli("send", worker_id, "--message", "hello", "--id", supplied_id)
            self.assertNotEqual(invalid_id.returncode, 0)
            self.assertIn("32 lowercase hexadecimal", invalid_id.stderr)

        # A live single-turn worker is not reusable, even while it is running.
        started = self.cli("start", "--engine", "fixture", "--workspace", str(self.root),
                           "--prompt-file", str(self.prompt), mode="brief")
        self.assertEqual(started.returncode, 0, started.stderr)
        nonreusable_id = re.search(r"Worker ([0-9a-f]{12})", started.stdout)[1]
        self.ids.append(nonreusable_id)
        self.until_running(nonreusable_id)
        not_reusable = self.cli("send", nonreusable_id, "--message", "not for this worker")
        self.assertNotEqual(not_reusable.returncode, 0)
        self.assertIn("not reusable", not_reusable.stderr)
        self.assertIn("claive open", not_reusable.stderr)

        # A closed reusable worker needs to be reopened before another send.
        process, closed_id = self.open_worker()
        self.until_idle(closed_id)
        self.assertEqual(self.cli("close", closed_id).returncode, 0)
        process.communicate(timeout=5)
        closed = self.cli("send", closed_id, "--message", "after close")
        self.assertNotEqual(closed.returncode, 0)
        self.assertIn("reopen it with claive open", closed.stderr)

        # Cancellation is also terminal for note and wake delivery.
        process, cancelled_id = self.open_worker()
        self.until_idle(cancelled_id)
        self.assertEqual(self.cli("cancel", cancelled_id).returncode, 0)
        process.communicate(timeout=5)
        cancelled = self.cli("send", cancelled_id, "--mode", "wake", "--message", "after cancel")
        self.assertNotEqual(cancelled.returncode, 0)
        self.assertIn("cancelled", cancelled.stderr)
        self.assertIn("reopen it", cancelled.stderr)

        terminal = self.cli("run", "--engine", "fixture", "--workspace", str(self.root),
                            "--prompt-file", str(self.prompt))
        terminal_id = re.search(r"Worker ([0-9a-f]{12})", terminal.stdout)[1]
        refused = self.cli("send", terminal_id, "--message", "after terminal")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("worker is completed", refused.stderr)
