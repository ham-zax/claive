"""End-to-end duplex steering, distinct from queued reusable turns."""
import hermetic  # noqa: F401
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
from claivelib import cli, steering
from claivelib.engines.pi import PiEngine
from claivelib.state import SCHEMA_VERSION

RPC_FIXTURE = '''#!/usr/bin/python3
import json,sys,time
from pathlib import Path
mode = sys.argv[sys.argv.index('--mode')+1]
def emit(value):
 print(json.dumps(value),flush=True)
if mode != 'rpc':
 emit(dict(type='message_end',message=dict(role='assistant',stopReason='stop',content=[dict(type='text',text='default turn')])))
 emit(dict(type='agent_settled'))
 sys.exit(0)
initial=json.loads(sys.stdin.readline())
emit(dict(type='response',id=initial['id'],command='prompt',success=True))
emit(dict(type='agent_start'))
emit(dict(type='message_start',message=dict(role='assistant')))
if initial['message'] == 'settle':
 emit(dict(type='message_end',message=dict(role='assistant',stopReason='stop',content=[dict(type='text',text='settled')])))
 emit(dict(type='agent_settled'))
 for line in sys.stdin: pass
 sys.exit(0)
for line in sys.stdin:
 request=json.loads(line)
 if request['type']!='steer': continue
 ok=request['message']!='reject'
 if request['message']=='no-ack':
  emit(dict(type='message_end',message=dict(role='assistant',stopReason='stop',content=[dict(type='text',text='unconfirmed')])))
  emit(dict(type='agent_settled'))
  continue
 emit(dict(type='response',id=request['id'],command='steer',success=ok,data=dict(disposition='queued') if ok else None,error=None if ok else 'rejected input'))
 if not ok: continue
 emit(dict(type='message_end',message=dict(role='assistant',stopReason='stop',content=[dict(type='text',text='steered: '+request['message'])])))
 emit(dict(type='agent_settled'))
'''


