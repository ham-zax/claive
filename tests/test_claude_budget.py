"""Run the real supervisor against a deterministic Claude stream-json process."""
import hermetic  # noqa: F401
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "bin"))
from claivelib import budget, cli, inbox

HANDOFF = "[claive-handoff]\n## Owed to the parent\nnone\n## Goal\nFinish the assignment.\n## Next step\nCheck the next step."
FINAL = 'Done.\n```claive-report\n{"status":"done","summary":"Finished"}\n```'

FIXTURE = '''#!/usr/bin/env python3
import json, os, select, sys, time
from pathlib import Path
root = Path(os.environ["BUDGET_FIXTURE_DIR"])
counter = root / "count"
index = int(counter.read_text()) if counter.exists() else 0
counter.write_text(str(index + 1))
plan = json.loads((root / "plan.json").read_text())[index]
def record(value):
    with (root / "calls.jsonl").open("a") as stream:
        stream.write(json.dumps(dict(session=index, **value)) + "\\n")
def emit(value):
    print(json.dumps(value), flush=True)
def read_user():
    assert select.select([sys.stdin], [], [], 1.5)[0], "stdin message did not arrive"
    frame = json.loads(sys.stdin.readline())
    record(dict(input=frame["message"]["content"]))
    emit(dict(frame, isReplay=True))
    return frame["message"]["content"]
record(dict(command=sys.argv))
emit(dict(type="system", subtype="init"))
if "--input-format" in sys.argv:
    prompt = read_user()
else:
    prompt = sys.argv[-1]
record(dict(prompt=prompt))
for action in plan:
    if "usage" in action:
        content = [dict(type="tool_use", id=action["tool"], name="Read", input={})] if "tool" in action else []
        emit(dict(type="assistant", message=dict(content=content, usage=dict(input_tokens=action["usage"]))))
    elif "gate" in action:
        import subprocess
        settings = json.loads(sys.argv[sys.argv.index("--settings") + 1])
        hook = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        done = subprocess.run(hook, shell=True, capture_output=True, text=True, input=json.dumps(dict(
            hook_event_name="PreToolUse", tool_name="Read", tool_use_id=action["gate"])))
        record(dict(gate=action["gate"], output=done.stdout))
    elif "read" in action:
        for _ in range(action["read"]):
            read_user()
    elif "compact" in action:
        emit(dict(type="system", subtype="compact_boundary"))
    elif "sleep" in action:
        time.sleep(action["sleep"])
    elif "remove_input" in action:
        state_file = next((root / "state").glob("*/state.json"))
        state = json.loads(state_file.read_text())
        manifest = Path(state["budget_context_file"])
        target = (Path(json.loads(manifest.read_text())["assignment_file"])
                  if action["remove_input"] == "assignment" else manifest)
        target.unlink()
    elif "rewrite_task" in action:
        (root / "task.md").write_text(action["rewrite_task"])
    elif "corrupt_receipt" in action:
        for file in (root / "state").glob("*/steering/receipts/*.json"):
            if json.loads(file.read_text()).get("source") != "budget":
                file.write_text("{")
    elif "handoff" in action:
        import subprocess
        state_file = next((root / "state").glob("*/state.json"))
        deadline = time.time() + 5
        # The supervisor must record the last usage before the parent reads it.
        while json.loads(state_file.read_text()).get("context_tokens") != action["tokens"] and time.time() < deadline:
            time.sleep(0.01)
        worker = state_file.parent.name
        env = {key: value for key, value in os.environ.items() if key != "CLAIVE_WORKER_ID"}
        done = subprocess.run([action["launcher"], "handoff", worker, "--note", action["handoff"], "--json"],
                              env=env, capture_output=True, text=True)
        record(dict(handoff=done.returncode, output=done.stdout, error=done.stderr))
    elif "cancel" in action:
        import signal
        os.kill(os.getppid(), signal.SIGTERM)
        time.sleep(2)
    elif "result" in action:
        emit(dict(type="result", subtype=action.get("subtype", "success"), result=action["result"]))
    elif "exit" in action:
        sys.exit(action["exit"])
'''


class BudgetTextChecks(unittest.TestCase):
    def test_handoff_requires_a_nonblank_owed_section(self):
        for body in ("", "\n ", "\n## Next step\nContinue."):
            self.assertIn("Owed", budget.handoff_error("[claive-handoff]\n## Owed to the parent" + body))
        self.assertIsNone(budget.handoff_error(HANDOFF))

    def test_full_inputs_survive_without_truncation(self):
        original = "ASSIGNMENT " + "α" * 50000
        handoff = HANDOFF + "\nEND_OF_HANDOFF"
        prompt = budget.continuation_prompt(handoff, original)
        self.assertIn(original, prompt)
        self.assertIn(handoff, prompt)
        self.assertLess(prompt.index(original), prompt.index(handoff))
        self.assertIn("Re-check inexpensive claims", prompt)
        self.assertIn("A handoff or hidden reasoning is not delivery", prompt)

    def test_inbox_formats_budget_handoff_as_one_trimmed_line(self):
        event = inbox.budget_event("123456789abc", "worker\nlabel", 3, 2, 5, 87999, "session", "  Check\n next step.  ")
        self.assertEqual(inbox.format_text([event]),
                         "123456789abc worker label | turn 3 | handoff 2 (total 5) | context 87k | next step: Check next step.")
        event["next_step"] = "x" * 350
        self.assertTrue(inbox.format_text([event]).endswith("next step: " + "x" * 300))

    def test_handoff_marker_is_the_exact_first_nonblank_line(self):
        self.assertTrue(budget.is_handoff("\n \n" + HANDOFF))
        self.assertTrue(budget.is_handoff(" [claive-handoff]"))
        self.assertTrue(budget.is_handoff("[claive-handoff] "))
        for text in ("", "prefix\n" + HANDOFF, "[claive-handoff] extra"):
            with self.subTest(text=text):
                self.assertFalse(budget.is_handoff(text))

    def test_handoff_marker_accepts_surrounding_whitespace(self):
        self.assertTrue(budget.is_handoff("\n\t\n \t[claive-handoff]\t \n## Next step\nContinue."))

    def test_late_marker_line_reports_only_an_exact_later_line(self):
        self.assertIsNone(budget.late_marker_line("\n \n" + HANDOFF))
        self.assertEqual(budget.late_marker_line("first line\nsecond line\n\n[claive-handoff]"), 4)
        self.assertIsNone(budget.late_marker_line("No marker here."))
        self.assertIsNone(budget.late_marker_line("see [claive-handoff] here"))
        self.assertEqual(budget.late_marker_line("preface\n  [claive-handoff]  "), 2)

    def test_next_step_body_is_stripped_and_limited(self):
        self.assertEqual(budget.next_step(HANDOFF + "\n\n## Later\nIgnore"), "Check the next step.")
        self.assertEqual(budget.next_step("## Next step\n\n  " + "x" * 350), "x" * 300)
        self.assertEqual(budget.next_step("## Goal\nNothing"), "")

    def test_message_texts_match_the_spec(self):
        spec = (REPO / "docs/design-budget-mode.md").read_text()
        def block_after(label):
            return spec.split(label, 1)[1].split("```", 2)[1].strip("\n")
        self.assertEqual(budget.TEMPLATE, block_after("Template:"))
        values = {"stop": 95000}
        self.assertEqual(budget.warn_message(85999, values), block_after("Warn:").format(
            k=85, stop_k=95, template=budget.TEMPLATE))
        self.assertEqual(budget.stop_message(95999, values, False), block_after("Stop:").format(k=95, stop_k=95))
        self.assertEqual(budget.stop_message(95000, values, True),
                         budget.stop_message(95000, values, False) + "\n\n" + budget.TEMPLATE)
        continuation = block_after("Continuation prompt (`<job>/prompt-NNNN-hK.md`):")
        self.assertEqual(budget.continuation_prompt(HANDOFF, "Task"),
                         continuation.replace("{handoff text}", HANDOFF).replace("{original turn prompt}", "Task"))
        self.assertEqual(budget.answer_prompt(HANDOFF, "Continue"),
                         f"{budget.PREAMBLE}\n\n{HANDOFF}\n\n---\nParent message:\nContinue")


