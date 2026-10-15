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
from claivelib.engines.claude import CLAUDE_MODEL, ClaudeEngine, compaction_point

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

    def test_engine_defaults_to_haiku_and_leaves_the_model_policy_to_the_config(self):
        launch = self.engine.resolve_launch()
        self.assertEqual(launch["model"], CLAUDE_MODEL)
        self.assertEqual(CLAUDE_MODEL, "claude-haiku-5-5")
        launch["binary"] = str(self.binary)
        for model in ("claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"):
            with self.subTest(model=model):
                self.engine.validate_launch(dict(launch, model=model))
                command = self.engine.build_command(self.request(model=model))
                self.assertEqual(command[command.index("--model") + 1], model)
        for model in (None, ""):
            with self.subTest(model=model):
                with self.assertRaisesRegex(ValueError, "needs a model name"):
                    self.engine.validate_launch(dict(launch, model=model))

    def test_unsupported_options_are_rejected(self):
        launch = dict(self.engine.resolve_launch(), binary=str(self.binary))
        with self.assertRaises(ValueError):
            self.engine.validate_launch(dict(launch, output_schema="/x.json"))
        broken = self.root / "broken.json"
        broken.write_text("[1]")
        with self.assertRaises(ValueError):
            self.engine.validate_launch(dict(launch, output_schema=str(broken)))
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
        self.assertEqual(command[command.index("--tools") + 1], "default")
        self.assertEqual(command[command.index("--permission-mode") + 1], "bypassPermissions")
        self.assertEqual(command[command.index("--setting-sources") + 1], "")
        self.assertNotIn("--strict-mcp-config", command)
        self.assertNotIn("--disable-slash-commands", command)
        self.assertEqual(command[command.index("--autocompact") + 1], "138k")
        self.assertEqual(self.engine.default_reasoning_effort, "xhigh")
        self.assertIn("--session-id", command)
        self.assertEqual(command[-2:], ["--", "--do the thing\n"])
        read_only = self.engine.build_command(self.request(read_only=True))
        self.assertEqual(read_only[read_only.index("--tools") + 1], "default")
        self.assertEqual(read_only[read_only.index("--permission-mode") + 1], "plan")
        for read_only_turn, expected in ((False, command), (True, read_only)):
            with self.subTest(read_only=read_only_turn):
                self.assertEqual(self.engine.build_command(self.request(read_only=read_only_turn, web=True)),
                                 expected)
        self.assertTrue(self.engine.resolve_launch(web=True)["web"])
        transcript = self.root / "config/projects/-w" / f"{SESSION}.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text("")
        resumed = self.engine.build_command(self.request())
        self.assertIn("--resume", resumed)
        self.assertNotIn("--session-id", resumed)
        self.assertIn("--no-session-persistence", self.engine.build_command(
            self.request(session_id=None, session_logging=False)))

    def test_fork_resumes_source_with_fork_session_and_binds_new_id(self):
        source_session = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        command = self.engine.build_command(self.request(fork_session_id=source_session))
        self.assertEqual(command[command.index("--resume") + 1], source_session)
        self.assertIn("--fork-session", command)
        self.assertNotIn("--session-id", command)

        event = {"type": "system", "subtype": "init", "session_id": "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"}
        normalized = self.engine.normalize_event(event)
        self.assertEqual(normalized[0]["type"], "session_bound")
        self.assertEqual(normalized[0]["session_id"], event["session_id"])

    def test_autocompact_window_per_family_and_budget(self):
        for model, budget, point, window in (
                (CLAUDE_MODEL, None, 105000, "138k"),
                ("CLAUDE-HAIKU-custom", None, 105000, "138k"),
                ("claude-sonnet-5-5", None, 155000, "188k"),
                (CLAUDE_MODEL, {"stop": 95000}, 105000, "138k"),
                ("claude-sonnet-5-5", {"stop": 145000}, 155000, "188k"),
                (CLAUDE_MODEL, {"stop": 200000}, 210000, "243k"),
                ("claude-sonnet-5-5", {"stop": 200000}, 210000, "243k")):
            with self.subTest(model=model, budget=budget):
                command = self.engine.build_command(self.request(model=model, budget=budget))
                self.assertEqual(compaction_point(model, budget), point)
                self.assertEqual(command[command.index("--autocompact") + 1], window)

    def test_budget_launch_forces_steering_and_retains_budget(self):
        budget = {"warn": 85000, "stop": 95000, "max_handoffs": None, "compact": 105000}
        launch = self.engine.resolve_launch(budget=budget, steering=False)
        self.assertTrue(launch["steering"])
        self.assertEqual(launch["budget"], budget)
        self.assertIsNone(launch["budget_off_reason"])
        disabled = self.engine.resolve_launch(budget=None, budget_off_reason="config")
        self.assertFalse(disabled["steering"])
        self.assertIsNone(disabled["budget"])
        self.assertEqual(disabled["budget_off_reason"], "config")
        self.assertTrue(self.engine.resolve_launch(budget=None, steering=True)["steering"])

    def test_context_usage_preserves_existing_events_and_sums_inputs(self):
        contents = [{"type": "tool_use", "name": "Read", "id": "read1"},
                    {"type": "text", "text": "done"}]
        for blocks, expected in ((contents, ["tool_started", "output_delta"]),
                                 ([], ["model_step"]), (None, ["activity"])):
            for parent in ({}, {"parent_tool_use_id": None}):
                with self.subTest(blocks=blocks, parent=parent):
                    message = {"content": blocks}
                    event = dict(type="assistant", message=message, **parent)
                    before = self.engine.normalize_event(event)
                    message["usage"] = {"input_tokens": 123, "cache_read_input_tokens": 4000,
                                        "cache_creation_input_tokens": 500, "output_tokens": 999}
                    result = self.engine.normalize_event(event)
                    self.assertEqual([item["type"] for item in before], expected)
                    self.assertEqual(result[:-1], before)
                    self.assertEqual(result[-1], {"type": "context_usage", "native_kind": "assistant",
                                                  "tokens": 4623})
        for usage, total in (({}, 0), ({"input_tokens": 23}, 23)):
            result = self.engine.normalize_event({"type": "assistant", "message": {"usage": usage}})
            self.assertEqual(result[-1]["tokens"], total)

    def test_sidechain_usage_does_not_emit_context_usage(self):
        result = self.engine.normalize_event({
            "type": "assistant", "parent_tool_use_id": "toolA",
            "message": {"content": [], "usage": {"input_tokens": 200}}})
        self.assertEqual(result, [{"type": "model_step", "native_kind": "assistant"}])
        result = self.engine.normalize_event({"type": "assistant", "message": {"content": [], "usage": None}})
        self.assertEqual(result, [{"type": "model_step", "native_kind": "assistant"}])

    def test_context_usage_ignores_values_that_are_not_integers(self):
        for invalid in (None, True, False, 2.5, "100", [], {}):
            with self.subTest(invalid=invalid):
                result = self.engine.normalize_event({"type": "assistant", "message": {
                    "content": [], "usage": {"input_tokens": invalid, "cache_read_input_tokens": 400,
                                               "cache_creation_input_tokens": invalid}}})
                self.assertEqual(result[-1], {"type": "context_usage", "native_kind": "assistant", "tokens": 400})

    def test_compact_boundary_emits_context_compacted(self):
        self.assertEqual(self.engine.normalize_event({"type": "system", "subtype": "compact_boundary"}),
                         [{"type": "context_compacted", "native_kind": "system"}])

    def test_output_schema_becomes_json_schema_and_structured_result(self):
        schema = self.root / "schema.json"
        schema.write_text('{"type": "object", "required": ["verdict"]}')
        self.engine.validate_launch(dict(self.engine.resolve_launch(output_schema=str(schema)), binary=str(self.binary)))
        command = self.engine.build_command(self.request(output_schema=str(schema)))
        self.assertEqual(command[command.index("--json-schema") + 1], schema.read_text())
        self.assertNotIn("--json-schema", self.engine.build_command(self.request()))
        done = self.engine.normalize_event({"type": "result", "subtype": "success", "is_error": False,
                                            "result": "prose", "structured_output": {"verdict": "pass"}})
        self.assertEqual(json.loads(done[0]["text"]), {"verdict": "pass"})

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
                         ["activity", "tool_started", "tool_completed", "task_warning", "output_delta", "terminal_completed"])
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
