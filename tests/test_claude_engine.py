import hermetic  # noqa: F401  (must run before claivelib reads the environment)
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "bin"))
from claivelib.engine import TurnRequest
from claivelib.engines import get_engine
from claivelib.engines.claude import CLAUDE_MODEL, ClaudeEngine

SESSION = "11111111-2222-4333-8444-555555555555"


class ClaudeChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="claude-engine-check-")
        self.root = Path(self.temp.name)
        self.prompt = self.root / "task.md"
        self.prompt.write_text("--do the thing\n")
        self.binary = self.root / "claude"
        self.binary.write_text("#!/bin/sh\n")
        self.binary.chmod(0o755)
        self.previous = os.environ.get("CLAUDE_CONFIG_DIR")
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.root / "config")
        self.engine = ClaudeEngine()

    def tearDown(self):
        if self.previous is None:
            os.environ.pop("CLAUDE_CONFIG_DIR")
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = self.previous
        self.temp.cleanup()

    def request(self, **overrides):
        values = dict(binary=str(self.binary), workspace=str(self.root), prompt_file=str(self.prompt),
                      session_id=SESSION, provider="anthropic", model=CLAUDE_MODEL,
                      reasoning_effort="high", max_model_steps=None, read_only=False, web=False,
                      output_schema=None, session_logging=True, isolation={})
        values.update(overrides)
        return TurnRequest(**values)

    def test_registry_resolves_the_engine(self):
        self.assertIsInstance(get_engine("claude"), ClaudeEngine)

    def test_model_is_locked_to_haiku(self):
        launch = self.engine.resolve_launch()
        self.assertEqual(launch["model"], CLAUDE_MODEL)
        self.assertEqual(CLAUDE_MODEL, "claude-haiku-5-5")
        launch["binary"] = str(self.binary)
        self.engine.validate_launch(launch)
        for model in ("sonnet", "claude-sonnet-5-5", "claude-opus-5-5", None):
            with self.subTest(model=model):
                with self.assertRaisesRegex(ValueError, "locked"):
                    self.engine.validate_launch(dict(launch, model=model))
                with self.assertRaisesRegex(ValueError, "locked"):
                    self.engine.build_command(self.request(model=model))

    def test_unsupported_options_are_rejected(self):
        launch = dict(self.engine.resolve_launch(), binary=str(self.binary))
        with self.assertRaises(ValueError):
            self.engine.validate_launch(dict(launch, output_schema="/x.json"))
        with self.assertRaises(ValueError):
            self.engine.validate_launch(dict(launch, isolation={"mode": "create"}))
        with self.assertRaises(ValueError):
            self.engine.validate_turn(self.request(max_model_steps=5))
        with self.assertRaises(ValueError):
            self.engine.validate_turn(self.request(reasoning_effort="off"))
        with self.assertRaises(ValueError):
            self.engine.validate_turn(self.request(provider="opencode2api"))

    def test_command_tools_session_and_resume(self):
        command = self.engine.build_command(self.request())
        self.assertEqual(command[command.index("--model") + 1], CLAUDE_MODEL)
        self.assertEqual(command[command.index("--tools") + 1], "Read,Edit,Write,Bash,Grep,Glob")
        self.assertEqual(command[command.index("--autocompact") + 1], "100k")
        self.assertEqual(self.engine.default_reasoning_effort, "max")
        self.assertIn("--session-id", command)
        self.assertEqual(command[-2:], ["--", "--do the thing\n"])
        read_only = self.engine.build_command(self.request(read_only=True))
        self.assertEqual(read_only[read_only.index("--tools") + 1], "Read,Grep,Glob")
        web = self.engine.build_command(self.request(read_only=True, web=True))
        self.assertTrue(web[web.index("--tools") + 1].endswith(",WebSearch,WebFetch"))
        transcript = self.root / "config/projects/-w" / f"{SESSION}.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text("")
        resumed = self.engine.build_command(self.request())
        self.assertIn("--resume", resumed)
        self.assertNotIn("--session-id", resumed)
        self.assertIn("--no-session-persistence", self.engine.build_command(
            self.request(session_id=None, session_logging=False)))

    def test_events_normalize_to_a_single_terminal(self):
        events = [
            {"type": "system", "subtype": "init"},
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read"}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "is_error": True, "content": "denied"}]}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "done"}]}},
            {"type": "result", "subtype": "success", "is_error": False, "result": "done"},
        ]
        normalized = [item for event in events for item in self.engine.normalize_event(event)]
        self.assertEqual([item["type"] for item in normalized],
                         ["activity", "tool_started", "task_warning", "output_delta", "terminal_completed"])
        self.assertEqual(normalized[-1]["text"], "done")
        failed = self.engine.normalize_event({"type": "result", "subtype": "error_max_turns", "is_error": True})
        self.assertEqual(failed[0]["type"], "terminal_failed")

    def test_rejected_rate_limit_reports_quota(self):
        info = {"status": "rejected", "resetsAt": 1791417600}
        events = self.engine.normalize_event({"type": "rate_limit_event", "rate_limit_info": info})
        self.assertEqual(events[1]["type"], "quota_exhausted")
        self.assertRegex(events[1]["reset_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        allowed = self.engine.normalize_event(
            {"type": "rate_limit_event", "rate_limit_info": dict(info, status="allowed")})
        self.assertEqual([item["type"] for item in allowed], ["activity"])


if __name__ == "__main__":
    unittest.main()