class BudgetGateChecks(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="claive-gate-")
        self.addCleanup(temp.cleanup)
        self.job = Path(temp.name)

    def test_missing_gate_file_allows_at_once(self):
        started = time.monotonic()
        self.assertIsNone(budget.gate_reason(self.job, "t1"))
        self.assertLess(time.monotonic() - started, 0.5)

    def test_seen_call_without_stop_is_allowed(self):
        budget.write_gate(self.job, None, ["t1"])
        self.assertIsNone(budget.gate_reason(self.job, "t1"))

    def test_unseen_call_waits_then_allows(self):
        budget.write_gate(self.job, None, [])
        started = time.monotonic()
        self.assertIsNone(budget.gate_reason(self.job, "t1", wait=0.2))
        self.assertGreaterEqual(time.monotonic() - started, 0.2)

    def test_stop_denies_any_call(self):
        budget.write_gate(self.job, "Stop now.", ["t1"])
        self.assertEqual(budget.gate_reason(self.job, "t2", wait=0), "Stop now.")
        output = json.loads(budget.gate_output(self.job, json.dumps({"tool_use_id": "t1"})))
        self.assertEqual(output["hookSpecificOutput"]["permissionDecisionReason"], "Stop now.")

    def test_seen_ids_are_bounded(self):
        budget.write_gate(self.job, None, [f"t{index}" for index in range(budget.GATE_IDS + 10)])
        seen = json.loads((self.job / budget.GATE_FILE).read_text())["seen"]
        self.assertEqual(len(seen), budget.GATE_IDS)
        self.assertEqual(seen[-1], f"t{budget.GATE_IDS + 9}")

    def test_hook_input_without_tool_id_follows_the_stop_state_without_waiting(self):
        budget.write_gate(self.job, None, [])
        started = time.monotonic()
        self.assertEqual(budget.gate_output(self.job, "not json"), "")
        self.assertLess(time.monotonic() - started, 0.5)
        budget.write_gate(self.job, "Stop now.", [])
        self.assertIn("Stop now.", budget.gate_output(self.job, "not json"))


