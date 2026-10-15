"""Claude context budgets resolve once at launch from validated host config."""
import hermetic  # noqa: F401
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "bin"))
from claivelib import config
from claivelib.cli import turn_request

HAIKU = "claude-haiku-5-5"
SONNET = "claude-sonnet-5-5"

# Complete one turn over either Claude transport without contacting a provider.
FIXTURE = '''#!/usr/bin/env python3
import json, sys
if "--input-format" in sys.argv:
    json.loads(sys.stdin.readline())
print(json.dumps({"type": "result", "subtype": "success", "result": "done"}), flush=True)
'''


class BudgetConfigChecks(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="claive-budget-config-")
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name)
        self.config = self.path / "config.json"
        env_patch = patch.dict(os.environ, {"CLAIVE_CONFIG": str(self.config)})
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def policy(self, budget, engine="claude"):
        self.config.write_text(json.dumps({"engines": {engine: {"budget": budget}}}))

    def test_missing_budget_uses_builtins(self):
        self.assertEqual(config.claude_budget(HAIKU),
                         ({"warn": 85000, "stop": 95000, "max_handoffs": None, "compact": 105000}, None))
        self.assertEqual(config.claude_budget(SONNET),
                         ({"warn": 135000, "stop": 145000, "max_handoffs": 7, "compact": 155000}, None))

    def test_config_default_merges_per_key_over_builtin_model(self):
        self.policy({"models": {"default": {"warn": 80000}}})
        for model, stop, maximum in ((HAIKU, 95000, None), (SONNET, 145000, 7)):
            with self.subTest(model=model):
                self.assertEqual(config.claude_budget(model),
                                 ({"warn": 80000, "stop": stop, "max_handoffs": maximum,
                                   "compact": stop + 10000}, None))

    def test_config_model_overrides_config_default_per_key(self):
        self.policy({"models": {"default": {"warn": 80000, "max_handoffs": 3},
                                HAIKU: {"warn": 90000}, "custom": {"stop": 200000}}})
        self.assertEqual(config.claude_budget(HAIKU),
                         ({"warn": 90000, "stop": 95000, "max_handoffs": 3, "compact": 105000}, None))
        self.assertEqual(config.claude_budget("custom"),
                         ({"warn": 80000, "stop": 200000, "max_handoffs": 3, "compact": 210000}, None))

    def test_boolean_shorthand_and_disabled_object(self):
        self.policy(True)
        enabled = config.claude_budget(HAIKU)
        self.assertEqual(enabled[0]["stop"], 95000)
        self.assertIsNone(enabled[1])
        for value in (False, {"enabled": False}):
            with self.subTest(value=value):
                self.policy(value)
                self.assertEqual(config.claude_budget(HAIKU), (None, "config"))

    def test_invalid_model_values_are_rejected(self):
        for entry, message in (
                ({"warn": 95000}, "warn must be less than stop"),
                ({"warn": 96000}, "warn must be less than stop"),
                ({"warn": 50000, "stop": 56000}, "stop must be between"),
                ({"stop": 958000}, "stop must be between"),
                ({"warn": 85500}, "multiple of 1000"),
                ({"warn": True}, "integer"),
                ({"stop": True}, "integer"),
                ({"stop": 95000.0}, "integer"),
                ({"stop": 95500}, "multiple of 1000"),
                ({"max_handoffs": 0}, "max_handoffs must be"),
                ({"max_handoffs": True}, "max_handoffs must be"),
                ({"max_handoffs": 1.5}, "max_handoffs must be"),
                ({"unknown": 1}, "unknown keys unknown")):
            with self.subTest(entry=entry):
                self.policy({"models": {HAIKU: entry}})
                with self.assertRaisesRegex(ValueError, message):
                    config.load()

    def test_invalid_budget_structure_is_rejected(self):
        for budget, message in ((1, "wrong type"), (None, "wrong type"),
                                ({"unknown": True}, "unknown keys unknown"),
                                ({"enabled": 1}, "enabled must be a boolean"),
                                ({"models": []}, "models must be an object"),
                                ({"models": {HAIKU: None}}, "must be an object"),
                                ({"models": {"": {}}}, "nonempty model name")):
            with self.subTest(budget=budget):
                self.policy(budget)
                with self.assertRaisesRegex(ValueError, message):
                    config.load()

    def test_validation_checks_builtin_models_after_default_override(self):
        self.policy({"models": {"default": {"warn": 100000}}})
        with self.assertRaisesRegex(ValueError, HAIKU + r".warn must be less than stop"):
            config.load()

    def test_budget_is_claude_only(self):
        for engine in ("codex", "pi", "muse"):
            with self.subTest(engine=engine):
                self.policy(True, engine=engine)
                with self.assertRaisesRegex(ValueError, f"engines.{engine}.budget is only for claude"):
                    config.load()

    def test_valid_threshold_boundaries_and_handoff_limits(self):
        for stop in (57000, 957000):
            for maximum in (None, 1):
                with self.subTest(stop=stop, maximum=maximum):
                    self.policy({"models": {"default": {"warn": 50000, "stop": stop,
                                                       "max_handoffs": maximum}}})
                    self.assertEqual(config.claude_budget(SONNET)[0]["compact"], stop + 10000)

    def test_launch_resolves_budget_and_off_reason_precedence(self):
        binary = self.path / "claude"
        binary.write_text(FIXTURE)
        binary.chmod(0o755)
        prompt = self.path / "task.md"
        prompt.write_text("Do the task.\n")
        schema = self.path / "schema.json"
        schema.write_text('{"type": "object"}')
        registry = self.path / "registry"
        env = dict(os.environ, CLAIVE_DIR=str(registry), CLAUDE_WORKER_BINARY=str(binary),
                   CLAUDE_CONFIG_DIR=str(self.path / "claude-config"))
        schema_args = ["--output-schema", str(schema)]
        fallback_args = ["--fallback-models", SONNET]
        for enabled, extra, reason in ((True, [], None),
                                       (True, schema_args, "output schema"),
                                       (True, fallback_args, "fallback models"),
                                       (True, schema_args + fallback_args, "output schema"),
                                       (False, schema_args + fallback_args, "config")):
            with self.subTest(enabled=enabled, extra=extra):
                self.policy(enabled)
                before = set(registry.glob("*/state.json"))
                result = subprocess.run(
                    [str(REPO / "bin/claive"), "run", "--engine", "claude", "--workspace", str(self.path),
                     "--prompt-file", str(prompt), *extra], env=env, capture_output=True, text=True, timeout=16)
                self.assertEqual(result.returncode, 0, result.stderr)
                created = set(registry.glob("*/state.json")) - before
                self.assertEqual(len(created), 1)
                state = json.loads(created.pop().read_text())
                launch = state["launch"]
                self.assertEqual(launch["budget_off_reason"], reason)
                if reason:
                    self.assertIsNone(launch["budget"])
                    self.assertFalse(launch["steering"])
                else:
                    self.assertEqual(launch["budget"]["stop"], 95000)
                    self.assertTrue(launch["steering"])
                self.assertEqual(turn_request(state).budget, launch["budget"])


if __name__ == "__main__":
    unittest.main()
