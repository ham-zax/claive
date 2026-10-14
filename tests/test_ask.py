"""Questions, deadline withdrawal, sender guards and durable turn answers."""
import hermetic  # noqa: F401
from contextlib import contextmanager
import json
import os
from pathlib import Path
import signal
import time
import unittest
from unittest import mock

from tests import test_messaging
from claivelib import asking, cli, messaging, steering, turns
from claivelib.acceptance import atomic


class AskChecks(unittest.TestCase):
    # Share the existing fixture lifecycle without inheriting its test cases.
    setUp = test_messaging.MessagingChecks.setUp
    cli = test_messaging.MessagingChecks.cli
    open_worker = test_messaging.MessagingChecks.open_worker
    until_idle = test_messaging.MessagingChecks.until_idle
    until_running = test_messaging.MessagingChecks.until_running
    prompt_for_turn = test_messaging.MessagingChecks.prompt_for_turn
    stop_workers = test_messaging.MessagingChecks.stop_workers

    def synthetic(self, identifier, status="idle", label="fixture"):
        path = self.registry / identifier
        atomic(path / "state.json", dict(id=identifier, status=status, label=label,
                                         engine="fixture", model="test-model", turn=1))
        return path

    def test_prefix_unique_ambiguous_unknown_and_wait_resolution(self):
        first = self.synthetic("abcd00000001", "completed", "one")
        with mock.patch.dict(os.environ, self.env):
            self.assertEqual(cli.job_path("abcd"), first)
        shown = self.cli("show", "abcd", "--json")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        waited = self.cli("wait", "abcd", "--timeout", "1")
        self.assertEqual(waited.returncode, 0, waited.stderr)
        self.synthetic("abcd00000002", "failed", "two")
        ambiguous = self.cli("show", "abcd")
        self.assertEqual(ambiguous.returncode, 1)
        self.assertIn("worker ID prefix 'abcd' is ambiguous: abcd00000001 (completed, one), "
                      "abcd00000002 (failed, two); use the full ID or an @alias", ambiguous.stderr)
        for identifier in ("ffff", "ffffffffffff"):
            unknown = self.cli("show", identifier)
            self.assertEqual(unknown.returncode, 1)
            self.assertIn(f"unknown worker '{identifier}'; run claive list", unknown.stderr)
        for identifier, error in (("", "empty worker ID"), ("xyz", "invalid worker ID")):
            invalid = self.cli("show", identifier)
            self.assertEqual(invalid.returncode, 1)
            self.assertIn(error, invalid.stderr)

    def test_sender_guards_precede_message_validation_for_send_and_ask(self):
        worker_id = "abcd00000001"
        self.synthetic(worker_id)
        self.env["CLAIVE_WORKER_ID"] = worker_id
        for command in ("send", "ask"):
            own = self.cli(command, "abcd", "--message-file", "/does-not-exist")
            self.assertEqual(own.returncode, 1)
            self.assertIn("a worker cannot message itself; put the note in your own answer", own.stderr)
            self.env["CLAIVE_WORKER_ID"] = "ffff00000001"
            nested = self.cli(command, worker_id, "--message", "")
            self.assertEqual(nested.returncode, 1)
            self.assertIn("workers cannot send messages to workers; report to the parent in your final answer", nested.stderr)
            self.env["CLAIVE_WORKER_ID"] = worker_id

    def test_needs_parent_refuses_wake_and_ask_before_receipts_but_accepts_note(self):
        _process, worker_id = self.open_worker()
        state = self.until_idle(worker_id)
        path = self.registry / worker_id
        state["needs_parent"] = dict(question="Choose A or B?")
        atomic(path / "state.json", state)
        for args in (("send", worker_id, "--mode", "wake"),
                     ("send", worker_id, "--mode", "now"), ("ask", worker_id)):
            refused = self.cli(*args, "--message", "Question?")
            self.assertEqual(refused.returncode, 1)
            self.assertIn(f"answer first with claive answer {worker_id} --message ..., or use --mode note", refused.stderr)
            self.assertEqual(list((path / "messages/receipts").glob("*.json")), [])
            self.assertEqual(list((path / "messages/pending").glob("*.json")), [])
        note = self.cli("send", worker_id, "--message", "Context only.")
        self.assertEqual(note.returncode, 0, note.stderr)

    def test_sender_and_target_receipts_and_prompt_headers(self):
        _process, worker_id = self.open_worker()
        self.until_idle(worker_id)
        for index, parent in enumerate(("codex", "", None), start=2):
            if parent is None:
                self.env.pop("CLAIVE_PARENT", None)
            else:
                self.env["CLAIVE_PARENT"] = parent
            result = self.cli("send", worker_id, "--mode", "wake", "--message", "Receipt test.", "--json")
            self.assertEqual(result.returncode, 0, result.stderr)
            receipt = json.loads(result.stdout)
            self.assertEqual(receipt["target"], worker_id)
            self.assertEqual(receipt["engine"], "fixture")
            state = self.until_idle(worker_id, index)
            self.assertEqual(receipt["model"], state["model"])
            sender = "parent (codex)" if parent else "parent (unknown harness)"
            self.assertEqual(receipt["sender"], sender)
            self.assertIn(f"[Message ID: {receipt['id']} | from: {sender}]", self.prompt_for_turn(index - 1))

    def test_nested_parent_sender_identity(self):
        _process, target_id = self.open_worker()
        self.until_idle(target_id)
        sender_id = "abcd00000001"
        self.synthetic(sender_id)
        self.env.update(CLAIVE_WORKER_ID=sender_id, CLAIVE_ALLOW_NESTED="1")
        result = self.cli("send", target_id, "--message", "Nested parent context.", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["sender"], f"worker {sender_id} (fixture/test-model)")
        pending = next((self.registry / target_id / "messages/pending").glob("*.json"))
        self.assertEqual(json.loads(pending.read_text())["sender"], receipt["sender"])

    def test_idle_ask_wakes_and_answers_from_per_turn_final_text(self):
        _process, worker_id = self.open_worker()
        self.until_idle(worker_id)
        result = self.cli("ask", worker_id[:4], "--message", "What did you find?", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        ask = json.loads(result.stdout)
        self.assertEqual(ask["status"], "answered")
        self.assertEqual(ask["answer_source"], "final_text")
        self.assertEqual(ask["answer"], "fixture result")
        self.assertEqual(ask["delivery"]["mode"], "wake")
        self.assertEqual(ask["delivered_turn"], 2)
        path = self.registry / worker_id
        for turn in (1, 2):
            self.assertEqual((path / "results" / f"turn-{turn:04}.txt").read_text(), "fixture result")
        prompt = self.prompt_for_turn(1)
        envelope = messaging.delivered_text(dict(id=ask["id"], kind="ask", message="What did you find?",
                                                sender=ask["sender"], expires_at=ask["deadline"]))
        self.assertEqual(prompt, envelope + "\n")
        self.assertEqual(messaging.extract_parent_message(envelope),
                         ("ask", ask["id"], "What did you find?"))

    def test_reply_from_fixture_wins_over_final_text_and_second_reply_refused(self):
        output = self.root / "replies.json"
        self.env["FIXTURE_REPLY_OUT"] = str(output)
        _process, worker_id = self.open_worker()
        self.until_idle(worker_id)
        result = self.cli("ask", worker_id, "--message", "Question?\nFIXTURE_MODE=reply", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        ask = json.loads(result.stdout)
        self.assertEqual(ask["answer_source"], "reply")
        self.assertEqual(ask["answer"], "fixture reply")
        self.until_idle(worker_id, 2)
        replies = json.loads(output.read_text())
        self.assertEqual(replies[0]["code"], 0)
        self.assertIn(f"Reply recorded for {ask['id']}", replies[0]["stdout"])
        self.assertEqual(replies[1]["code"], 1)
        self.assertTrue("already has an answer" in replies[1]["stderr"] or
                        "the parent no longer waits" in replies[1]["stderr"])

    def test_reply_without_worker_other_worker_and_after_withdrawal_refused(self):
        identifier = "a" * 32
        missing_worker = self.cli("reply", identifier, "--message", "Answer")
        self.assertEqual(missing_worker.returncode, 1)
        self.assertIn("claive reply works only inside a worker turn", missing_worker.stderr)
        first = self.synthetic("abcd00000001")
        second = self.synthetic("abcd00000002")
        atomic(first / "messages/asks" / f"{identifier}.json",
               dict(id=identifier, status="withdrawn", deadline=time.time() + 10))
        self.env["CLAIVE_WORKER_ID"] = second.name
        wrong_worker = self.cli("reply", identifier, "--message", "Answer")
        self.assertEqual(wrong_worker.returncode, 1)
        self.assertIn(f"no question {identifier} for this worker", wrong_worker.stderr)
        self.env["CLAIVE_WORKER_ID"] = first.name
        withdrawn = self.cli("reply", identifier, "--message", "Answer")
        self.assertEqual(withdrawn.returncode, 1)
        self.assertIn(f"question {identifier} is withdrawn; the parent no longer waits", withdrawn.stderr)

    def test_busy_unsteered_ask_withdraws_and_never_reaches_next_turn(self):
        _process, worker_id = self.open_worker(mode="slow")
        state = self.until_running(worker_id)
        question = "Unique stale question, do not deliver later."
        result = self.cli("ask", worker_id, "--message", question, "--timeout", "0.4", "--json")
        self.assertEqual(result.returncode, 124, result.stderr)
        ask = json.loads(result.stdout)
        self.assertEqual(ask["status"], "withdrawn")
        self.assertEqual(ask["delivery"]["mode"], "note")
        path = self.registry / worker_id
        self.assertEqual(list((path / "messages/pending").glob("*.json")), [])
        os.killpg(state["worker_pid"], signal.SIGKILL)
        self.until_idle(worker_id)
        self.prompt.write_text("Continue the existing assignment.\nFIXTURE_MODE=success")
        followup = self.cli("followup", worker_id, "--prompt-file", str(self.prompt))
        self.assertEqual(followup.returncode, 0, followup.stderr)
        self.until_idle(worker_id, 2)
        self.assertNotIn(question, self.prompt_for_turn(1))

    def test_expired_note_is_dropped_without_ask_process(self):
        path = self.synthetic("abcd00000001")
        identifier = "a" * 32
        file = path / "messages/pending" / f"000-{identifier}.json"
        atomic(file, dict(id=identifier, message="stale question", expires_at=time.time() - 1))
        atomic(path / "messages/receipts" / f"{identifier}.json", dict(id=identifier, status="queued"))
        ask_file = path / "messages/asks" / f"{identifier}.json"
        atomic(ask_file, dict(id=identifier, status="open"))
        with messaging.lock(path):
            self.assertEqual(messaging.pending_messages(path), [])
        self.assertFalse(file.exists())
        self.assertTrue((path / "messages/withdrawn" / file.name).is_file())
        self.assertEqual(messaging._existing_receipt(path, identifier)["status"], "withdrawn")
        self.assertEqual(json.loads(ask_file.read_text())["status"], "withdrawn")

    def test_wake_expiry_and_stop_are_rechecked_under_message_lock(self):
        for reason in ("expiry", "stop"):
            with self.subTest(reason=reason):
                path = self.synthetic("abcd00000001", "completed")
                state = json.loads((path / "state.json").read_text())
                state.update(ended_at=50, reusable=True, session_id="session", launch={}, schema_version=2)
                atomic(path / "state.json", state)
                atomic(path / "policy.json", dict(reasoning_effort="high", max_model_steps=10))
                request_id = "b" * 32
                stop_file = path / "stopped"
                request = dict(request_id=request_id, prompt_file=str(self.prompt), expires_at=100,
                               stop_file=str(stop_file))
                queued = path / "requests" / f"000-{request_id}.json"
                atomic(queued, request)
                (path / "close.request").unlink(missing_ok=True)
                (path / "responses" / f"{request_id}.json").unlink(missing_ok=True)
                inside = False

                @contextmanager
                def racing_lock(_path):
                    nonlocal inside
                    inside = True
                    if reason == "stop":
                        stop_file.touch()
                    yield

                original_complete = turns.complete

                def complete(*args, **kwargs):
                    original_complete(*args, **kwargs)
                    (path / "close.request").touch()

                engine = mock.Mock()
                with (mock.patch.dict(os.environ, self.env),
                      mock.patch.object(cli, "supervise"), mock.patch.object(cli, "report"),
                      mock.patch.object(cli, "release_worktree"), mock.patch.object(cli, "get_engine", return_value=engine),
                      mock.patch.object(steering, "summary", return_value={}),
                      mock.patch.object(messaging, "lock", racing_lock),
                      mock.patch.object(turns, "complete", side_effect=complete),
                      mock.patch("time.time", side_effect=lambda: 150 if inside and reason == "expiry" else 50)):
                    cli.reusable_worker(path.name)
                engine.build_command.assert_not_called()
                response = json.loads((path / "responses" / f"{request_id}.json").read_text())
                self.assertEqual(response["code"], 124 if reason == "expiry" else 1)
                self.assertEqual(response["error"], "queued follow-up expired before delivery" if reason == "expiry"
                                 else "conversation stopped before delivery")
                self.assertFalse(queued.exists())
                self.assertEqual(json.loads((path / "state.json").read_text())["turn"], 1)

    def test_timeout_bounds(self):
        self.synthetic("abcd00000001")
        for timeout in ("0", "-1", "3600.1", "nan", "inf"):
            result = self.cli("ask", "abcd", "--message", "Question?", "--timeout", timeout)
            self.assertEqual(result.returncode, 1)
            self.assertIn("--timeout must be greater than 0 and at most 3600 seconds", result.stderr)
        for timeout in (0.1, 120, 3600):
            asking.validate_timeout(timeout)


class AskingPollChecks(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.identifier = "a" * 32
        self.steering_id = "b" * 32
        self.request_id = "c" * 32
        self.file = self.path / "messages/asks" / f"{self.identifier}.json"
        self.state = dict(id="abcd00000001", status="running", turn=4)

    def prepare(self, mode, *, delivered=False, deadline=None):
        delivery = dict(mode=mode)
        receipt = dict(id=self.identifier, mode=mode, status="delivered" if delivered else "queued")
        if delivered:
            receipt["turn"] = 4
        if mode == "now":
            delivery["steering_receipt_id"] = self.steering_id
            receipt.update(steering_receipt_id=self.steering_id, turn=4, status="pending")
            steering_record = dict(id=self.steering_id, turn=4, status="pending")
            atomic(self.path / "steering/receipts" / f"{self.steering_id}.json", steering_record)
            atomic(self.path / "steering/requests" / f"000-{self.steering_id}.json",
                   dict(steering_record, message="Question?"))
        elif mode == "wake":
            delivery["turn_request_id"] = self.request_id
            receipt["turn_request_id"] = self.request_id
        else:
            delivery["note"] = self.identifier
        if mode != "now" and not delivered:
            atomic(self.path / "messages/pending" / f"000-{self.identifier}.json",
                   dict(id=self.identifier, message="Question?"))
        record = dict(id=self.identifier, target=self.state["id"], sender="parent (unknown harness)",
                      question="Question?", deadline=deadline or time.time() - 1, created_at=time.time(),
                      delivery=delivery, delivered_turn=None, status="open", answer=None,
                      answer_source=None, ended_at=None)
        atomic(self.file, record)
        messaging._store_receipt(self.path, receipt)
        return receipt

    def run_ask(self, receipt):
        with (mock.patch.object(messaging, "send", return_value=(receipt, True)),
              mock.patch.object(cli, "load", return_value=self.state)):
            return asking.ask(self.path, "Question?", identifier=self.identifier)

    def test_now_timeout_withdraws_unclaimed_request(self):
        receipt = self.prepare("now")
        record, code, notice = self.run_ask(receipt)
        self.assertEqual(code, 124)
        self.assertEqual(record["status"], "withdrawn")
        self.assertEqual(notice, "withdrawn: question never reached the worker")
        self.assertEqual(steering.receipt(self.path, self.steering_id)["status"], "withdrawn")

    def test_claimed_now_and_delivered_note_expire(self):
        for mode in ("now", "note"):
            with self.subTest(mode=mode):
                receipt = self.prepare(mode, delivered=True)
                if mode == "now":
                    request = next((self.path / "steering/requests").glob("*.json"))
                    request.unlink()
                record, code, notice = self.run_ask(receipt)
                self.assertEqual(code, 124)
                self.assertEqual(record["status"], "expired")
                self.assertEqual(record["delivered_turn"], 4)
                self.assertEqual(notice, "expired: question delivered at turn 4, no answer before the deadline")

    def test_wake_timeout_creates_stop_marker_and_withdraws_pending(self):
        receipt = self.prepare("wake")
        record, code, _notice = self.run_ask(receipt)
        self.assertEqual(code, 124)
        self.assertEqual(record["status"], "withdrawn")
        self.assertTrue(self.file.with_suffix(".withdrawn").exists())
        self.assertEqual(messaging._existing_receipt(self.path, self.identifier)["status"], "withdrawn")

    def test_final_text_uses_delivery_turn_after_another_turn_started(self):
        receipt = self.prepare("note", delivered=True, deadline=time.time() + 10)
        self.state.update(turn=5)
        messaging._write_text(self.path / "results/turn-0004.txt", "Answer for turn four")
        messaging._write_text(self.path / "result.txt", "Wrong answer from turn five")
        record, code, notice = self.run_ask(receipt)
        self.assertEqual(code, 0)
        self.assertEqual(record["answer"], "Answer for turn four")
        self.assertEqual(record["answer_source"], "final_text")
        self.assertEqual(notice, "Answer from abcd00000001 (final_text):\nAnswer for turn four")

    def test_needs_parent_and_failed_turn_exit_codes(self):
        receipt = self.prepare("note", delivered=True, deadline=time.time() + 10)
        self.state.update(status="idle", last_turn_status="completed", needs_parent=dict(question="A or B?"))
        messaging._write_text(self.path / "results/turn-0004.txt", "Waiting for a decision")
        _record, code, notice = self.run_ask(receipt)
        self.assertEqual(code, 3)
        self.assertEqual(notice, "A or B?")
        receipt = self.prepare("note", delivered=True, deadline=time.time() + 10)
        self.state.update(last_turn_status="failed", needs_parent=None)
        messaging._write_text(self.path / "results/turn-0004.txt", "")
        _record, code, notice = self.run_ask(receipt)
        self.assertEqual(code, 1)
        self.assertEqual(notice, "turn 4 ended failed with no answer")

    def test_reply_is_exclusive_and_deadline_enforced(self):
        self.prepare("note", deadline=time.time() + 10)
        with mock.patch.object(cli, "load", return_value=self.state):
            asking.reply(self.path, self.identifier, "An explicit answer")
            with self.assertRaisesRegex(ValueError, "already has an answer"):
                asking.reply(self.path, self.identifier, "Duplicate answer")
        answer = json.loads(self.file.with_name(f"{self.identifier}.answer.json").read_text())
        self.assertEqual(answer["answer"], "An explicit answer")
        self.assertEqual(answer["turn"], 4)
        record = json.loads(self.file.read_text())
        record["deadline"] = time.time() - 1
        atomic(self.file, record)
        with self.assertRaisesRegex(ValueError, "is open; the parent no longer waits"):
            asking.reply(self.path, self.identifier, "Late answer")


class SteeringWithdrawalChecks(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.identifier = "c" * 32
        self.request = self.path / "steering/requests" / f"000-{self.identifier}.json"
        record = dict(id=self.identifier, turn=1, status="pending")
        atomic(self.path / "steering/receipts" / f"{self.identifier}.json", record)
        atomic(self.request, dict(record, message="Question?"))
        reader, writer = os.pipe()
        self.reader = reader
        self.addCleanup(os.close, reader)
        stream = os.fdopen(writer, "wb", buffering=0)
        engine = mock.Mock()
        engine.steering_frame.side_effect = lambda identifier, message: message + "\n"
        with mock.patch.object(steering, "get_engine", return_value=engine):
            self.transport = steering.Transport(self.path, dict(engine="fake", turn=1), stream, "")
        self.addCleanup(self.transport.close)

    def test_withdraw_before_pump_skips_delivery(self):
        self.assertTrue(steering.withdraw(self.path, self.identifier))
        self.assertEqual(steering.receipt(self.path, self.identifier)["status"], "withdrawn")
        self.transport.pump()
        self.assertFalse(steering.withdraw(self.path, self.identifier))
        self.assertFalse(self.transport.pending)
        os.set_blocking(self.reader, False)
        with self.assertRaises(BlockingIOError):
            os.read(self.reader, 100)

    def test_withdraw_after_claim_returns_false(self):
        original_read = Path.read_text
        observed = []

        def read(file, *args, **kwargs):
            if file.suffix == ".claimed":
                observed.append(steering.withdraw(self.path, self.identifier))
            return original_read(file, *args, **kwargs)

        with mock.patch.object(Path, "read_text", read):
            self.transport.pump()
        self.assertEqual(observed, [False])
        self.assertEqual(os.read(self.reader, 100), b"Question?\n")
        self.assertFalse(list((self.path / "steering/requests").glob("*.claimed")))

    def test_close_removes_leftover_claims_from_current_turn(self):
        claimed = self.request.with_suffix(".claimed")
        os.rename(self.request, claimed)
        self.transport.close()
        self.assertFalse(claimed.exists())


if __name__ == "__main__":
    unittest.main()