class ClaudeBudgetChecks(unittest.TestCase):
    def _retained_message(self, mode):
        from claivelib import asking, messaging, steering

        question = "AUDIT_OWED_QUESTION: what is six times seven?"
        final = "AUDIT_OWED_ANSWER: 42.\n" + FINAL
        self.plan([{"read": 1}, {"result": HANDOFF}], [{"result": HANDOFF}], [{"result": final}])
        self.create("--report")
        submitted = []
        original_pump = steering.Transport.pump

        def pump(transport):
            if not submitted:
                if mode == "steer":
                    submitted.append(steering.submit(self.path, question))
                else:
                    receipt, _ = messaging.send(self.path, question, "now", kind=mode,
                                                expires_at=time.time() + 120)
                    submitted.append(receipt)
            return original_pump(transport)

        with patch.object(steering.Transport, "pump", pump):
            code, state = self.supervise()
        self.assertEqual(code, 0, state)
        self.assertEqual(state["handoffs"], 2)
        prompts = self.calls("prompt")
        self.assertEqual(len(prompts), 3)
        for prompt in prompts[1:]:
            self.assertEqual(prompt.count(question), 1)
            self.assertEqual(prompt.count(HANDOFF), 1)
            self.assertEqual(prompt.count("Original assignment:"), 1)
        self.assertIn("AUDIT_OWED_ANSWER: 42", (self.path / "result.txt").read_text())
        identifier = submitted[0].get("steering_receipt_id", submitted[0]["id"])
        receipt = steering.receipt(self.path, identifier)
        self.assertEqual(receipt["status"], "acknowledged")
        self.assertEqual(receipt["continuation"]["phase"], "entered")
        self.assertNotIn("payload", receipt)
        if mode != "steer":
            self.assertIn("<parent-message>", prompts[-1])
            self.assertIn(submitted[0]["id"], prompts[-1])
        if mode == "ask":
            record, answer_code, _ = asking.ask(self.path, question, identifier=submitted[0]["id"])
            self.assertEqual(answer_code, 0)
            self.assertEqual(record["status"], "answered")
            self.assertIn("AUDIT_OWED_ANSWER: 42", record["answer"])

    def test_acknowledged_steering_survives_two_model_handoffs(self):
        self._retained_message("steer")

    def test_message_accepted_at_close_survives_entry_and_later_delivery_expiry(self):
        from claivelib import steering

        question = "PENDING_AT_BOUNDARY stays in history."
        self.plan([{"result": HANDOFF}], [{"result": HANDOFF}], [{"result": "done"}])
        self.create()
        submitted = []
        original_update = cli.event_update
        original_records = steering.continuation_records
        original_observe = budget.observe_entry
        entered = []

        def update(state, event):
            if event["type"] == "terminal_completed" and not submitted:
                submitted.append(steering.submit(self.path, question, expires_at=time.time() + 120))
            return original_update(state, event)

        def observe(path, state, event):
            result = original_observe(path, state, event)
            if state.get("budget_input_id") and event.get("id") == state["budget_input_id"]:
                entered.append(True)
            return result

        def records(path, turn, carried_ids=(), entered_ids=(), now=None):
            self.assertEqual(cli.load(path)["status"], "running")
            return original_records(path, turn, carried_ids, entered_ids,
                                    now=time.time() + 3600 if entered else now)

        with (patch.object(cli, "event_update", update), patch.object(budget, "observe_entry", observe),
              patch.object(steering, "continuation_records", records)):
            code, state = self.supervise()
        self.assertEqual(code, 0, state)
        self.assertEqual(state["handoffs"], 2)
        receipt = steering.receipt(self.path, submitted[0]["id"])
        self.assertEqual(receipt["status"], "not_delivered")
        self.assertEqual(receipt["continuation"]["phase"], "entered")
        self.assertTrue(all(prompt.count(question) == 1 for prompt in self.calls("prompt")[1:]))

    def test_mid_turn_message_keeps_sender_wrapper_across_handoffs(self):
        self._retained_message("send")

    def test_ask_is_answered_by_the_final_result_after_handoffs(self):
        self._retained_message("ask")

    def test_missing_owed_section_pauses_without_accepting_the_handoff(self):
        raw = "[claive-handoff]\n## Next step\nContinue."
        self.plan([{"result": raw}])
        self.create("--report")
        with patch.object(cli, "record_report", wraps=cli.record_report) as report:
            code, state = self.supervise()
        self.assertEqual(code, 3)
        self.assertEqual(state["needs_parent"]["kind"], "budget_recovery")
        self.assertEqual(state["handoffs"], 0)
        self.assertEqual(len(self.calls("command")), 1)
        self.assertEqual(report.call_count, 0)
        self.assertEqual((self.path / "handoffs/turn-0001-1.md").read_text(), raw)

    def test_missing_frozen_assignment_requests_recovery_before_a_new_session(self):
        self.plan([{"remove_input": "assignment"}, {"result": HANDOFF}])
        self.create()
        code, state = self.supervise()
        self.assertEqual(code, 3, state)
        self.assertEqual(state["needs_parent"]["kind"], "budget_recovery")
        self.assertIn("assignment.md", state["needs_parent"]["question"])
        self.assertEqual(len(self.calls("command")), 1)

    def test_missing_manifest_requests_recovery_before_a_new_session(self):
        self.plan([{"remove_input": "manifest"}, {"result": HANDOFF}])
        self.create()
        code, state = self.supervise()
        self.assertEqual(code, 3, state)
        self.assertEqual(state["needs_parent"]["kind"], "budget_recovery")
        self.assertEqual(len(self.calls("command")), 1)

    def test_corrupt_receipt_is_recovery_not_a_provider_protocol_error(self):
        from claivelib import steering

        self.plan([{"read": 1}, {"corrupt_receipt": True}, {"result": HANDOFF}])
        self.create()
        submitted = []
        original_pump = steering.Transport.pump

        def pump(transport):
            if not submitted:
                submitted.append(steering.submit(self.path, "Preserve the pending parent direction."))
            return original_pump(transport)

        with patch.object(steering.Transport, "pump", pump):
            code, state = self.supervise()
        self.assertEqual(code, 3, state)
        self.assertEqual(state["needs_parent"]["kind"], "budget_recovery")
        self.assertIn(submitted[0]["id"], state["needs_parent"]["question"])
        self.assertEqual(state["malformed_events"], 0)
        self.assertEqual(len(self.calls("command")), 1)

    def test_original_assignment_is_frozen_before_the_model_runs(self):
        self.plan([{"rewrite_task": "A changed file is not the original assignment."}, {"result": HANDOFF}],
                  [{"result": "done"}])
        original = self.prompt.read_text()
        self.create()
        code, state = self.supervise()
        self.assertEqual(code, 0, state)
        self.assertEqual(self.calls("prompt")[0], original)
        self.assertIn(original, self.calls("prompt")[1])
        self.assertNotIn(self.prompt.read_text(), self.calls("prompt")[1])

    def test_stopping_reply_with_late_marker_records_budget_warning(self):
        reply = "A preface.\nMore detail.\n\n[claive-handoff]\n## Next step\nContinue."
        self.plan([{"usage": 100000}, {"read": 1}, {"result": reply}])
        self.create()
        code, state = self.supervise()
        warning = budget.LATE_MARKER.format(line=4)
        self.assertEqual(code, 3, state)
        self.assertEqual(state["needs_parent"]["kind"], "budget_recovery")
        self.assertEqual(state["budget_phase"], "stopping")
        self.assertEqual(state["budget_warning"], warning)
        self.assertEqual((state["handoffs"], state["budget_missed"]), (0, 0))
        shown = subprocess.run([str(REPO / "bin/claive"), "show", state["id"]], env=self.env,
                               text=True, capture_output=True, timeout=5)
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertIn(f"Budget warning: {warning}\n", shown.stdout)
        shown_json = subprocess.run([str(REPO / "bin/claive"), "show", state["id"], "--json"], env=self.env,
                                    text=True, capture_output=True, timeout=5)
        self.assertEqual(shown_json.returncode, 0, shown_json.stderr)
        self.assertEqual(json.loads(shown_json.stdout)["budget_warning"], warning)

    def test_stopping_reply_without_marker_does_not_record_budget_warning(self):
        self.plan([{"usage": 100000}, {"read": 1}, {"result": "The worker stopped without a marker."}])
        self.create()
        code, state = self.supervise()
        self.assertEqual(code, 0, state)
        self.assertEqual(state["budget_phase"], "stopping")
        self.assertNotIn("budget_warning", state)

    def test_late_marker_below_stop_limit_does_not_record_budget_warning(self):
        self.plan([{"usage": 90000}, {"read": 1}, {"result": "A preface.\n[claive-handoff]"}])
        self.create()
        code, state = self.supervise()
        self.assertEqual(code, 0, state)
        self.assertEqual(state["budget_phase"], "warned")
        self.assertNotIn("budget_warning", state)

    def test_show_budget_on_after_handoff_in_text_and_json(self):
        self.plan([{"result": HANDOFF}], [{"usage": 92999}, {"read": 1}, {"result": "done"}])
        self.create()
        code, state = self.supervise()
        self.assertEqual(code, 0, state)
        text = subprocess.run([str(REPO / "bin/claive"), "show", state["id"]], env=self.env,
                              text=True, capture_output=True, timeout=5)
        self.assertEqual(text.returncode, 0, text.stderr)
        lines = text.stdout.splitlines()
        index = next(index for index, line in enumerate(lines) if line.startswith("Live steering:"))
        self.assertEqual(lines[index], "Live steering: True | pending 0 | acknowledged 0")
        self.assertEqual(lines[index + 1], "Context budget: on | context 92k | warn 90k | stop 100k | "
                         "compact 120k | handoffs 1 (this turn 1) | missed 0")
        shown = subprocess.run([str(REPO / "bin/claive"), "show", state["id"], "--json"], env=self.env,
                               text=True, capture_output=True, timeout=5)
        self.assertEqual(shown.returncode, 0, shown.stderr)
        data = json.loads(shown.stdout)
        self.assertEqual(data["budget"], {"enabled": True, "values": state["launch"]["budget"], "off_reason": None})
        self.assertEqual((data["context_tokens"], data["handoffs"], data["turn_handoffs"], data["budget_missed"]),
                         (92999, 1, 1, 0))
        self.assertIsNone(data["pending_handoff"])
        self.assertEqual((data["steering"]["pending"], data["steering"]["acknowledged"], data["steering"]["budget"]),
                         (0, 0, 1))

    def test_show_output_schema_budget_off_in_text_and_json(self):
        self.plan([{"result": "done"}])
        schema = self.workspace / "schema.json"
        schema.write_text('{"type": "object"}')
        self.create("--output-schema", str(schema))
        code, state = self.supervise()
        self.assertEqual(code, 0, state)
        text = subprocess.run([str(REPO / "bin/claive"), "show", state["id"]], env=self.env,
                              text=True, capture_output=True, timeout=5)
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertIn("Live steering: False | pending 0 | acknowledged 0\nContext budget: off (output schema)\n",
                      text.stdout)
        shown = subprocess.run([str(REPO / "bin/claive"), "show", state["id"], "--json"], env=self.env,
                               text=True, capture_output=True, timeout=5)
        self.assertEqual(shown.returncode, 0, shown.stderr)
        data = json.loads(shown.stdout)
        self.assertEqual(data["budget"], {"enabled": False, "values": None, "off_reason": "output schema"})
        self.assertEqual([data[key] for key in ("context_tokens", "handoffs", "turn_handoffs", "budget_missed")],
                         [0, 0, 0, 0])
        self.assertIsNone(data["pending_handoff"])
        self.assertFalse(data["steering"]["enabled"])

    def test_one_shot_limit_has_no_pending_handoff_and_says_start_a_new_worker(self):
        self.plan([{"result": HANDOFF}], [{"result": HANDOFF}])
        state = self.create()
        state["launch"]["budget"]["max_handoffs"] = 1
        cli.save(self.path / "state.json", state)
        code, state = self.supervise()
        self.assertEqual(code, 3, state)
        self.assertIsNone(state.get("pending_handoff"))
        self.assertEqual(state["needs_parent"]["kind"], "budget_limit")
        handoff_file = self.path / "handoffs/turn-0001-2.md"
        self.assertIn(str(handoff_file), state["needs_parent"]["question"])
        self.assertIn("Start a new worker from the saved context", state["needs_parent"]["question"])
        shown = subprocess.run([str(REPO / "bin/claive"), "show", state["id"]], env=self.env,
                               text=True, capture_output=True, timeout=5)
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertNotIn("Pending handoff:", shown.stdout)

    def test_show_pre_budget_worker_has_defaults_and_launch_reason(self):
        state = self.create()
        state["launch"].pop("budget")
        state["launch"].pop("budget_off_reason")
        cli.save(self.path / "state.json", state)
        shown = subprocess.run([str(REPO / "bin/claive"), "show", state["id"]], env=self.env,
                               text=True, capture_output=True, timeout=5)
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertIn("Context budget: off (not set at launch)\n", shown.stdout)
        shown = subprocess.run([str(REPO / "bin/claive"), "show", state["id"], "--json"], env=self.env,
                               text=True, capture_output=True, timeout=5)
        self.assertEqual(shown.returncode, 0, shown.stderr)
        data = json.loads(shown.stdout)
        self.assertEqual(data["budget"], {"enabled": False, "values": None, "off_reason": None})
        self.assertEqual([data[key] for key in ("context_tokens", "handoffs", "turn_handoffs", "budget_missed")],
                         [0, 0, 0, 0])
        self.assertIsNone(data["pending_handoff"])

    def test_other_engines_have_no_budget_enrichment_or_report_line(self):
        state = self.create()
        state["engine"] = "codex"
        cli.save(self.path / "state.json", state)
        self.assertIsNone(cli.load(self.path)["budget"])
        shown = subprocess.run([str(REPO / "bin/claive"), "show", state["id"]], env=self.env,
                               text=True, capture_output=True, timeout=5)
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertNotIn("Context budget:", shown.stdout)

    def test_steering_summary_separates_budget_and_parent_receipts_for_current_turn(self):
        state = self.create()
        directory = self.path / "steering/receipts"
        directory.mkdir(parents=True)
        records = [{"turn": 1, "status": status, **source}
                   for source in ({}, {"source": "parent"}, {"source": "budget"})
                   for status in ("pending", "sent", "acknowledged", "not_delivered")]
        records.append({"turn": 2, "status": "acknowledged", "source": "budget"})
        for index, record in enumerate(records):
            (directory / f"{index:032x}.json").write_text(json.dumps(record))
        summary = cli.steering.summary(self.path, state)
        self.assertEqual((summary["pending"], summary["acknowledged"], summary["budget"]), (4, 2, 4))
        self.assertEqual(len(summary["receipts"]), 12)

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="claive-budget-")
        self.addCleanup(temp.cleanup)
        self.workspace = Path(temp.name)
        self.binary = self.workspace / "claude"
        self.binary.write_text(FIXTURE)
        self.binary.chmod(0o755)
        self.prompt = self.workspace / "task.md"
        self.prompt.write_text("Complete the original assignment.")
        self.registry = self.workspace / "state"
        self.env = dict(os.environ, CLAIVE_DIR=str(self.registry), CLAUDE_WORKER_BINARY=str(self.binary),
                        CLAUDE_CONFIG_DIR=str(self.workspace / "claude-config"),
                        CLAIVE_CONFIG=str(self.workspace / "config.json"), BUDGET_FIXTURE_DIR=str(self.workspace))
        env_patch = patch.dict(os.environ, self.env)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        previous_launcher = cli.SCRIPT
        cli.set_launcher_path(REPO / "bin/claive")
        self.addCleanup(setattr, cli, "SCRIPT", previous_launcher)
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            self.addCleanup(signal.signal, sig, signal.getsignal(sig))

    def plan(self, *sessions):
        (self.workspace / "plan.json").write_text(json.dumps(sessions))

    def create(self, *extra):
        args = cli.parser().parse_args(["run", "--engine", "claude", "--workspace", str(self.workspace),
                                        "--prompt-file", str(self.prompt), *extra])
        self.path, state = cli.create_job(args)
        return state

    def supervise(self):
        code = cli.supervise(self.path.name)
        return code, json.loads((self.path / "state.json").read_text())

    def calls(self, key):
        return [record[key] for record in map(json.loads, (self.workspace / "calls.jsonl").read_text().splitlines())
                if key in record]

    def receipts(self):
        return [json.loads(file.read_text()) for file in (self.path / "steering/receipts").glob("*.json")]

    def test_warn_and_stop_arrive_once_with_budget_receipts(self):
        self.plan([{"usage": 90000}, {"usage": 91000}, {"read": 1},
                   {"usage": 100000}, {"usage": 101000}, {"read": 1}, {"result": "done"}])
        self.create()
        code, state = self.supervise()
        self.assertEqual(code, 0, state)
        messages = self.calls("input")[1:]
        self.assertEqual(messages, [budget.warn_message(90000, state["launch"]["budget"]),
                                    budget.stop_message(100000, state["launch"]["budget"], False)])
        receipts = self.receipts()
        self.assertEqual(len(receipts), 2)
        self.assertTrue(all(item["source"] == "budget" and item["status"] == "acknowledged" for item in receipts))
        self.assertEqual((state["context_tokens"], state["budget_phase"]), (101000, "stopping"))
        self.assertNotIn("budget_error", state)

    def test_non_haiku_warns_and_stops_at_later_limits_then_returns_final(self):
        self.plan([{"usage": 139999}, {"usage": 140000}, {"read": 1},
                   {"usage": 149999}, {"usage": 150000}, {"read": 1}, {"result": "Final answer."}])
        self.create("--model", "claude-sonnet-5-5")
        code, state = self.supervise()
        self.assertEqual(code, 0)
        messages = self.calls("input")[1:]
        self.assertEqual(messages, [budget.warn_message(140000, state["launch"]["budget"]),
                                    budget.stop_message(150000, state["launch"]["budget"], False)])
        self.assertEqual(state["launch"]["budget"]["compact"], 175000)
        self.assertEqual(state["handoffs"], 0)
        self.assertEqual(len(self.calls("prompt")), 1)
        self.assertEqual((self.path / "result.txt").read_text(), "Final answer.")

    def test_jump_to_stop_carries_template_without_warn(self):
        self.plan([{"usage": 100000}, {"read": 1}, {"result": "done"}])
        self.create()
        code, state = self.supervise()
        self.assertEqual(code, 0, state)
        self.assertEqual(self.calls("input")[1:], [budget.stop_message(100000, state["launch"]["budget"], True)])
        self.assertEqual(len(self.receipts()), 1)

    def gates(self):
        return {record["gate"]: json.loads(record["output"]) if record["output"].strip() else None
                for record in map(json.loads, (self.workspace / "calls.jsonl").read_text().splitlines())
                if "gate" in record}

    @staticmethod
    def deny(reason):
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                       "permissionDecisionReason": reason}}

    def test_stop_on_a_tool_call_is_denied_by_the_gate_and_sent_on_stdin(self):
        self.plan([{"usage": 50000, "tool": "t1"}, {"gate": "t1"},
                   {"usage": 100000, "tool": "t2"}, {"gate": "t2"}, {"gate": "t3"}, {"read": 1},
                   {"result": "done"}])
        self.create()
        code, state = self.supervise()
        self.assertEqual(code, 0, state)
        message = budget.stop_message(100000, state["launch"]["budget"], True)
        deny = self.deny(budget.gate_deny_message(100000, state["launch"]["budget"]))
        self.assertEqual(self.gates(), {"t1": None, "t2": deny, "t3": deny})
        self.assertNotIn("Stop now", deny["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertEqual(self.calls("input")[1:], [message])
        self.assertEqual(state["budget_phase"], "stopping")

    def stop_with_broken_gate(self, write_gate):
        self.plan([{"usage": 100000, "tool": "t1"}, {"gate": "t1"}, {"read": 1}, {"result": "done"}])
        self.create()
        with patch.object(budget, "write_gate", side_effect=write_gate):
            code, state = self.supervise()
        self.assertEqual(code, 0, state)
        self.assertEqual(self.gates(), {"t1": None})
        self.assertEqual(self.calls("input")[1:], [budget.stop_message(100000, state["launch"]["budget"], True)])
        return state

    def test_gate_timeout_allows_the_call_and_stdin_still_carries_stop(self):
        write = budget.write_gate

        def slow(job_dir, stop, seen):
            if stop:
                time.sleep(budget.GATE_WAIT_SECONDS + 0.4)
            write(job_dir, stop, seen)
        self.stop_with_broken_gate(slow)

    def test_gate_write_failure_allows_the_call_and_stdin_still_carries_stop(self):
        write = budget.write_gate

        def failing(job_dir, stop, seen):
            if stop:
                raise OSError("disk full")
            write(job_dir, stop, seen)
        state = self.stop_with_broken_gate(failing)
        self.assertEqual(state["budget_error"], "disk full")

    def test_warn_on_a_tool_call_still_arrives_on_stdin(self):
        self.plan([{"usage": 90000, "tool": "t1"}, {"gate": "t1"}, {"read": 1}, {"result": "done"}])
        self.create()
        code, state = self.supervise()
        self.assertEqual(code, 0, state)
        self.assertEqual(self.gates(), {"t1": None})
        self.assertEqual(self.calls("input")[1:], [budget.warn_message(90000, state["launch"]["budget"])])

    def test_gate_opens_after_compaction_and_in_the_next_session(self):
        self.plan([{"usage": 100000, "tool": "t1"}, {"gate": "t1"}, {"compact": True},
                   {"usage": 50000, "tool": "t2"}, {"gate": "t2"}, {"usage": 100000, "tool": "t3"},
                   {"gate": "t3"}, {"result": HANDOFF}],
                  [{"usage": 10000, "tool": "t4"}, {"gate": "t4"}, {"result": "done"}])
        self.create()
        code, state = self.supervise()
        self.assertEqual(code, 0, state)
        gates = self.gates()
        self.assertEqual((gates["t2"], gates["t4"]), (None, None))
        self.assertEqual(gates["t1"]["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(gates["t3"]["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(state["handoffs"], 1)

    def test_handoff_changes_session_and_checks_only_final_report(self):
        self.plan([{"usage": 90000}, {"read": 1}, {"result": HANDOFF}],
                  [{"usage": 100000}, {"read": 1}, {"result": FINAL}])
        initial = self.create("--report")
        original = Path(initial["prompt_file"]).read_text()
        with patch.object(cli, "record_report", wraps=cli.record_report) as report:
            code, state = self.supervise()
        self.assertEqual(code, 0, state)
        self.assertEqual(report.call_count, 1)
        self.assertEqual(report.call_args.args[1], FINAL)
        self.assertEqual(state["report_state"], "ok")
        commands = self.calls("command")
        sessions = [command[command.index("--session-id") + 1] for command in commands]
        self.assertEqual(len(set(sessions)), 2)
        self.assertEqual(sessions[-1], state["session_id"])
        self.assertEqual(self.calls("prompt")[1], budget.continuation_prompt(HANDOFF, original)
                         + "\n\n---\nEarlier sessions of this worker, newest first (session ID, transcript):\n"
                         f"- {sessions[0]} none (session logging off or file missing)")
        self.assertEqual((self.path / "handoffs/turn-0001-1.md").read_text(), HANDOFF)
        self.assertEqual((self.path / "result.txt").read_text(), FINAL)
        self.assertEqual((self.path / "results/turn-0001.txt").read_text(), FINAL)
        self.assertEqual((state["handoffs"], state["turn_handoffs"], state["context_tokens"]), (1, 1, 100000))
        self.assertNotIn("budget_warning", state)
        self.assertEqual(len(self.receipts()), 2)
        self.assertTrue(all(item["status"] == "acknowledged" for item in self.receipts()))
        events = [json.loads(line) for line in (self.registry / "inbox.jsonl").read_text().splitlines()]
        event = next(item for item in events if item["type"] == "budget_handoff")
        expected = inbox.budget_event(state["id"], state["label"], 1, 1, 1, 90000, sessions[1], "Check the next step.")
        for key in expected.keys() - {"seq", "at"}:
            self.assertEqual(event[key], expected[key])
        self.assertIsInstance(event["seq"], int)
        self.assertTrue(event["at"].endswith("Z"))

    def test_continuation_links_the_ended_session_transcript(self):
        self.plan([{"result": HANDOFF}], [{"result": "done"}])
        initial = self.create()
        transcript = self.workspace / "claude-config/projects/-work" / f"{initial['session_id']}.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text("{}\n")
        code, state = self.supervise()
        self.assertEqual(code, 0, state)
        self.assertTrue(self.calls("prompt")[1].endswith(
            "\n\n---\nEarlier sessions of this worker, newest first (session ID, transcript):\n"
            f"- {initial['session_id']} {transcript}"))
        manifest = json.loads((self.path / "budget-context/turn-0001-h1.json").read_text())
        self.assertEqual(manifest["sessions"], [{"session_id": initial["session_id"], "transcript": str(transcript)}])

    def test_manual_handoff_steers_the_template_and_continues(self):
        launcher = str(REPO / "bin/claive")
        self.plan([{"usage": 50000}, {"handoff": "Wrap up the parser.", "launcher": launcher, "tokens": 50000}, {"read": 1},
                   {"result": HANDOFF}], [{"result": "done"}])
        self.create()
        code, state = self.supervise()
        self.assertEqual(code, 0, state)
        call = self.calls("handoff")
        self.assertEqual(call, [0], self.calls("error"))
        receipt = json.loads(self.calls("output")[0])
        self.assertEqual(receipt["source"], "budget")
        self.assertEqual(self.calls("input")[1], budget.manual_message(50000, "Wrap up the parser."))
        self.assertIn("\nParent note for the handoff: Wrap up the parser.\n", budget.manual_message(50000, " Wrap up the parser. "))
        self.assertEqual((state["handoffs"], state["budget_phase"]), (1, "none"))

    def test_manual_handoff_refuses_a_worker_without_budget(self):
        Path(self.env["CLAIVE_CONFIG"]).write_text(json.dumps({"engines": {"claude": {"budget": False}}}))
        state = self.create()
        refused = subprocess.run([str(REPO / "bin/claive"), "handoff", state["id"]], env=self.env,
                                 text=True, capture_output=True, timeout=5)
        self.assertEqual(refused.returncode, 1, refused.stdout)
        self.assertIn("claive handoff needs a Claude worker launched with budget mode", refused.stderr)
        self.assertFalse((self.path / "steering/receipts").exists() and any((self.path / "steering/receipts").iterdir()))

    def test_haiku_allows_three_handoffs_in_one_turn(self):
        self.plan(*([[{"result": HANDOFF}]] * 3), [{"result": "done"}])
        self.create()
        code, state = self.supervise()
        self.assertEqual(code, 0, state)
        self.assertEqual((state["handoffs"], state["turn_handoffs"]), (3, 3))
        self.assertNotIn("needs_parent", state)
        self.assertEqual(len(list((self.path / "handoffs").glob("*.md"))), 3)
        sessions = [command[command.index("--session-id") + 1] for command in self.calls("command")]
        self.assertEqual(self.calls("prompt")[-1], budget.continuation_prompt(HANDOFF, self.prompt.read_text())
                         + "\n\n---\nEarlier sessions of this worker, newest first (session ID, transcript):\n"
                         + "\n".join(f"- {session} none (session logging off or file missing)"
                                     for session in reversed(sessions[:3])))

    def test_handoff_limit_counts_automatic_continuations_then_asks_parent(self):
        self.plan([{"result": HANDOFF}], [{"result": HANDOFF}], [{"result": HANDOFF}])
        state = self.create("--report")
        state["launch"]["budget"]["max_handoffs"] = 2
        cli.save(self.path / "state.json", state)
        with patch.object(cli, "record_report", wraps=cli.record_report) as report:
            code, state = self.supervise()
        self.assertEqual(code, 3, state)
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["needs_parent"]["kind"], "budget_limit")
        handoff_file = self.path / "handoffs/turn-0001-3.md"
        self.assertIn(str(handoff_file), state["needs_parent"]["question"])
        self.assertEqual(handoff_file.read_text(), HANDOFF)
        self.assertEqual((state["handoffs"], state["turn_handoffs"]), (3, 3))
        self.assertEqual(report.call_count, 0)
        self.assertEqual((self.path / "result.txt").read_text(), HANDOFF)
        last_command = self.calls("command")[-1]
        self.assertNotEqual(state["session_id"], last_command[last_command.index("--session-id") + 1])

    def test_compaction_counts_missed_and_resets_phase(self):
        self.plan([{"usage": 90000}, {"read": 1}, {"compact": True},
                   {"usage": 90000}, {"read": 1}, {"result": "done"}])
        self.create()
        code, state = self.supervise()
        self.assertEqual(code, 0, state)
        self.assertEqual(state["budget_missed"], 1)
        self.assertEqual(len(self.receipts()), 3)
        self.assertIn(budget.COMPACTED.format(git="(not a git repository or git failed)"), self.calls("input")[1:])
        self.assertEqual(state["budget_phase"], "warned")

    def test_submit_value_error_is_recorded_without_failing_turn(self):
        self.plan([{"usage": 90000}, {"result": "done"}])
        self.create()
        with patch.object(cli.steering, "submit", side_effect=ValueError("transport unavailable")):
            code, state = self.supervise()
        self.assertEqual(code, 0, state)
        self.assertEqual(state["budget_error"], "transport unavailable")

    def test_failed_session_does_not_count_as_handoff(self):
        self.plan([{"result": HANDOFF}, {"exit": 1}])
        self.create()
        code, state = self.supervise()
        self.assertEqual(code, 1, state)
        self.assertEqual(state["handoffs"], 0)
        self.assertFalse((self.path / "handoffs").exists())

    def test_turn_timeout_spans_budget_continuations(self):
        self.plan([{"sleep": 0.6}, {"result": HANDOFF}], [{"sleep": 2}, {"result": "too late"}])
        self.create("--turn-timeout", "1")
        started = time.monotonic()
        code, state = self.supervise()
        elapsed = time.monotonic() - started
        self.assertEqual(code, 1, state)
        self.assertEqual(state["failure_kind"], "timeout")
        self.assertEqual(state["handoffs"], 1)
        self.assertLess(elapsed, 1.55)

    def test_model_fallback_still_restarts_the_timeout(self):
        self.plan([{"sleep": 0.4}, {"result": "retry", "subtype": "error"}, {"exit": 1}],
                  [{"sleep": 0.4}, {"result": "done"}])
        self.create("--turn-timeout", "0.7", "--fallback-models", "claude-sonnet-5-5")
        code, state = self.supervise()
        self.assertEqual(code, 0, state)
        self.assertEqual(state["model"], "claude-sonnet-5-5")
        self.assertEqual(len(state["fallbacks"]), 1)
        self.assertEqual(state["handoffs"], 0)

    def test_cancelled_session_does_not_count_as_handoff(self):
        self.plan([{"result": HANDOFF}, {"cancel": True}])
        self.create()
        code, state = self.supervise()
        self.assertEqual(code, 130, state)
        self.assertEqual(state["status"], "cancelled")
        self.assertEqual(state["handoffs"], 0)

    def test_budget_continues_without_session_logging(self):
        self.plan([{"result": HANDOFF}], [{"result": "done"}])
        self.create("--no-session-log")
        code, state = self.supervise()
        self.assertEqual(code, 0, state)
        self.assertIsNone(state["session_id"])
        self.assertEqual(state["handoffs"], 1)
        self.assertTrue(all("--no-session-persistence" in command for command in self.calls("command")))

    def test_answer_after_limit_delivers_handoff_and_resets_turn_count(self):
        self.plan([{"result": HANDOFF}], [{"result": HANDOFF}], [{"result": FINAL}], [{"result": FINAL}])
        Path(self.env["CLAIVE_CONFIG"]).write_text(json.dumps({"engines": {"claude": {"budget": {
            "models": {"claude-haiku-5-5": {"max_handoffs": 1}}}}}}))
        launcher = str(REPO / "bin/claive")
        process = subprocess.Popen([launcher, "open", "--engine", "claude", "--workspace", str(self.workspace),
                                    "--prompt-file", str(self.prompt), "--report"], env=self.env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def cleanup():
            if process.poll() is None:
                process.terminate()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
        self.addCleanup(cleanup)
        def idle(turn):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                for file in self.registry.glob("*/state.json"):
                    state = json.loads(file.read_text())
                    if state.get("status") == "idle" and state["turn"] == turn:
                        self.path = file.parent
                        return state
                if process.poll() is not None:
                    self.fail(f"reusable worker exited: {process.communicate()}")
                time.sleep(0.02)
            self.fail(f"turn {turn} did not become idle")
        first = idle(1)
        self.assertEqual(first["needs_parent"]["kind"], "budget_limit")
        self.assertEqual(first["pending_handoff"], str(self.path / "handoffs/turn-0001-2.md"))
        self.assertIn("answer or followup", first["needs_parent"]["question"])
        self.assertEqual(cli.outcome_code(first), 3)
        shown = subprocess.run([launcher, "show", first["id"]], env=self.env,
                               text=True, capture_output=True, timeout=5)
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertIn(f"Pending handoff: {first['pending_handoff']} (answer or followup continues from it)\n",
                      shown.stdout)
        manifest = json.loads(Path(first["pending_budget_context"]).read_text())
        assignment = Path(manifest["assignment_file"])
        saved_assignment = assignment.read_text()
        assignment.unlink()
        refused = subprocess.run([launcher, "answer", first["id"], "--message", "Continue."],
                                 env=self.env, text=True, capture_output=True, timeout=5)
        self.assertEqual(refused.returncode, 0, refused.stderr)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            blocked = json.loads((self.path / "state.json").read_text())
            if "Cannot resume budget context" in blocked.get("needs_parent", {}).get("question", ""):
                break
            time.sleep(0.02)
        self.assertEqual(blocked["turn"], 1)
        self.assertEqual(cli.outcome_code(blocked), 3)
        self.assertEqual(len(self.calls("command")), 2)
        self.assertIn("pending_budget_context", blocked)
        assignment.write_text(saved_assignment)
        answer = subprocess.run([launcher, "answer", first["id"], "--message", "Continue with the next step."],
                                env=self.env, text=True, capture_output=True, timeout=5)
        self.assertEqual(answer.returncode, 0, answer.stderr)
        second = idle(2)
        self.assertNotIn("pending_handoff", second)
        self.assertNotIn("needs_parent", second)
        self.assertEqual((second["handoffs"], second["turn_handoffs"]), (2, 0))
        self.assertEqual(second["report_state"], "ok")
        prompt = self.calls("prompt")[-1]
        self.assertTrue(prompt.startswith(budget.PREAMBLE + "\n\nOriginal assignment:\n"))
        self.assertIn(HANDOFF, prompt)
        self.assertIn("---\nParent message:\n", prompt)
        self.assertIn("Continue with the next step.", prompt)
        self.assertTrue(prompt.endswith(cli.REPORT_CONTRACT))
        self.assertEqual(Path(second["turn_prompt_file"]).read_text(), prompt)
        ordinary = self.workspace / "ordinary.md"
        ordinary.write_text("An ordinary independent next turn.")
        queued = subprocess.run([launcher, "followup", first["id"], "--prompt-file", str(ordinary)],
                                env=self.env, text=True, capture_output=True, timeout=5)
        self.assertEqual(queued.returncode, 0, queued.stderr)
        third = idle(3)
        self.assertNotIn("pending_budget_context", third)
        self.assertNotIn("budget_input_id", third)
        self.assertNotIn(HANDOFF, self.calls("prompt")[-1])
        self.assertNotIn("Complete the original assignment.", self.calls("prompt")[-1])
        closed = subprocess.run([launcher, "close", first["id"]], env=self.env,
                                text=True, capture_output=True, timeout=5)
        self.assertEqual(closed.returncode, 0, closed.stderr)
        process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0)

    def test_close_after_limit_clears_parent_state_and_keeps_handoff(self):
        self.plan([{"result": HANDOFF}], [{"result": HANDOFF}])
        Path(self.env["CLAIVE_CONFIG"]).write_text(json.dumps({"engines": {"claude": {"budget": {
            "models": {"claude-haiku-5-5": {"max_handoffs": 1}}}}}}))
        launcher = str(REPO / "bin/claive")
        process = subprocess.Popen([launcher, "open", "--engine", "claude", "--workspace", str(self.workspace),
                                    "--prompt-file", str(self.prompt), "--report"], env=self.env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        def cleanup():
            if process.poll() is None:
                process.terminate()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()

        self.addCleanup(cleanup)
        deadline = time.monotonic() + 5
        first = None
        while time.monotonic() < deadline:
            for file in self.registry.glob("*/state.json"):
                candidate = json.loads(file.read_text())
                if candidate.get("status") == "idle" and candidate["turn"] == 1:
                    self.path = file.parent
                    first = candidate
                    break
            if first is not None:
                break
            if process.poll() is not None:
                self.fail(f"reusable worker exited: {process.communicate()}")
            time.sleep(0.02)
        self.assertIsNotNone(first, "turn 1 did not become idle")
        self.assertEqual(first["needs_parent"]["kind"], "budget_limit")
        handoff_file = Path(first["pending_handoff"])
        self.assertTrue(handoff_file.is_file())
        inbox_file = self.registry / "inbox.jsonl"
        inbox_before = inbox_file.read_text()

        closed = subprocess.run([launcher, "close", first["id"]], env=self.env,
                                text=True, capture_output=True, timeout=5)
        self.assertEqual(closed.returncode, 0, closed.stderr)
        process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0)
        state = json.loads((self.path / "state.json").read_text())
        self.assertEqual((state["status"], state["phase"]), ("completed", "session closed"))
        self.assertNotIn("needs_parent", state)
        self.assertNotIn("pending_handoff", state)
        self.assertTrue(handoff_file.is_file())
        self.assertEqual(inbox_file.read_text(), inbox_before)

        listed = subprocess.run([launcher, "list"], env=self.env, text=True, capture_output=True, timeout=5)
        self.assertEqual(listed.returncode, 0, listed.stderr)
        row = next(line for line in listed.stdout.splitlines() if first["id"] in line)
        self.assertNotIn("ASK", row)
        self.assertIn("COMPLETED", row)
        shown = subprocess.run([launcher, "show", first["id"]], env=self.env,
                               text=True, capture_output=True, timeout=5)
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertNotIn("Needs parent", shown.stdout)
        self.assertNotIn("Pending handoff:", shown.stdout)


if __name__ == "__main__":
    unittest.main()
