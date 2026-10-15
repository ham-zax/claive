"""Questions, deadline withdrawal, sender guards and durable turn answers."""
import hermetic  # noqa: F401
from contextlib import contextmanager
import errno
import json
import os
from pathlib import Path
import re
import signal
import time
import unittest
from unittest import mock

from tests import test_messaging
from claivelib import ask_schema, asking, budget, cli, messaging, steering, turns
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
        _sender_process, sender_id = self.open_worker()
        self.until_idle(sender_id)
        self.assertEqual(self.cli("channel", "open", "nested", sender_id, target_id).returncode, 0)
        self.env["CLAIVE_WORKER_ID"] = sender_id
        self.env.pop("CLAIVE_ALLOW_NESTED", None)
        result = self.cli("send", target_id, "--message", "Nested parent context.", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["sender"], f"worker {sender_id} ({receipt['engine']}/{receipt['model']})")
        self.assertEqual(receipt["channel"], "nested")
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

    def test_schema_validates_final_text_and_invalid_schema_never_sends(self):
        schema_file = self.root / "answer-schema.json"
        schema_file.write_text(json.dumps({
            "type": "object",
            "required": ["answer"],
            "properties": {"answer": {"type": "string", "enum": ["ready"]}},
        }))
        text_file = self.root / "answer.txt"
        text_file.write_text('{"answer":"ready"}')
        self.env["FIXTURE_TEXT_FILE"] = str(text_file)
        _process, worker_id = self.open_worker()
        self.until_idle(worker_id)
        result = self.cli("ask", worker_id, "--message", "Return an answer.",
                          "--schema", str(schema_file), "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        ask = json.loads(result.stdout)
        self.assertEqual(ask["answer"], '{"answer":"ready"}')
        self.assertTrue((self.registry / worker_id / "messages/asks" /
                         f"{ask['id']}.schema.json").is_file())
        receipt_count = len(list((self.registry / worker_id / "messages/receipts").glob("*.json")))

        malformed = self.root / "bad-schema.json"
        malformed.write_text('{"type":"not-a-json-type"}')
        rejected = self.cli("ask", worker_id, "--message", "Do not send.",
                            "--schema", str(malformed))
        self.assertEqual(rejected.returncode, 1)
        self.assertIn("$schema.type entries", rejected.stderr)
        self.assertEqual(len(list((self.registry / worker_id / "messages/receipts").glob("*.json"))),
                         receipt_count)

    def test_schema_rejects_invalid_final_text_with_reason(self):
        schema_file = self.root / "answer-schema.json"
        schema_file.write_text('{"type":"object"}')
        text_file = self.root / "answer.txt"
        text_file.write_text("not JSON")
        self.env["FIXTURE_TEXT_FILE"] = str(text_file)
        _process, worker_id = self.open_worker()
        self.until_idle(worker_id)
        result = self.cli("ask", worker_id, "--message", "Return JSON.", "--schema", str(schema_file), "--json")
        self.assertEqual(result.returncode, 1)
        output = json.loads(result.stdout)
        self.assertIn("reply must be valid JSON", output["error"])
        ask_file = (self.registry / worker_id / "messages/asks" / f"{output['id']}.json")
        self.assertEqual(json.loads(ask_file.read_text())["status"], "expired")

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

    def test_schema_rejects_fixture_reply_and_worker_can_retry(self):
        schema_file = self.root / "answer-schema.json"
        schema_file.write_text('{"type":"object","required":["answer"]}')
        output = self.root / "replies.json"
        self.env["FIXTURE_REPLY_OUT"] = str(output)
        _process, worker_id = self.open_worker()
        self.until_idle(worker_id)
        result = self.cli("ask", worker_id, "--message", "Question?\nFIXTURE_MODE=reply",
                          "--schema", str(schema_file))
        self.assertEqual(result.returncode, 1)
        self.assertIn("reply must be valid JSON", result.stdout)
        replies = json.loads(output.read_text())
        self.assertEqual([reply["code"] for reply in replies], [1, 1])
        self.assertTrue(all("reply must be valid JSON" in reply["stderr"] for reply in replies))
        ask_file = next(file for file in (self.registry / worker_id / "messages/asks").glob("*.json")
                        if not file.name.endswith(".schema.json"))
        ask_id = json.loads(ask_file.read_text())["id"]
        self.assertFalse(ask_file.with_name(f"{ask_id}.answer.json").exists())

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
                message_id = "c" * 32
                stop_file = path / "messages" / "asks" / f"{message_id}.withdrawn"
                stop_file.parent.mkdir(parents=True, exist_ok=True)
                request = dict(request_id=request_id, message_id=message_id,
                               prompt_file=str(self.prompt), expires_at=100,
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
                self.assertFalse(stop_file.exists())
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
    def test_wake_answer_survives_an_unrelated_later_budget_pause(self):
        receipt = self.prepare("wake", delivered=True, deadline=time.time() + 30)
        manifest = self.path / "budget-context/turn-0005-resume.json"
        context = dict(version=1, assignment_file=str(self.path / "assignment.md"), handoff_file=None,
                       steering_ids=[], entered_ids=[], turns=[4, 5], recovery_messages=[], turn=5, phase="entered")
        atomic(manifest, context)
        budget._link_turns(self.path, context, manifest)
        paused = dict(kind="budget_limit", question="Old pause.")
        atomic(self.path / "responses" / f"{self.request_id}.json",
               dict(turn=4, status="completed", code=3, needs_parent=paused))
        messaging._write_text(self.path / "results/turn-0005.txt", "Recovered answer.")
        assignment = self.path / "new-task.md"
        assignment.write_text("Unrelated new task.")
        self.state.update(schema_version=2, engine="claude", launch={"budget": {}}, turn=6,
                          status="idle", last_turn_status="completed", prompt_file=str(assignment),
                          needs_parent=dict(kind="budget_limit", question="Unrelated pause."))
        budget.begin_turn(self.path, self.state)
        self.assertEqual(budget.question_context(self.path, self.state, 4), dict(turn=5, phase="entered"))
        self.assertEqual(json.loads(Path(self.state["budget_context_file"]).read_text())["turns"], [6])
        record, code, _notice = self.run_ask(receipt)
        self.assertEqual(code, 0)
        self.assertEqual(record["answer"], "Recovered answer.")
        self.assertEqual(record["delivered_turn"], 5)

    def test_missing_budget_lineage_keeps_a_wake_question_open(self):
        receipt = self.prepare("wake", delivered=True, deadline=time.time() + 30)
        self.state.update(schema_version=2, engine="claude", launch={"budget": {}},
                          budget_lineage_version=1, budget_context_file=str(self.path / "context.json"))
        record, code, notice = self.run_ask(receipt)
        self.assertEqual(code, 3)
        self.assertEqual(record["status"], "open")
        self.assertIn("turn-0004-lineage.json", notice)

    def test_wake_question_follows_budget_recovery_without_accepting_a_handoff(self):
        receipt = self.prepare("wake", delivered=True, deadline=time.time() + 30)
        manifest = self.path / "budget-context/turn-0004-h1.json"
        context = dict(version=1, assignment_file=str(self.path / "assignment.md"), handoff_file=None,
                       steering_ids=[], entered_ids=[], turns=[4], recovery_messages=[], turn=4, phase="staged")
        atomic(manifest, context)
        self.state.update(schema_version=2, engine="claude", launch={"budget": {}}, status="idle",
                          last_turn_status="completed", budget_context_file=str(manifest),
                          needs_parent=dict(kind="budget_limit", question="Continue from saved context."))
        paused_response = dict(turn=4, status="completed", code=3, needs_parent=self.state["needs_parent"])
        atomic(self.path / "responses" / f"{self.request_id}.json", paused_response)
        messaging._write_text(self.path / "results/turn-0004.txt", "[claive-handoff]\nNot the answer.")
        record, code, notice = self.run_ask(receipt)
        self.assertEqual(code, 3)
        self.assertEqual(record["status"], "open")
        self.assertIsNone(record["answer"])
        deadline = record["deadline"]
        self.state.pop("needs_parent")
        self.state.update(turn=5, status="running")
        context.update(turn=5, turns=[4, 5], phase="staged")
        atomic(manifest, context)
        polls = []

        def enter_and_finish(_delay):
            polls.append(True)
            self.assertEqual(json.loads(self.file.read_text())["status"], "open")
            context["phase"] = "entered"
            atomic(manifest, context)
            self.state.update(status="idle", last_turn_status="completed")
            messaging._write_text(self.path / "results/turn-0005.txt", "Actual final answer after recovery.")

        with mock.patch.object(asking.time, "sleep", side_effect=enter_and_finish):
            record, code, notice = self.run_ask(receipt)
        self.assertEqual(code, 0)
        self.assertEqual(len(polls), 1)
        self.assertEqual(record["answer"], "Actual final answer after recovery.")
        self.assertEqual(record["deadline"], deadline)

    def test_unconfirmed_question_waits_through_recovery_and_accepts_a_reply(self):
        receipt = self.prepare("now", deadline=time.time() + 30)
        original_deadline = json.loads(self.file.read_text())["deadline"]
        self.state.update(schema_version=2, engine="claude", launch={"budget": {}}, turn=5)
        filename = self.path / "steering/receipts" / f"{self.steering_id}.json"
        native = json.loads(filename.read_text())
        native.update(status="unconfirmed", continuation=dict(turn=5, phase="staged"))
        atomic(filename, native)
        polls = []

        def enter_and_reply(_delay):
            polls.append(True)
            self.assertEqual(json.loads(self.file.read_text())["status"], "open")
            native["continuation"]["phase"] = "entered"
            atomic(filename, native)
            asking.reply(self.path, self.identifier, "The owed answer is 42.")

        with mock.patch.object(asking.time, "sleep", side_effect=enter_and_reply):
            record, code, _ = self.run_ask(receipt)
        self.assertEqual(code, 0)
        self.assertEqual(len(polls), 1)
        self.assertEqual(record["answer_source"], "reply")
        self.assertEqual(record["delivered_turn"], 5)
        self.assertEqual(record["deadline"], original_deadline)
        self.assertEqual(steering.receipt(self.path, self.steering_id)["status"], "unconfirmed")

    def test_budget_pause_is_guidance_not_an_answer_and_same_id_can_wait_again(self):
        receipt = self.prepare("now", deadline=time.time() + 30)
        self.state.update(schema_version=2, engine="claude", launch={"budget": {}}, status="idle",
                          last_turn_status="completed", needs_parent=dict(kind="budget_limit", question="Resume context."))
        filename = self.path / "steering/receipts" / f"{self.steering_id}.json"
        native = json.loads(filename.read_text())
        native.update(status="not_delivered", continuation=dict(turn=4, phase="staged"))
        atomic(filename, native)
        record, code, notice = self.run_ask(receipt)
        self.assertEqual((code, notice), (3, "Resume context."))
        self.assertEqual(record["status"], "open")
        self.assertIsNone(record["answer"])
        deadline = record["deadline"]
        self.state.pop("needs_parent")
        self.state["turn"] = 5
        native["continuation"].update(turn=5, phase="entered")
        atomic(filename, native)
        messaging._write_text(self.path / "results/turn-0005.txt", "Final owed answer.")
        record, code, _ = self.run_ask(receipt)
        self.assertEqual(code, 0)
        self.assertEqual(record["answer"], "Final owed answer.")
        self.assertEqual(record["deadline"], deadline)

    def test_carried_question_keeps_its_original_answer_deadline(self):
        receipt = self.prepare("now", deadline=time.time() - 1)
        self.state.update(schema_version=2, engine="claude", launch={"budget": {}})
        filename = self.path / "steering/receipts" / f"{self.steering_id}.json"
        native = json.loads(filename.read_text())
        native.update(status="unconfirmed", continuation=dict(turn=4, phase="entered"))
        atomic(filename, native)
        record, code, _ = self.run_ask(receipt)
        self.assertEqual((code, record["status"]), (124, "expired"))
        with self.assertRaisesRegex(ValueError, "no longer waits"):
            asking.reply(self.path, self.identifier, "Too late.")

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
            steering_record = dict(id=self.steering_id, turn=4,
                                   status="acknowledged" if delivered else "pending")
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

    def run_ask(self, receipt, schema=None):
        with (mock.patch.object(messaging, "send", return_value=(receipt, True)),
              mock.patch.object(cli, "load", return_value=self.state)):
            return asking.ask(self.path, "Question?", identifier=self.identifier, schema=schema)

    def test_ask_id_rejects_a_changed_question(self):
        state = dict(id=self.state["id"], status="idle", reusable=True,
                     engine="fixture", model="test-model")
        deadline = time.time() + 10
        with mock.patch.object(cli, "load", return_value=state):
            first, duplicate = messaging.send(self.path, "Original question?", "note", self.identifier,
                                              kind="ask", expires_at=deadline)
            same, same_duplicate = messaging.send(self.path, "Original question?", "note", self.identifier,
                                                  kind="ask", expires_at=deadline)
            with self.assertRaisesRegex(
                    ValueError, f"message ID {self.identifier} was already used for a different message"):
                messaging.send(self.path, "Changed question?", "note", self.identifier,
                               kind="ask", expires_at=deadline)

        self.assertFalse(duplicate)
        self.assertTrue(same_duplicate)
        self.assertEqual(same, first)
        ask = json.loads(self.file.read_text())
        self.assertEqual(ask["question"], "Original question?")

    def test_failed_steering_delivery_removes_ask_and_receipt(self):
        state = dict(id=self.state["id"], status="running", terminal=False,
                     worker_pid=123, turn=4, engine="fake", model="test-model")
        engine = mock.Mock()
        engine.steering_capability.return_value = mock.Mock(supported=True, reason="")
        with (mock.patch.object(cli, "load", return_value=state),
              mock.patch("claivelib.engines.get_engine", return_value=engine),
              mock.patch("claivelib.state.launch_config", return_value={"steering": True}),
              mock.patch.object(steering, "submit", side_effect=RuntimeError("steering transport failed"))):
            with self.assertRaisesRegex(RuntimeError, "steering transport failed"):
                messaging.send(self.path, "Question?", "now", self.identifier,
                               kind="ask", expires_at=time.time() + 10)

        self.assertFalse(self.file.exists())
        self.assertFalse(messaging._receipt_path(self.path, self.identifier).exists())

    def test_finish_error_after_queue_keeps_ask_receipt_for_retry(self):
        state = dict(id=self.state["id"], status="idle", reusable=True,
                     turn=4, engine="fixture", model="test-model")
        queue_calls = []

        def queue(path, prompt_file, **kwargs):
            request_id = f"{len(queue_calls) + 1:032x}"
            queue_calls.append(request_id)
            atomic(path / "requests" / f"000-{request_id}.json",
                   dict(request_id=request_id, message_id=self.identifier,
                        prompt_file=prompt_file))
            return request_id, state

        original_atomic = messaging.atomic
        fail_finish = [True]

        def finish_fails(path, value):
            if (Path(path) == self.file and value.get("delivery")
                    and value["delivery"].get("mode") and fail_finish[0]):
                fail_finish[0] = False
                raise RuntimeError("finish failed after queue")
            return original_atomic(path, value)

        with (mock.patch.object(cli, "load", return_value=state),
              mock.patch.object(turns, "queue", side_effect=queue),
              mock.patch.object(messaging, "atomic", side_effect=finish_fails)):
            with self.assertRaisesRegex(RuntimeError, "finish failed after queue"):
                messaging.send(self.path, "Question?", "wake", self.identifier,
                               kind="ask", expires_at=time.time() + 30)

            stored = messaging._existing_receipt(self.path, self.identifier)
            retried, duplicate = messaging.send(self.path, "Question?", "wake", self.identifier,
                                                kind="ask", expires_at=time.time() + 30)

        self.assertTrue(self.file.exists())
        self.assertTrue(duplicate)
        self.assertEqual(retried, stored)
        self.assertEqual(len(queue_calls), 1)
        self.assertEqual(len(list((self.path / "requests").glob("*.json"))), 1)

    def test_now_timeout_withdraws_unclaimed_request(self):
        receipt = self.prepare("now")
        record, code, notice = self.run_ask(receipt)
        self.assertEqual(code, 124)
        self.assertEqual(record["status"], "withdrawn")
        self.assertEqual(notice, "withdrawn: question never reached the worker")
        self.assertEqual(steering.receipt(self.path, self.steering_id)["status"], "withdrawn")

    def test_now_timeout_after_pump_claim_is_expired(self):
        receipt = self.prepare("now")
        with mock.patch.object(steering, "withdraw", return_value=False):
            record, code, notice = self.run_ask(receipt)

        self.assertEqual(code, 124)
        self.assertEqual(record["status"], "expired")
        self.assertIn("expired: question delivered at turn", notice)
        self.assertEqual(messaging._existing_receipt(self.path, self.identifier)["status"], "pending")

    def test_unacknowledged_steer_does_not_answer_from_its_turn(self):
        for status in ("rejected", "not_delivered", "unconfirmed"):
            with self.subTest(status=status):
                receipt = self.prepare("now", delivered=True)
                steering_record = steering.receipt(self.path, self.steering_id)
                steering_record["status"] = status
                atomic(self.path / "steering/receipts" / f"{self.steering_id}.json", steering_record)
                next((self.path / "steering/requests").glob("*.json")).unlink()
                self.state.update(status="idle", last_turn_status="completed")
                messaging._write_text(self.path / "results/turn-0004.txt", "Unrelated final text")

                record, code, notice = self.run_ask(receipt)

                self.assertEqual(code, 124)
                self.assertEqual(record["status"], "withdrawn")
                self.assertIsNone(record["delivered_turn"])
                self.assertEqual(notice, "withdrawn: question never reached the worker")

    def test_terminal_unacknowledged_steer_withdraws_before_deadline(self):
        for status in ("rejected", "not_delivered", "unconfirmed"):
            with self.subTest(status=status):
                receipt = self.prepare("now", delivered=True, deadline=time.time() + 10)
                steering_record = steering.receipt(self.path, self.steering_id)
                steering_record["status"] = status
                atomic(self.path / "steering/receipts" / f"{self.steering_id}.json", steering_record)
                next((self.path / "steering/requests").glob("*.json")).unlink()
                self.state.update(status="idle", last_turn_status="completed")
                messaging._write_text(self.path / "results/turn-0004.txt", "Unrelated final text")

                with mock.patch.object(
                        asking.time, "sleep",
                        side_effect=AssertionError("ask waited after terminal steer")):
                    record, code, notice = self.run_ask(receipt)

                self.assertEqual(code, 124)
                self.assertEqual(record["status"], "withdrawn")
                self.assertIsNone(record["delivered_turn"])
                self.assertEqual(notice, "withdrawn: question never reached the worker")

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

    def test_wake_timeout_cleans_stop_marker_when_request_is_gone(self):
        receipt = self.prepare("wake")
        record, code, _notice = self.run_ask(receipt)
        self.assertEqual(code, 124)
        self.assertEqual(record["status"], "withdrawn")
        self.assertFalse(self.file.with_suffix(".withdrawn").exists())
        self.assertEqual(messaging._existing_receipt(self.path, self.identifier)["status"], "withdrawn")

    def test_final_text_uses_delivery_turn_after_another_turn_started(self):
        receipt = self.prepare("note", delivered=True, deadline=time.time() + 10)
        self.state.update(turn=5, last_turn_status="completed")
        messaging._write_text(self.path / "results/turn-0004.txt", "Answer for turn four")
        messaging._write_text(self.path / "result.txt", "Wrong answer from turn five")
        record, code, notice = self.run_ask(receipt)
        self.assertEqual(code, 0)
        self.assertEqual(record["answer"], "Answer for turn four")
        self.assertEqual(record["answer_source"], "final_text")
        self.assertEqual(notice, "Answer from abcd00000001 (final_text):\nAnswer for turn four")

    def test_turn_that_starts_and_ends_between_polls_uses_its_event_or_result(self):
        for event_status, expected_code, expected_status in (
                ("completed", 0, "answered"), ("failed", 1, "expired"),
                (None, 0, "answered")):
            with self.subTest(event_status=event_status):
                receipt = self.prepare("note", delivered=True, deadline=time.time() + 10)
                self.state.update(turn=6, status="idle", last_turn_status="completed")
                messaging._write_text(self.path / "results/turn-0004.txt", "Answer for skipped turn")
                event_file = self.path / "inbox.jsonl"
                if event_status is None:
                    event_file.unlink(missing_ok=True)
                else:
                    event_file.write_text(json.dumps(dict(
                        type="turn", id=self.state["id"], turn=4, status=event_status,
                        needs_parent=None)) + "\n")

                with mock.patch.object(cli, "root", return_value=self.path):
                    record, code, notice = self.run_ask(receipt)

                self.assertEqual(code, expected_code, notice)
                self.assertEqual(record["status"], expected_status)
                if expected_status == "answered":
                    self.assertEqual(record["answer"], "Answer for skipped turn")
                else:
                    self.assertEqual(notice, "turn 4 ended failed with no answer")

    def test_invalid_final_text_returns_schema_reason_and_expires_question(self):
        receipt = self.prepare("note", delivered=True, deadline=time.time() + 10)
        schema = {"type": "object", "required": ["answer"]}
        ask_schema.store_schema(self.path, self.identifier, schema)
        self.state.update(status="idle")
        messaging._write_text(self.path / "results/turn-0004.txt", "not JSON")
        record, code, notice = self.run_ask(receipt, schema)
        self.assertEqual(code, 1)
        self.assertIn("reply must be valid JSON", notice)
        self.assertEqual(record["status"], "expired")
        self.assertEqual(record["answer"], "not JSON")
        self.assertEqual(record["answer_source"], "final_text")

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

    def test_report_question_wins_over_failed_turn_status(self):
        receipt = self.prepare("note", delivered=True, deadline=time.time() + 10)
        outcome = dict(status="failed", report_contract=True)
        cli.record_report(outcome,
                          '```claive-report\n{"status":"needs_decision",'
                          '"summary":"Waiting for a decision","question":"A or B?"}\n```')
        self.assertEqual(outcome["status"], "failed")
        self.assertEqual(outcome["needs_parent"]["question"], "A or B?")
        self.state.update(status="idle", last_turn_status=outcome["status"],
                          needs_parent=outcome["needs_parent"])
        (self.path / "inbox.jsonl").write_text(json.dumps(dict(
            type="turn", id=self.state["id"], turn=4, status=outcome["status"],
            needs_parent=outcome["needs_parent"])) + "\n")
        messaging._write_text(self.path / "results/turn-0004.txt", "Waiting on a choice")

        with mock.patch.object(cli, "root", return_value=self.path):
            _record, code, notice = self.run_ask(receipt)

        self.assertEqual(code, 3)
        self.assertEqual(notice, "A or B?")

    def test_noncompleted_turn_partial_text_is_not_an_answer(self):
        receipt = self.prepare("note", delivered=True, deadline=time.time() + 10)
        self.state.update(status="idle", last_turn_status="failed", needs_parent=None)
        messaging._write_text(self.path / "results/turn-0004.txt", "Partial output before failure")

        record, code, notice = self.run_ask(receipt)

        self.assertEqual(code, 1)
        self.assertEqual(record["status"], "expired")
        self.assertIsNone(record["answer"])
        self.assertEqual(notice, "turn 4 ended failed with no answer")

    def test_completed_turn_with_blank_final_text_has_no_answer(self):
        for text in ("", " \n\t "):
            with self.subTest(text=repr(text)):
                receipt = self.prepare("note", delivered=True, deadline=time.time() + 10)
                self.state.update(status="idle", last_turn_status="completed", needs_parent=None)
                messaging._write_text(self.path / "results/turn-0004.txt", text)

                record, code, notice = self.run_ask(receipt)

                self.assertEqual(code, 1)
                self.assertEqual(record["status"], "expired")
                self.assertIsNone(record["answer"])
                self.assertEqual(notice, "turn 4 ended completed with no answer")

    def test_needs_parent_exit_three_wins_over_schema_validation(self):
        receipt = self.prepare("note", delivered=True, deadline=time.time() + 10)
        schema = {"type": "object"}
        ask_schema.store_schema(self.path, self.identifier, schema)
        self.state.update(status="idle", last_turn_status="completed",
                          needs_parent=dict(question="Choose A or B?"))
        messaging._write_text(self.path / "results/turn-0004.txt", "Choose A or B?")
        record, code, notice = self.run_ask(receipt, schema)
        self.assertEqual(code, 3)
        self.assertEqual(notice, "Choose A or B?")
        self.assertEqual(record["status"], "answered")

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

    def test_reply_state_load_failure_leaves_no_answer_file(self):
        self.prepare("note", deadline=time.time() + 10)
        with mock.patch.object(cli, "load", side_effect=RuntimeError("worker state unavailable")):
            with self.assertRaisesRegex(RuntimeError, "worker state unavailable"):
                asking.reply(self.path, self.identifier, "An explicit answer")

        self.assertFalse(self.file.with_name(f"{self.identifier}.answer.json").exists())

    def test_reply_hard_link_error_becomes_value_error(self):
        self.prepare("note", deadline=time.time() + 10)
        with (mock.patch.object(cli, "load", return_value=self.state),
              mock.patch.object(asking.os, "link",
                                side_effect=OSError(errno.EPERM, "Operation not permitted"))):
            with self.assertRaises(ValueError) as raised:
                asking.reply(self.path, self.identifier, "An explicit answer")

        self.assertEqual(str(raised.exception),
                         "cannot record the answer: [Errno 1] Operation not permitted")
        self.assertFalse(self.file.with_name(f"{self.identifier}.answer.json").exists())

    def test_invalid_reply_is_refused_without_answer_file_and_can_be_retried(self):
        self.prepare("note", deadline=time.time() + 10)
        ask_schema.store_schema(self.path, self.identifier,
                                {"type": "object", "required": ["answer"],
                                 "properties": {"answer": {"type": "string"}}})
        answer_file = self.file.with_name(f"{self.identifier}.answer.json")
        with mock.patch.object(cli, "load", return_value=self.state):
            with self.assertRaisesRegex(ValueError, "reply must be valid JSON"):
                asking.reply(self.path, self.identifier, "not JSON")
            self.assertFalse(answer_file.exists())
            asking.reply(self.path, self.identifier, '{"answer":"ready"}')
        answer = json.loads(answer_file.read_text())
        self.assertEqual(answer["answer"], '{"answer":"ready"}')


class AskSchemaValidationChecks(unittest.TestCase):
    def test_supported_schema_keywords_validate_nested_values(self):
        schema = ask_schema.validate_schema({
            "type": "object",
            "required": ["state", "entries"],
            "properties": {
                "state": {"enum": ["ready", "done"]},
                "entries": {"type": "array", "items": {
                    "type": "object",
                    "required": ["count"],
                    "properties": {"count": {"type": "integer"}},
                }},
            },
        })
        ask_schema.validate_reply('{"state":"ready","entries":[{"count":2}]}', schema)
        for reply, reason in (
            ('{"state":"ready"}', "$.entries: required property is missing"),
            ('{"state":"unknown","entries":[]}', "$.state: value is not one of the allowed enum values"),
            ('{"state":"ready","entries":[{"count":"2"}]}', "$.entries[0].count: expected integer, got string"),
        ):
            with self.subTest(reply=reply), self.assertRaisesRegex(ValueError, re.escape(reason)):
                ask_schema.validate_reply(reply, schema)

    def test_bad_schema_and_bad_json_replies_have_clear_errors(self):
        for schema in (
            [],
            {"additionalProperties": False},
            {"type": ["string", "string"]},
            {"required": ["x", 2]},
            {"items": True},
        ):
            with self.subTest(schema=schema), self.assertRaises(ValueError):
                ask_schema.validate_schema(schema)
        with self.assertRaisesRegex(ValueError, "reply must be valid JSON"):
            ask_schema.validate_reply("{broken", {"type": "object"})


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

    def test_expired_request_is_withdrawn_without_provider_write(self):
        request = json.loads(self.request.read_text())
        request["expires_at"] = time.time() - 1
        self.request.write_text(json.dumps(request))

        self.transport.pump()

        self.assertEqual(steering.receipt(self.path, self.identifier)["status"], "withdrawn")
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

    def test_close_removes_leftover_withdrawn_requests_from_current_turn(self):
        self.assertTrue(steering.withdraw(self.path, self.identifier))
        withdrawn = self.request.with_name(self.request.name + ".withdrawn")
        self.assertTrue(withdrawn.exists())

        self.transport.close()

        self.assertFalse(withdrawn.exists())


if __name__ == "__main__":
    unittest.main()
