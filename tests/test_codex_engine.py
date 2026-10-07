import hermetic  # noqa: F401  (must run before claivelib reads the environment)
import os
from pathlib import Path
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "bin"))
from claivelib import cli
from claivelib.engine import TurnRequest
from claivelib.engines import get_engine
from claivelib.engines.codex import CODEX_MODEL, CodexEngine

THREAD = "01a1184e-d220-7891-95de-8dee4b56367e"


class CodexChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="codex-engine-check-")
        self.root = Path(self.temp.name)
        self.prompt = self.root / "task.md"
        self.prompt.write_text("--do the thing\n")
        self.binary = self.root / "codex"
        self.binary.write_text("#!/bin/sh\n")
        self.binary.chmod(0o755)
        self.previous = os.environ.get("CODEX_HOME")
        os.environ["CODEX_HOME"] = str(self.root / "home")
        self.engine = CodexEngine()

    def tearDown(self):
        if self.previous is None:
            os.environ.pop("CODEX_HOME")
        else:
            os.environ["CODEX_HOME"] = self.previous
        self.temp.cleanup()

    def request(self, **overrides):
        values = dict(binary=str(self.binary), workspace=str(self.root), prompt_file=str(self.prompt),
                      session_id=THREAD, provider="openai", model=CODEX_MODEL,
                      reasoning_effort="max", max_model_steps=None, read_only=False, web=False,
                      output_schema=None, session_logging=True, isolation={})
        values.update(overrides)
        return TurnRequest(**values)

    def test_registry_resolves_the_engine(self):
        self.assertIsInstance(get_engine("codex"), CodexEngine)
        self.assertEqual(self.engine.default_reasoning_effort, "max")

    def test_model_and_effort_are_locked(self):
        launch = dict(self.engine.resolve_launch(), binary=str(self.binary))
        self.assertEqual(launch["model"], "gpt-6-luna")
        self.engine.validate_launch(launch)
        for model in ("gpt-5.6-luna", "gpt-6.1-sol", None):
            with self.subTest(model=model):
                with self.assertRaisesRegex(ValueError, "locked"):
                    self.engine.validate_launch(dict(launch, model=model))
                with self.assertRaisesRegex(ValueError, "locked"):
                    self.engine.build_command(self.request(model=model))
        for effort in ("high", "xhigh", "medium"):
            with self.subTest(effort=effort):
                with self.assertRaisesRegex(ValueError, "locked"):
                    self.engine.build_command(self.request(reasoning_effort=effort))

    def test_unsupported_options_are_rejected(self):
        launch = dict(self.engine.resolve_launch(), binary=str(self.binary))
        with self.assertRaises(ValueError):
            self.engine.validate_launch(dict(launch, output_schema="/x.json"))
        with self.assertRaises(ValueError):
            self.engine.validate_launch(dict(launch, isolation={"mode": "create"}))
        with self.assertRaises(ValueError):
            self.engine.validate_turn(self.request(max_model_steps=5))
        with self.assertRaises(ValueError):
            self.engine.validate_turn(self.request(provider="opencode2api"))

    def test_command_leaves_compaction_alone_and_resumes_a_known_thread(self):
        command = self.engine.build_command(self.request(read_only=True))
        self.assertNotIn("resume", command)
        self.assertFalse([part for part in command if "compact" in part])
        self.assertIn('model_reasoning_effort="max"', command)
        self.assertIn('sandbox_mode="read-only"', command)
        self.assertIn('web_search="disabled"', command)
        self.assertEqual(command[-2:], ["--", "--do the thing\n"])
        writer = self.engine.build_command(self.request(web=True))
        self.assertIn('sandbox_mode="workspace-write"', writer)
        self.assertIn('web_search="live"', writer)
        rollout = self.root / f"home/sessions/2026/10/08/rollout-2026-10-08T00-00-00-{THREAD}.jsonl"
        rollout.parent.mkdir(parents=True)
        rollout.write_text("")
        resumed = self.engine.build_command(self.request())
        self.assertEqual(resumed[1:3], ["exec", "resume"])
        self.assertEqual(resumed[resumed.index(THREAD) - 1], 'web_search="disabled"')
        self.assertIn("--ephemeral", self.engine.build_command(
            self.request(session_id=None, session_logging=False)))

    def test_events_normalize_and_bind_the_thread(self):
        events = [
            {"type": "thread.started", "thread_id": THREAD},
            {"type": "item.completed", "item": {"type": "error", "message": "bad agent file"}},
            {"type": "turn.started"},
            {"type": "item.started", "item": {"type": "command_execution", "command": "cat a.txt"}},
            {"type": "item.completed", "item": {"type": "command_execution", "exit_code": 1}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "banana"}},
            {"type": "turn.completed", "usage": {}},
        ]
        normalized = [item for event in events for item in self.engine.normalize_event(event)]
        self.assertEqual([item["type"] for item in normalized],
                         ["session_bound", "activity", "model_step", "tool_started", "task_warning",
                          "output_delta", "terminal_completed"])
        self.assertEqual(normalized[0]["session_id"], THREAD)
        self.assertEqual(normalized[-1]["text"], "banana")
        state = {"session_id": "claive-made", "steps": 0, "task_failures": 0}
        cli.event_update(state, normalized[0])
        self.assertEqual(state["session_id"], THREAD)

    def test_failed_turn_and_usage_limit(self):
        failed = self.engine.normalize_event({"type": "turn.failed", "error": {"message": "boom"}})
        self.assertEqual([item["type"] for item in failed], ["terminal_failed"])
        limited = self.engine.normalize_event(
            {"type": "turn.failed", "error": {"message": "You've hit your usage limit"}})
        self.assertEqual([item["type"] for item in limited], ["terminal_failed", "quota_exhausted"])


if __name__ == "__main__":
    unittest.main()
