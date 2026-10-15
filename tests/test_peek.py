"""Read-only recent provider steps, using synthetic worker directories."""
import hermetic  # noqa: F401
from contextlib import redirect_stdout
import io
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
sys.path.insert(0, str(REPO / "bin"))
from claivelib import cli, intent, messaging, peek, redaction
from claivelib.engines import get_engine


class RedactionChecks(unittest.TestCase):
    def test_issuer_token_shapes(self):
        cases = [
            "sk-" + "a_b-" * 4,
            *(prefix + "a" * 36 for prefix in ("ghp_", "gho_", "ghs_", "github_pat_")),
            *(f"xox{kind}-1234567890-abcdefghij" for kind in "abpr"),
            "AKIA" + "A1" * 8,
        ]
        for secret in cases:
            with self.subTest(prefix=secret.split("-", 1)[0]):
                self.assertEqual(redaction.redact(secret), "[redacted]")

    def test_bearer_private_key_and_named_values(self):
        self.assertEqual(redaction.redact("Authorization: Bearer abc.def_123+/="),
                         "Authorization: Bearer [redacted]")
        private_key = ("-----BEGIN RSA PRIVATE KEY-----\nMIIE-private-material\n"
                       "-----END RSA PRIVATE KEY-----")
        self.assertEqual(redaction.redact(private_key), "[redacted]")

        for key in ("client_secret", "refresh_token", "DB_PASSWORD", "passwd", "api_key", "APIKEY"):
            with self.subTest(key=key):
                self.assertEqual(redaction.redact(f"{key}=sensitive-value"),
                                 f"{key}=[redacted]")
        self.assertEqual(redaction.redact('{"database_token": "sensitive-value"}'),
                         '{"database_token": "[redacted]"}')
        self.assertEqual(redaction.redact("password='sensitive value'"),
                         "password='[redacted]'")

    def test_false_positives_remain_unchanged(self):
        text = ("Normal prose mentions a token, password, and secret without assigning values. "
                "Commit 0123456789abcdef0123456789abcdef01234567; worker 012345abcdef.")
        self.assertEqual(redaction.redact(text), text)

    def test_short_and_numeric_named_values_remain_unchanged(self):
        text = ('input_tokens=120 max_tokens=4096 token=12345678 secret=short '
                '{"input_tokens":"120","api_key":"12345678"}')
        self.assertEqual(redaction.redact(text), text)
        self.assertEqual(redaction.redact("secret=long-value"), "secret=[redacted]")
        self.assertEqual(redaction.redact('{"api_key":"long-value"}'),
                         '{"api_key":"[redacted]"}')


class IntentChecks(unittest.TestCase):
    def test_latest_text_step_first_nonempty_line_and_markdown_cleanup(self):
        steps = [
            {"kind": "text", "text": "Earlier intent"},
            {"kind": "tool_result", "text": "ignored"},
            {"kind": "text", "text": "\n  - **Review the _latest_ change**\nAnother line"},
        ]
        self.assertEqual(intent.intent_from_steps(steps), "Review the latest change")

    def test_no_text_or_empty_latest_text_returns_none(self):
        self.assertIsNone(intent.intent_from_steps([]))
        self.assertIsNone(intent.intent_from_steps([{"kind": "tool_call", "text": "Read"}]))
        self.assertIsNone(intent.intent_from_steps([
            {"kind": "text", "text": "Older intent"}, {"kind": "text", "text": " \n\x00 "},
        ]))

    def test_control_characters_are_removed_and_long_intent_is_capped(self):
        self.assertEqual(intent.intent_from_steps([{"kind": "text", "text": "Review\x00 the patch"}]),
                         "Review the patch")
        self.assertEqual(intent.intent_from_steps([{"kind": "text", "text": "\x1b[31mReview the patch\x1b[0m"}]),
                         "Review the patch")
        self.assertEqual(intent.intent_from_steps([{"kind": "text", "text": "x" * 130}]),
                         "x" * 119 + "…")

    def test_intent_from_each_engine_text_fixture(self):
        pi_events = [json.loads(line) for line in
                     (REPO / "tests/fixtures/pi/success.jsonl").read_text().splitlines()]
        fixtures = [
            ("pi", pi_events, "PI_PROBE_OK"),
            ("claude", [{"type": "assistant", "message": {"content": [
                {"type": "text", "text": "**Claude scans sources**\nsecond line"}]}}],
             "Claude scans sources"),
            ("codex", [{"type": "item.completed", "item": {
                "type": "agent_message", "text": "- Codex summarizes evidence\nsecond line"}}],
             "Codex summarizes evidence"),
            ("muse", [{"payload_type": "run.terminal.completed", "payload": {
                "terminal": "completed", "text": "1. Muse checks the result\nsecond line"}}],
             "Muse checks the result"),
        ]
        for name, events, expected in fixtures:
            with self.subTest(engine=name):
                engine = get_engine(name)
                steps = [step for event in events for step in engine.describe_event(event)]
                self.assertEqual(intent.intent_from_steps(steps), expected)


class PeekChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="peek-check-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.worker_id = "012345abcdef"
        self.path = self.root / self.worker_id
        self.path.mkdir()
        self.state = dict(id=self.worker_id, engine="muse", status="idle", phase="waiting",
                          turn=3, last_activity=time.time() - 10,
                          started_at=time.time() - 100, supervisor_pid=99999999)
        self.write_state()
        self.events = self.path / "events.jsonl"
        self.events.write_text("")
        self.env = dict(os.environ, CLAIVE_DIR=str(self.root))

    def write_state(self):
        (self.path / "state.json").write_text(json.dumps(self.state))

    def write_events(self, events):
        self.events.write_text("".join(json.dumps(event) + "\n" for event in events))

    def run_cli(self, *args, env=None):
        return subprocess.run([sys.executable, str(REPO / "bin/claive"), "peek",
                               self.worker_id, *args], env=env or self.env,
                              text=True, capture_output=True, timeout=10)

    def output(self, *args):
        result = self.run_cli(*args, "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_steps_default_and_maximum(self):
        self.write_events([{"type": str(index)} for index in range(60)])
        self.assertEqual([step["text"] for step in self.output()["steps"]],
                         [str(index) for index in range(55, 60)])
        steps = self.output("--steps", "50")["steps"]
        self.assertEqual(len(steps), 50)
        self.assertEqual(steps[0]["text"], "10")

    def test_invalid_step_limits(self):
        for count in (0, 51):
            with self.subTest(count=count):
                result = self.run_cli("--steps", str(count))
                self.assertEqual(result.returncode, 1)
                self.assertIn("--steps must be between 1 and 50", result.stderr)

    def test_step_text_cap_keeps_head_and_tail(self):
        self.write_events([{"type": "a" * 600}, {"type": "h" * 400 + "m" * 100 + "t" * 200}])
        steps = self.output()["steps"]
        self.assertEqual(steps[0]["text"], "a" * 600)
        self.assertEqual(steps[1]["text"], "h" * 400 + "\n[… 100 chars elided …]\n" + "t" * 200)

    def test_numbers_are_stable_and_paging_works(self):
        self.write_events([{"type": str(index)} for index in range(1, 13)])
        result = self.output()
        self.assertEqual(result["total"], 12)
        self.assertEqual([step["index"] for step in result["steps"]], [8, 9, 10, 11, 12])
        page = self.output("--start", "3", "--steps", "2")["steps"]
        self.assertEqual(page, [{"index": 3, "kind": "event", "text": "3"},
                                {"index": 4, "kind": "event", "text": "4"}])
        human = self.run_cli("--start", "3", "--steps", "2").stdout
        self.assertIn("Steps 3-4 of 12", human)
        self.assertIn("[3] event: 3", human)
        self.assertIn(f"claive peek {self.worker_id} --start 5", human)
        for start, error in (("0", "--start must be at least 1"),
                             ("13", "nothing at step 13: the worker has 12 step(s)")):
            result = self.run_cli("--start", start)
            self.assertEqual(result.returncode, 1)
            self.assertIn(error, result.stderr)

    def test_unfinished_last_line_is_not_a_step(self):
        self.events.write_bytes(b'{"type":"done"}\n{"type":"half"}')
        self.assertEqual(self.output()["steps"], [{"index": 1, "kind": "event", "text": "done"}])

    def test_malformed_lines_and_non_objects_are_skipped(self):
        self.events.write_bytes(b'broken\nnull\n[]\n42\n"text"\n\xff\n{"type":"ok"}\n{')
        self.assertEqual(self.output()["steps"], [{"index": 1, "kind": "event", "text": "ok"}])

    def test_missing_and_empty_events(self):
        for missing in (False, True):
            with self.subTest(missing=missing):
                if missing:
                    self.events.unlink()
                self.assertEqual(self.output()["steps"], [])
                result = self.run_cli()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("No steps recorded yet.", result.stdout)

    def test_worker_cannot_peek_even_with_nested_override(self):
        for identifier in ("", self.worker_id):
            env = dict(self.env, CLAIVE_WORKER_ID=identifier, CLAIVE_ALLOW_NESTED="1")
            result = self.run_cli(env=env)
            self.assertEqual(result.returncode, 1)
            self.assertIn("only the parent can peek at workers", result.stderr)

    def test_files_and_mtimes_unchanged_and_no_new_files(self):
        self.write_events([{"type": "proof"}])
        before = {path.relative_to(self.root): (path.read_bytes(), path.stat().st_mtime_ns)
                  for path in self.root.rglob("*") if path.is_file()}
        self.output()
        self.run_cli()
        after = {path.relative_to(self.root): (path.read_bytes(), path.stat().st_mtime_ns)
                 for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(after, before)

    def test_no_locks_writes_or_signals(self):
        # cli.load reads /proc for the liveness check; that is read-only and allowed.
        with (mock.patch.dict(os.environ, self.env, clear=True),
              mock.patch("fcntl.flock", side_effect=AssertionError("lock")),
              mock.patch.object(cli, "save", side_effect=AssertionError("write")),
              mock.patch("os.kill", side_effect=AssertionError("signal")),
              redirect_stdout(io.StringIO())):
            self.assertEqual(peek.show(self.worker_id), 0)

    def test_json_shape_and_header(self):
        self.write_events([{"type": "first\nsecond"}])
        result = self.output()
        self.assertEqual(set(result), {"id", "status", "phase", "turn",
                                       "last_activity_age_seconds", "intent", "steps", "total"})
        self.assertEqual(result["id"], self.worker_id)
        self.assertEqual(result["status"], "idle")
        self.assertEqual(result["phase"], "waiting")
        self.assertEqual(result["turn"], 3)
        self.assertGreaterEqual(result["last_activity_age_seconds"], 10)
        self.assertIsNone(result["intent"])
        self.assertEqual(result["steps"], [{"index": 1, "kind": "event", "text": "first\nsecond"}])
        human = self.run_cli()
        self.assertEqual(human.returncode, 0, human.stderr)
        self.assertRegex(human.stdout, rf"Worker {self.worker_id} \| idle \| turn 3 \| waiting \| last event \S+ ago")
        self.assertIn("event: first ⏎ second", human.stdout)
        self.assertNotIn("Intent:", human.stdout)

    def test_intent_uses_full_log_and_is_redacted_in_human_and_json(self):
        self.state["engine"] = "claude"
        self.write_state()
        token = "sk-" + "intent-" + "A9" * 10
        events = [{"type": "assistant", "message": {"content": [
            {"type": "text", "text": f"Step {index}"}]}} for index in range(5)]
        events.append({"type": "assistant", "message": {"content": [
            {"type": "text", "text": f"**Review {token} now**\nsecond line"}]}})
        self.write_events(events)

        result = self.output("--start", "1", "--steps", "1")
        self.assertEqual(result["steps"], [{"index": 1, "kind": "text", "text": "Step 0"}])
        self.assertEqual(result["intent"], "Review [redacted] now")
        human = self.run_cli("--start", "1", "--steps", "1")
        self.assertEqual(human.returncode, 0, human.stderr)
        self.assertEqual(human.stdout.splitlines()[1], "Intent: Review [redacted] now")
        self.assertNotIn(token, human.stdout)

    def test_human_output_formats_parent_message_without_changing_json(self):
        identifier = "f" * 32
        envelope = messaging.delivered_text(dict(
            id=identifier, kind="note", message="Keep the final check focused.",
            sender="parent (codex)"))
        self.state["engine"] = "claude"
        self.write_state()
        self.write_events([{"type": "assistant", "message": {"content": [
            {"type": "text", "text": envelope}]}}])

        result = self.output()
        self.assertEqual(result["steps"][0]["text"], envelope)
        human = self.run_cli()
        self.assertEqual(human.returncode, 0, human.stderr)
        self.assertIn(f"parent -> worker [note {identifier}]: Keep the final check focused.", human.stdout)
        self.assertIn(f"Intent: parent -> worker [note {identifier}]: Keep the final check focused.", human.stdout)
        self.assertNotIn("<parent-message>", human.stdout)

    def test_codex_duplicate_agent_message_event_is_one_step(self):
        self.state["engine"] = "codex"
        self.write_state()
        text = "final response"
        self.write_events([
            {"type": "item.completed", "item": {
                "type": "agent_message", "id": "msg_123", "text": text,
                "command": None, "exit_code": None}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": text}},
        ])

        result = self.output()
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["steps"], [{"index": 1, "kind": "text", "text": text}])

        self.write_events([
            {"type": "item.completed", "item": {"type": "agent_message", "id": "msg_123", "text": text}},
            {"type": "item.completed", "item": {"type": "agent_message", "id": "msg_456", "text": text}},
        ])
        self.assertEqual(self.output()["total"], 2)

    def test_step_text_is_redacted_before_clip_in_human_and_json(self):
        self.state["engine"] = "claude"
        self.write_state()
        token = "sk-" + "Z_9-" * 4
        text = f"Keep this value private: {token}"
        self.write_events([{"type": "assistant", "message": {"content": [
            {"type": "text", "text": text}]}}])

        self.assertEqual(self.output()["steps"][0]["text"],
                         "Keep this value private: [redacted]")
        human = self.run_cli()
        self.assertIn("Keep this value private: [redacted]", human.stdout)
        self.assertNotIn(token, human.stdout)

        long_text = "h" * 395 + token + "t" * 400
        self.write_events([{"type": "assistant", "message": {"content": [
            {"type": "text", "text": long_text}]}}])
        clipped_inputs = []

        def capture_before_clip(value, limit=peek.STEP_CHARS):
            clipped_inputs.append(value)
            return value

        with mock.patch.object(peek, "clip", side_effect=capture_before_clip):
            peek.steps_window(self.path, get_engine("claude"), 5)
        self.assertEqual(clipped_inputs, [redaction.redact(long_text)])

    def test_human_output_strips_controls_but_json_keeps_step_text(self):
        self.write_events([{"type": "probe"}])
        unsafe_kind = "event\x1b[2J"
        unsafe_text = "plain\x07 café\x9b2J"
        engine = mock.Mock()
        engine.describe_event.return_value = [dict(kind=unsafe_kind, text=unsafe_text)]

        with (mock.patch.dict(os.environ, self.env, clear=True),
              mock.patch("claivelib.engines.get_engine", return_value=engine),
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(peek.show(self.worker_id), 0)
        human = output.getvalue()
        self.assertNotIn("\x1b", human)
        self.assertNotIn("\x07", human)
        self.assertNotIn("\x9b", human)
        self.assertIn("event: plain café", human)

        with (mock.patch.dict(os.environ, self.env, clear=True),
              mock.patch("claivelib.engines.get_engine", return_value=engine),
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(peek.show(self.worker_id, json_output=True), 0)
        record = json.loads(output.getvalue())
        self.assertEqual(record["steps"], [dict(index=1, kind=unsafe_kind, text=unsafe_text)])

    def test_unknown_activity_age(self):
        self.state.pop("last_activity")
        self.write_state()
        self.assertIsNone(self.output()["last_activity_age_seconds"])
        self.assertIn("last event unknown ago", self.run_cli().stdout)

    def test_alias_resolution(self):
        (self.root / "aliases.json").write_text(json.dumps({"builder": self.worker_id}))
        result = subprocess.run([sys.executable, str(REPO / "bin/claive"), "peek",
                                 "@builder", "--json"], env=self.env, text=True,
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["id"], self.worker_id)


class EventDescriptions(unittest.TestCase):
    def test_pi_fixture_text_call_and_result(self):
        events = [json.loads(line) for line in (REPO / "tests/fixtures/pi/tool.jsonl").read_text().splitlines()]
        steps = [step for event in events for step in get_engine("pi").describe_event(event)]
        self.assertEqual(steps, [
            {"kind": "tool_call", "text": 'read {"path":"proof.txt"}'},
            {"kind": "tool_result", "text": "PI_PROBE_OK"},
            {"kind": "text", "text": "PI_PROBE_OK"},
        ])

    def test_claude_text_call_and_result(self):
        # Shapes from tests/test_claude_engine.py's normalization cases.
        engine = get_engine("claude")
        self.assertEqual(engine.describe_event({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "done"}, {"type": "tool_use", "name": "Read"}]}}),
            [{"kind": "text", "text": "done"}, {"kind": "tool_call", "text": "Read {}"}])
        self.assertEqual(engine.describe_event({"type": "user", "message": {"content": [
            {"type": "tool_result", "is_error": True, "content": "denied"}]}}),
            [{"kind": "tool_result", "text": "error: denied"}])
        self.assertEqual(engine.describe_event({"type": "result", "subtype": "success", "result": "done"}), [])
        self.assertEqual(engine.describe_event({"type": "system", "subtype": "init"}), [])
        self.assertEqual(engine.describe_event({"type": "system", "subtype": "hook_started"}), [])
        self.assertEqual(engine.describe_event({"type": "system", "subtype": "api_error",
                                                "error": {"message": "service unavailable"}}),
                         [{"kind": "error", "text": "service unavailable"}])

    def test_claude_rate_limit_peek_uses_normalized_quota_only_when_rejected(self):
        engine = get_engine("claude")
        allowed_statuses = ("allowed", "allowed_warning", "not_rejected")
        for status in allowed_statuses:
            with self.subTest(status=status):
                event = {"type": "rate_limit_event", "rate_limit_info": {"status": status}}
                self.assertEqual(engine.describe_event(event), [])

        rejected = {"type": "rate_limit_event", "rate_limit_info": {
            "status": "rejected", "resetsAt": 1791417600}}
        quota = next(item for item in engine.normalize_event(rejected)
                     if item["type"] == "quota_exhausted")
        self.assertEqual(engine.describe_event(rejected),
                         [{"kind": "error", "text": peek.compact(quota)}])

    def test_claude_provider_error_pair_is_one_error_and_distinct_result_remains(self):
        engine = get_engine("claude")
        error_text = (
            "Autocompact is thrashing: the context refilled to the limit within 3 turns of the previous "
            "compact, 3 times in a row. A file being read or a tool output is likely too large for the "
            "context window. Try reading in smaller chunks, or use /clear to start fresh.")
        assistant = {"type": "assistant", "error": "invalid_request", "message": {
            "type": "message", "role": "assistant", "content": [{"type": "text", "text": error_text}]}}
        matching_result = {"type": "result", "subtype": "success", "is_error": True,
                           "result": error_text}

        steps = engine.describe_event(assistant) + engine.describe_event(matching_result)

        self.assertEqual(steps, [{"kind": "error", "text": error_text}])
        different_result = {"type": "result", "subtype": "error_api_error", "is_error": True,
                            "result": "A different provider error"}
        different_pair = engine.describe_event(assistant) + engine.describe_event(different_result)
        self.assertEqual(different_pair, [
            {"kind": "error", "text": error_text},
            {"kind": "error", "text": "A different provider error"},
        ])

    def test_codex_text_call_and_result(self):
        # Shapes from tests/test_codex_engine.py's normalization cases.
        engine = get_engine("codex")
        cases = [
            ({"type": "item.completed", "item": {"type": "agent_message", "text": "banana"}},
             {"kind": "text", "text": "banana"}),
            ({"type": "item.started", "item": {"type": "command_execution", "command": "cat a.txt"}},
             {"kind": "tool_call", "text": 'shell {"command":"cat a.txt"}'}),
            ({"type": "item.completed", "item": {"type": "command_execution", "exit_code": 1}},
             {"kind": "tool_result", "text": 'error: {"exit_code":1}'}),
        ]
        for event, expected in cases:
            with self.subTest(event=event):
                self.assertEqual(engine.describe_event(event), [expected])

    def test_provider_errors(self):
        pi_events = [json.loads(line) for line in (REPO / "tests/fixtures/pi/failure.jsonl").read_text().splitlines()]
        steps = [step for event in pi_events for step in get_engine("pi").describe_event(event)]
        self.assertEqual(steps, [{"kind": "error", "text": "400: PI_CHARACTERIZATION_FAILURE"}])
        self.assertEqual(get_engine("codex").describe_event({"type": "turn.failed", "error": {"message": "boom"}}),
                         [{"kind": "error", "text": "boom"}])
        self.assertEqual(get_engine("claude").describe_event({"type": "result", "subtype": "error_max_turns", "is_error": True}),
                         [{"kind": "error", "text": "error_max_turns"}])

    def test_unknown_events_fall_back_in_every_engine(self):
        for name in ("muse", "pi", "claude", "codex"):
            with self.subTest(engine=name):
                self.assertEqual(get_engine(name).describe_event({"type": "future"}),
                                 [{"kind": "event", "text": "future"}])
                self.assertEqual(get_engine(name).describe_event({}),
                                 [{"kind": "event", "text": "unknown"}])
                self.assertEqual(get_engine(name).describe_event({"type": []}),
                                 [{"kind": "event", "text": "[]"}])


if __name__ == "__main__":
    unittest.main()