class DurableSteeringChecks(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name)
        self.state = dict(schema_version=SCHEMA_VERSION, engine="pi", launch=dict(steering=True),
                          status="running", worker_pid=123, turn=1)
        patch = mock.patch.object(cli, "load", return_value=self.state)
        patch.start()
        self.addCleanup(patch.stop)

    def raw(self, identifier):
        return json.loads(self.file(identifier).read_text())

    def file(self, identifier):
        return self.path / "steering" / "receipts" / f"{identifier}.json"

    def record(self, status="pending", turn=1, sequence=1, expiry=None, **fields):
        identifier = f"{len(list((self.path / 'steering' / 'receipts').glob('*.json'))) + 1:032x}"
        record = dict(id=identifier, turn=turn, status=status, created_at=1,
                      payload=dict(message=f"message {identifier}", sequence=sequence, expires_at=expiry))
        record.update(fields)
        steering.atomic(self.file(identifier), record)
        return identifier

    def transport(self):
        # A local pipe exercises the existing framing without a provider or model.
        read_fd, write_fd = os.pipe()
        reader = os.fdopen(read_fd, "rb", buffering=0)
        writer = os.fdopen(write_fd, "wb", buffering=0)
        self.addCleanup(reader.close)
        self.addCleanup(writer.close)
        return steering.Transport(self.path, self.state, writer, ""), reader

    def test_unicode_payload_is_durable_before_request_and_private(self):
        message = "  親の指示 👩🏽‍💻\n café e\u0301\t\n"
        original_atomic = steering.atomic
        writes = []

        def save(path, value):
            if path.parent.name == "requests":
                self.assertEqual(self.raw(value["id"])["payload"]["message"], message)
                self.assertNotIn("payload", value)
            writes.append(path.parent.name)
            original_atomic(path, value)

        with mock.patch.object(steering, "atomic", side_effect=save):
            public = steering.submit(self.path, message, expires_at=123.5, source="peer")
        identifier = public["id"]
        self.assertEqual(writes, ["receipts", "requests"])
        self.assertEqual(self.raw(identifier)["payload"],
                         dict(message=message, sequence=1, expires_at=123.5))
        request = json.loads(next((self.path / "steering" / "requests").glob("*.json")).read_text())
        self.assertEqual(request, dict(public, message=message, expires_at=123.5))
        for output in (public, steering.receipt(self.path, identifier), steering.summary(self.path, self.state)):
            self.assertNotIn("payload", json.dumps(output))
            self.assertNotIn(message, str(output))

    def test_sequences_follow_maximum_in_same_turn_and_ignore_budget(self):
        self.record(sequence=8)
        self.record(sequence=3)
        self.record(turn=2, sequence=100)
        self.record(sequence=500, source="budget")
        self.record(payload=None)  # Legacy metadata does not allocate a sequence.
        first = steering.submit(self.path, "first")
        budget = steering.submit(self.path, "budget", source="budget")
        second = steering.submit(self.path, "second")
        self.assertEqual(self.raw(first["id"])["payload"]["sequence"], 9)
        self.assertEqual(self.raw(second["id"])["payload"]["sequence"], 10)
        self.assertNotIn("payload", self.raw(budget["id"]))
        self.state["turn"] = 3
        third = steering.submit(self.path, "new turn")
        self.assertEqual(self.raw(third["id"])["payload"],
                         dict(message="new turn", sequence=1, expires_at=None))

    def test_payload_and_continuation_survive_update_withdraw_and_close(self):
        first = steering.submit(self.path, "queued")["id"]
        second = steering.submit(self.path, "withdraw")["id"]
        third = self.record(status="sent", sequence=3)
        steering.stage_continuation(self.path, [first, second, third], turn=1, session_id="session",
                                    prompt_file=self.path / "prompt", input_id="input")
        before = {identifier: self.raw(identifier) for identifier in (first, second, third)}
        transport, _ = self.transport()
        transport.update(first, disposition={"queued": True}, reason="waiting")
        self.assertTrue(steering.withdraw(self.path, second))
        transport.close()
        for identifier, status in ((first, "not_delivered"), (second, "withdrawn"), (third, "unconfirmed")):
            raw = self.raw(identifier)
            self.assertEqual(raw["status"], status)
            self.assertEqual(raw["payload"], before[identifier]["payload"])
            self.assertEqual(raw["continuation"], before[identifier]["continuation"])
        self.assertEqual(list((self.path / "steering" / "requests").iterdir()), [])

    def test_transport_preserves_framing_and_acknowledgement(self):
        identifier = steering.submit(self.path, "hello λ")["id"]
        payload = self.raw(identifier)["payload"]
        transport, reader = self.transport()
        transport.pump()
        self.assertEqual(reader.readline(), PiEngine().steering_frame(identifier, "hello λ").encode())
        self.assertEqual(steering.receipt(self.path, identifier)["status"], "sent")
        transport.observe(dict(type="control_response", command="steer", id=identifier, success=True))
        transport.close()
        self.assertEqual(self.raw(identifier)["payload"], payload)
        self.assertEqual(steering.receipt(self.path, identifier)["status"], "acknowledged")

    def test_transport_rejects_each_immutable_field_without_writing(self):
        identifier = self.record()
        transport, _ = self.transport()
        before = self.file(identifier).read_bytes()
        for field in ("id", "turn", "source", "created_at", "payload"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                transport.update(identifier, **{field: self.raw(identifier).get(field)})
            self.assertEqual(self.file(identifier).read_bytes(), before)
        transport.update(identifier, status="sent", reason="sent", disposition="queued", turn_ended=True)
        self.assertEqual(self.raw(identifier)["status"], "sent")

    def test_legacy_metadata_remains_publicly_readable(self):
        identifier = self.record()
        raw = self.raw(identifier)
        del raw["payload"]
        steering.atomic(self.file(identifier), raw)
        self.assertEqual(steering.receipt(self.path, identifier), raw)
        self.assertEqual(steering.summary(self.path, self.state)["receipts"], [raw])
        transport, _ = self.transport()
        transport.update(identifier, status="sent")
        self.assertNotIn("payload", self.raw(identifier))

    def test_status_and_expiry_selection(self):
        expected = []
        for status in ("acknowledged", "sent", "unconfirmed", "pending", "not_delivered", "rejected", "withdrawn"):
            for expiry in (None, 99, 100, 101, 100.5, 10 ** 400):
                identifier = self.record(status=status, expiry=expiry)
                if status in {"acknowledged", "sent", "unconfirmed"} or (
                        status in {"pending", "not_delivered"} and (expiry is None or expiry > 100)):
                    expected.append(identifier)
        self.record(source="budget", status="unknown", payload=None)
        self.assertEqual([record["id"] for record in steering.continuation_records(self.path, 1, now=100)], expected)

    def test_deduplication_order_historical_entry_and_no_receipt_writes(self):
        late = self.record(turn=2, sequence=2)
        early = self.record(turn=2, sequence=1)
        tied = self.record(turn=2, sequence=1)
        historical = self.record(turn=1, sequence=9, status="not_delivered", expiry=1)
        excluded = self.record(turn=1, status="withdrawn", payload=None)
        self.record(turn=1, status="acknowledged")  # Ordinary old turns are not inherited.
        (self.path / ".steering.lock").write_text("unchanged lock")
        before = {file: file.read_bytes() for file in (self.path / "steering" / "receipts").glob("*.json")}
        records = steering.continuation_records(self.path, 2, carried_ids=[late, historical, historical, excluded],
                                                 entered_ids=[historical, excluded], now=100)
        self.assertEqual([record["id"] for record in records], [historical, early, tied, late])
        self.assertTrue(all("payload" in record for record in records))
        self.assertEqual({file: file.read_bytes() for file in before}, before)
        self.assertEqual((self.path / ".steering.lock").read_text(), "unchanged lock")
        self.assertEqual([record["id"] for record in steering.continuation_records(self.path, 2, [historical], now=100)],
                         [early, tied, late])

    def test_staging_does_not_imply_entry_or_override_expiry(self):
        identifier = self.record(status="not_delivered", expiry=1)
        steering.stage_continuation(self.path, [identifier], turn=1, session_id="s", prompt_file="p", input_id="uuid")
        self.assertEqual(steering.continuation_records(self.path, 1, now=100), [])
        self.assertEqual(steering.enter_continuation(self.path, "uuid"), [identifier])
        self.assertEqual(steering.continuation_records(self.path, 1, now=100), [])
        self.assertEqual(len(steering.continuation_records(self.path, 1, entered_ids=[identifier], now=100)), 1)

    def test_missing_receipt_and_eligible_legacy_payload_fail_with_context(self):
        missing = "f" * 32
        with self.assertRaises(ValueError) as caught:
            steering.continuation_records(self.path, 1, [missing])
        self.assertIn(missing, str(caught.exception))
        self.assertIn(str(self.path), str(caught.exception))
        identifier = self.record(status="acknowledged")
        raw = self.raw(identifier)
        del raw["payload"]
        steering.atomic(self.file(identifier), raw)
        with self.assertRaises(ValueError) as caught:
            steering.continuation_records(self.path, 1)
        self.assertIn(identifier, str(caught.exception))
        self.assertIn(str(self.path), str(caught.exception))
        raw["status"] = "rejected"
        steering.atomic(self.file(identifier), raw)
        self.assertEqual(steering.continuation_records(self.path, 1), [])

    def test_default_clock_applies_to_pending_expiry(self):
        identifier = self.record(expiry=100)
        with mock.patch.object(steering.time, "time", return_value=99):
            self.assertEqual([record["id"] for record in steering.continuation_records(self.path, 1)], [identifier])
        with mock.patch.object(steering.time, "time", return_value=100):
            self.assertEqual(steering.continuation_records(self.path, 1), [])

    def test_malformed_eligible_payloads_and_unknown_status_fail(self):
        identifier = self.record(status="sent")
        valid = self.raw(identifier)
        invalid_payloads = [None, [], {}, dict(valid["payload"], message=" \n"),
                            dict(valid["payload"], message=1), dict(valid["payload"], sequence=True),
                            dict(valid["payload"], sequence=0), dict(valid["payload"], sequence=1.5),
                            dict(valid["payload"], expires_at=True), dict(valid["payload"], expires_at="100"),
                            dict(valid["payload"], expires_at=float("inf")),
                            dict(valid["payload"], expires_at=float("nan")),
                            dict(message="text", sequence=1)]
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                steering.atomic(self.file(identifier), dict(valid, payload=payload))
                with self.assertRaises(ValueError) as caught:
                    steering.continuation_records(self.path, 1)
                self.assertIn(identifier, str(caught.exception))
                self.assertIn(str(self.path), str(caught.exception))
        steering.atomic(self.file(identifier), dict(valid, status="surprise"))
        with self.assertRaisesRegex(ValueError, "unknown steering status"):
            steering.continuation_records(self.path, 1)

    def test_stage_and_enter_use_exact_input_id_and_bounded_metadata(self):
        first = self.record(status="acknowledged")
        second = self.record(status="unconfirmed")
        third = self.record()
        before = {identifier: self.raw(identifier) for identifier in (first, second, third)}
        steering.stage_continuation(self.path, [first, first, second], turn=2, session_id="session-2",
                                    prompt_file=self.path / "prompt-2", input_id="uuid-2")
        steering.stage_continuation(self.path, [third], turn=2, session_id="other",
                                    prompt_file="other", input_id="uuid-2-other")
        self.assertEqual(self.raw(first)["continuation"],
                         dict(turn=2, session_id="session-2", prompt_file=str(self.path / "prompt-2"),
                              input_id="uuid-2", phase="staged"))
        self.assertEqual(steering.enter_continuation(self.path, "uuid"), [])
        self.assertEqual(steering.enter_continuation(self.path, "uuid-2"), [first, second])
        after = {identifier: self.raw(identifier) for identifier in (first, second, third)}
        self.assertEqual(steering.enter_continuation(self.path, "uuid-2"), [])
        for identifier in (first, second, third):
            self.assertEqual(self.raw(identifier), after[identifier])
            self.assertEqual(after[identifier]["status"], before[identifier]["status"])
            self.assertEqual(after[identifier]["payload"], before[identifier]["payload"])
        self.assertEqual(after[third]["continuation"]["phase"], "staged")
        steering.stage_continuation(self.path, [first], turn=3, session_id="session-3",
                                    prompt_file="prompt-3", input_id="uuid-3")
        self.assertEqual(len(self.raw(first)["continuation"]), 5)
        self.assertEqual(steering.enter_continuation(self.path, "uuid-2"), [])
        self.assertEqual(steering.enter_continuation(self.path, "uuid-3"), [first])

    def test_stage_missing_receipt_rejects_without_partial_metadata(self):
        identifier = self.record()
        before = self.file(identifier).read_bytes()
        with self.assertRaises(ValueError):
            steering.stage_continuation(self.path, [identifier, "f" * 32], turn=2, session_id="s",
                                        prompt_file="p", input_id="uuid")
        self.assertEqual(self.file(identifier).read_bytes(), before)


class SteeringChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.binary = self.root / "pi"
        self.binary.write_text(RPC_FIXTURE)
        self.binary.chmod(0o755)
        self.prompt = self.root / "prompt"
        self.prompt.write_text("wait for steering")
        self.registry = self.root / "state"
        self.env = dict(os.environ, CLAIVE_DIR=str(self.registry), PI_WORKER_BINARY=str(self.binary))
        self.launcher = str(Path(__file__).resolve().parents[1] / "bin" / "claive")
        self.identifiers = []
        self.addCleanup(self.stop)

    def run_cli(self, *args):
        return subprocess.run([self.launcher, *args], env=self.env, text=True, capture_output=True, timeout=15)

    def stop(self):
        for identifier in self.identifiers:
            self.run_cli("cancel", identifier)
            self.run_cli("wait", identifier, "--timeout", "5")

    def start(self, enabled=True, action="start"):
        command = [action, "--engine", "pi", "--workspace", str(self.root), "--prompt-file", str(self.prompt)]
        if action == "open":
            command.append("--detach")
        if enabled:
            command.append("--enable-steering")
        result = self.run_cli(*command)
        self.assertEqual(result.returncode, 0, result.stderr)
        identifier = re.search(r"Worker ([0-9a-f]{12})", result.stdout)[1]
        self.identifiers.append(identifier)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = json.loads(self.run_cli("show", identifier, "--json").stdout)
            if state.get("last_event") == "message_start" or state["status"] not in {"starting", "running"}:
                return identifier
            time.sleep(0.03)
        self.fail("fixture did not start")

    def test_steer_acknowledged_and_changes_same_turn_result(self):
        identifier = self.start()
        result = self.run_cli("steer", identifier, "--message", "updated task", "--wait", "5", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["status"], "acknowledged")
        self.assertEqual(receipt["turn"], 1)
        self.assertEqual(self.run_cli("wait", identifier, "--timeout", "5").returncode, 0)
        state = json.loads(self.run_cli("show", identifier, "--json").stdout)
        self.assertEqual(state["turn"], 1)
        self.assertEqual(state["queue_depth"], 0)
        self.assertEqual(state["steering"]["acknowledged"], 1)
        self.assertIn("steered: updated task", (self.registry / identifier / "result.txt").read_text())

    def test_rejected_provider_input_is_not_claimed_delivered(self):
        identifier = self.start()
        result = self.run_cli("steer", identifier, "--message", "reject", "--wait", "5", "--json")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["status"], "rejected")
        state = json.loads(self.run_cli("show", identifier, "--json").stdout)
        self.assertEqual(state["status"], "running")
        self.assertEqual(state["queue_depth"], 0)

    def test_steer_message_file_requires_absolute_path(self):
        identifier = self.start()
        result = self.run_cli("steer", identifier, "--message-file", "tests/test_steering.py")

        self.assertEqual(result.returncode, 1)
        self.assertIn("--message-file must be an existing absolute file", result.stderr)

    def test_no_ack_terminal_is_unconfirmed(self):
        identifier = self.start()
        result = self.run_cli("steer", identifier, "--message", "no-ack", "--wait", "5", "--json")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["status"], "unconfirmed")
        self.assertEqual(self.run_cli("wait", identifier, "--timeout", "5").returncode, 0)

    def test_default_transport_remains_print_and_rejects_steering(self):
        self.prompt.write_text("settle")
        identifier = self.start(False)
        state = json.loads(self.run_cli("show", identifier, "--json").stdout)
        self.assertIn("--print", state["command"])
        self.assertFalse(state["steering"]["enabled"])
        self.assertNotEqual(self.run_cli("steer", identifier, "--message", "later").returncode, 0)

    def test_reusable_new_turn_requires_new_steering_receipt(self):
        identifier = self.start(action="open")
        first = self.run_cli("steer", identifier, "--message", "one", "--wait", "5", "--json")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(self.run_cli("wait", identifier, "--timeout", "5").returncode, 0)
        self.assertNotEqual(self.run_cli("steer", identifier, "--message", "idle").returncode, 0)
        self.assertEqual(self.run_cli("followup", identifier, "--prompt-file", str(self.prompt)).returncode, 0)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = json.loads(self.run_cli("show", identifier, "--json").stdout)
            if state["turn"] == 2 and state.get("last_event") == "message_start":
                break
            time.sleep(0.03)
        second = self.run_cli("steer", identifier, "--message", "two", "--wait", "5", "--json")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(json.loads(second.stdout)["turn"], 2)
        self.assertEqual(self.run_cli("wait", identifier, "--timeout", "5").returncode, 0)
        self.assertEqual((self.registry / identifier / "turn-0002.txt").read_text(), "steered: two")

    def test_unsupported_engine_rejects_launch_opt_in(self):
        result = self.run_cli("start", "--engine", "muse", "--enable-steering", "--workspace", str(self.root), "--prompt-file", str(self.prompt))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no", result.stderr.lower())

    def test_help_and_docs_report_steering_exits_and_engine_effort_defaults(self):
        help_result = self.run_cli("start", "--help")
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("xhigh (Claude)", help_result.stdout)

        docs = Path(__file__).resolve().parents[1] / "docs"
        harness = (docs / "harness-integration.md").read_text()
        steering = (docs / "steering.md").read_text()
        messaging = " ".join((docs / "design-messaging-and-roles.md").read_text().split())
        self.assertIn("1 refused or failed turn", harness)
        self.assertIn("not_delivered, and unconfirmed receipts exit 1", steering)
        self.assertIn("`--message-file /abs/instruction.md`", steering)
        self.assertIn("`withdrawn` (request withdrawn before delivery, including expiry)", steering)
        self.assertIn("The steering pump marks an expired steering request `withdrawn` before sending it.", messaging)

    def test_tool_completion_clears_phase_from_real_normalized_pi_event(self):
        state = dict(steps=0, task_failures=0)
        engine = PiEngine()
        for native in (dict(type="tool_execution_start", toolName="bash"), dict(type="tool_execution_end", toolName="bash", isError=False)):
            for event in engine.normalize_event(native):
                cli.event_update(state, event)
        self.assertEqual(state["phase"], "waiting for model")

    def test_overlapping_tools_keep_remaining_tool_active(self):
        state = dict(steps=0, task_failures=0)
        engine = PiEngine()
        for native in (dict(type="tool_execution_start", toolName="build", toolCallId="A"),
                       dict(type="tool_execution_start", toolName="lint", toolCallId="B"),
                       dict(type="tool_execution_end", toolName="build", toolCallId="A", isError=False)):
            for event in engine.normalize_event(native):
                cli.event_update(state, event)
        self.assertEqual(state["phase"], "tool: lint")
        self.assertEqual(state["active_tools"], {"B": "lint"})
        for event in engine.normalize_event(dict(type="tool_execution_end", toolName="lint", toolCallId="B", isError=False)):
            cli.event_update(state, event)
        self.assertEqual(state["phase"], "waiting for model")

    def test_muse_model_completion_does_not_finish_running_tool(self):
        from claivelib.engines.muse import MuseEngine
        state = dict(steps=0, task_failures=0)
        engine = MuseEngine()
        for kind, detail in (("task.lifecycle.proposed", dict(task_kind="tool.bash", task_id="A")),
                             ("task.lifecycle.proposed", dict(task_kind="model.meta.response", task_id="M")),
                             ("task.lifecycle.completed", dict(task_id="M")),
                             ("task.lifecycle.completed", dict(task_id="A"))):
            for event in engine.normalize_event(dict(payload_type=kind, payload=dict(event=detail))):
                cli.event_update(state, event)
            if detail.get("task_id") == "M":
                self.assertEqual(state["active_tools"], {"A": "tool.bash"})
        self.assertEqual(state["active_tools"], {})

    def test_claude_multi_tool_message_preserves_all_tool_ids(self):
        from claivelib.engines.claude import ClaudeEngine
        engine = ClaudeEngine()
        state = dict(steps=0, task_failures=0)
        for event in engine.normalize_event(dict(type="assistant", message=dict(content=[
                dict(type="tool_use", name="Read", id="A"), dict(type="tool_use", name="Bash", id="B")]))):
            cli.event_update(state, event)
        for event in engine.normalize_event(dict(type="user", message=dict(content=[dict(type="tool_result", tool_use_id="A", content="ok")]))):
            cli.event_update(state, event)
        self.assertEqual(state["phase"], "tool: Bash")
