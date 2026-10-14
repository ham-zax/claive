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
from claivelib import cli, peek
from claivelib.engine import WorkerEngine
from claivelib.engines import get_engine


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
                                       "last_activity_age_seconds", "steps", "total"})
        self.assertEqual(result["id"], self.worker_id)
        self.assertEqual(result["status"], "idle")
        self.assertEqual(result["phase"], "waiting")
        self.assertEqual(result["turn"], 3)
        self.assertGreaterEqual(result["last_activity_age_seconds"], 10)
        self.assertEqual(result["steps"], [{"index": 1, "kind": "event", "text": "first\nsecond"}])
        human = self.run_cli()
        self.assertEqual(human.returncode, 0, human.stderr)
        self.assertRegex(human.stdout, rf"Worker {self.worker_id} \| idle \| turn 3 \| waiting \| last event \S+ ago")
        self.assertIn("event: first ⏎ second", human.stdout)

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
