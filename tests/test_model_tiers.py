import hermetic  # noqa: F401  (must run before claivelib reads the environment)
"""The three model tiers: the default, the allowed set, and the extra set that needs an opt-in."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from claivelib import config
from claivelib.cli import turn_request
from claivelib.engines import ENGINE_NAMES, get_engine

CLI = Path(__file__).resolve().parents[1] / "bin/claive"


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="claive-model-tiers-")
        self.root = Path(self.temp.name)
        self.config = self.root / "config.json"
        self.previous = os.environ.get("CLAIVE_CONFIG")
        os.environ["CLAIVE_CONFIG"] = str(self.config)

    def tearDown(self):
        if self.previous is None:
            os.environ.pop("CLAIVE_CONFIG", None)
        else:
            os.environ["CLAIVE_CONFIG"] = self.previous
        self.temp.cleanup()

    def write(self, data):
        self.config.write_text(json.dumps(data), encoding="utf-8")

    def run_cli(self, *arguments):
        return subprocess.run([str(CLI), *arguments], capture_output=True, text=True, timeout=120,
                              env=dict(os.environ, CLAIVE_CONFIG=str(self.config)))


class Tiers(Base):
    def test_without_an_allowlist_the_catalog_is_allowed(self):
        tiers = config.model_tiers("claude", get_engine("claude"))
        self.assertEqual(tiers["default"], "claude-haiku-5-5")
        self.assertEqual(sorted(tiers["allowed"]), sorted(get_engine("claude").supported_models()))
        self.assertEqual(tiers["extra"], [])

    def test_an_allowlist_moves_the_rest_into_extra(self):
        self.write({"engines": {"claude": {"models": {"claude-haiku-5-5": {"efforts": ["high"]}}}}})
        tiers = config.model_tiers("claude", get_engine("claude"))
        self.assertEqual(tiers["allowed"], ["claude-haiku-5-5"])
        self.assertEqual(sorted(tiers["extra"]),
                         ["claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5-5"])

    def test_an_open_catalog_has_no_extra_tier(self):
        tiers = config.model_tiers("pi", get_engine("pi"))
        self.assertIsNone(tiers["allowed"])
        self.assertEqual(tiers["extra"], [])

    def test_extra_models_extend_the_catalog(self):
        self.write({"engines": {"claude": {"extra_models": {"claude-opus-6": {"efforts": ["high"]}}}}})
        catalog = config.supported_models("claude", get_engine("claude"))
        self.assertEqual(catalog["claude-opus-6"], ["high"])
        self.assertIn("claude-haiku-5-5", catalog)

    def test_extra_models_cannot_close_an_open_catalog(self):
        self.write({"engines": {"pi": {"extra_models": {"only-this": {"efforts": []}}}}})
        self.assertIsNone(config.supported_models("pi", get_engine("pi")))

    def test_extra_models_cannot_repeat_an_allowlist_entry(self):
        self.write({"engines": {"claude": {"models": {"claude-haiku-5-5": {}},
                                           "extra_models": {"claude-haiku-5-5": {}}}}})
        with self.assertRaises(ValueError) as caught:
            config.load()
        self.assertIn("already in engines.claude.models", str(caught.exception))

    def test_extra_models_rejects_a_bad_shape(self):
        self.write({"engines": {"claude": {"extra_models": {"m": {"efforts": "high"}}}}})
        with self.assertRaises(ValueError) as caught:
            config.load()
        self.assertIn("extra_models.m", str(caught.exception))


class Enforcement(Base):
    def test_a_tier_three_model_is_refused_with_the_flag_and_the_config_edit(self):
        self.write({"engines": {"claude": {"models": {"claude-haiku-5-5": {}}}}})
        with self.assertRaises(ValueError) as caught:
            config.check_engine("claude", "claude-opus-5-5")
        message = str(caught.exception)
        self.assertIn("not in the models allowlist", message)
        self.assertIn("--allow-model claude-opus-5-5", message)
        self.assertIn("engines.claude.models", message)

    def test_allow_permits_a_tier_three_model(self):
        self.write({"engines": {"claude": {"models": {"claude-haiku-5-5": {}}}}})
        config.check_engine("claude", "claude-opus-5-5", allow=("claude-opus-5-5",))

    def test_an_unknown_model_is_refused_with_the_catalog_hint(self):
        with self.assertRaises(ValueError) as caught:
            config.check_engine("claude", "claude-opus-9")
        message = str(caught.exception)
        self.assertIn("not in the claude catalog", message)
        self.assertIn("--allow-model claude-opus-9", message)
        self.assertIn("engines.claude.extra_models", message)

    def test_allow_permits_an_unknown_model(self):
        config.check_engine("claude", "claude-opus-9", allow=("claude-opus-9",))

    def test_an_allowed_model_still_passes(self):
        self.write({"engines": {"claude": {"models": {"claude-haiku-5-5": {}}}}})
        config.check_engine("claude", "claude-haiku-5-5")

    def test_an_extra_model_is_allowed_once_declared(self):
        self.write({"engines": {"claude": {"extra_models": {"claude-opus-6": {"efforts": ["high"]}}}}})
        config.check_engine("claude", "claude-opus-6", "high")

    def test_an_open_catalog_still_accepts_any_model(self):
        config.check_engine("pi", "anything-at-all")

    def test_a_disabled_model_is_still_refused_before_the_tier_check(self):
        self.write({"engines": {"claude": {"disabled_models": {"claude-opus-5-5": "reserved"}}}})
        with self.assertRaises(ValueError) as caught:
            config.check_engine("claude", "claude-opus-5-5", allow=("claude-opus-5-5",))
        self.assertIn("explicitly disabled", str(caught.exception))


class LaunchRecord(Base):
    def test_turn_request_carries_the_recorded_permission(self):
        state = {"schema_version": 2, "engine": "claude", "workspace": str(self.root),
                 "prompt_file": str(self.root / "task.md"), "reasoning_effort": "high",
                 "launch": {"binary": "/bin/true", "provider": "anthropic", "model": "claude-opus-5-5",
                            "read_only": True, "web": False, "output_schema": None,
                            "session_logging": True, "isolation": {}, "allowed_models": ["claude-opus-5-5"]}}
        self.assertEqual(turn_request(state).allowed_models, ("claude-opus-5-5",))

    def test_turn_request_defaults_to_no_permission(self):
        state = {"schema_version": 2, "engine": "claude", "workspace": str(self.root),
                 "prompt_file": str(self.root / "task.md"), "reasoning_effort": "high",
                 "launch": {"binary": "/bin/true", "provider": "anthropic", "model": "claude-haiku-5-5",
                            "read_only": True, "web": False, "output_schema": None,
                            "session_logging": True, "isolation": {}}}
        self.assertEqual(turn_request(state).allowed_models, ())


class EngineProvider(Base):
    def test_claude_accepts_the_configured_provider(self):
        self.write({"engines": {"claude": {"provider": "bedrock"}}})
        engine = get_engine("claude")
        request = type("Request", (), {"model": "claude-haiku-5-5", "provider": "bedrock",
                                       "reasoning_effort": "high", "max_model_steps": None,
                                       "read_only": True, "web": False, "output_schema": None,
                                       "session_logging": True, "isolation": {}, "steering": False,
                                       "fork_session_id": None, "budget": None})()
        engine.validate_turn(request)

    def test_codex_accepts_the_configured_provider(self):
        self.write({"engines": {"codex": {"provider": "azure"}}})
        engine = get_engine("codex")
        request = type("Request", (), {"model": "gpt-6.1-terra", "provider": "azure",
                                       "reasoning_effort": "xhigh", "max_model_steps": None,
                                       "read_only": True, "web": False, "output_schema": None,
                                       "session_logging": True, "isolation": {}, "steering": False,
                                       "fork_session_id": None, "budget": None})()
        engine.validate_turn(request)

    def test_codex_accepts_an_extra_model(self):
        self.write({"engines": {"codex": {"extra_models": {"gpt-7": {"efforts": ["high"]}}}}})
        engine = get_engine("codex")
        request = type("Request", (), {"model": "gpt-7", "provider": "openai",
                                       "reasoning_effort": "high", "max_model_steps": None,
                                       "read_only": True, "web": False, "output_schema": None,
                                       "session_logging": True, "isolation": {}, "steering": False,
                                       "fork_session_id": None, "budget": None})()
        engine.validate_turn(request)


class Surface(Base):
    def test_models_json_reports_the_three_tiers(self):
        self.write({"engines": {"claude": {"models": {"claude-haiku-5-5": {}}}}})
        result = self.run_cli("models", "claude", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        tiers = json.loads(result.stdout)["engines"]["claude"]
        self.assertEqual(tiers["default"], "claude-haiku-5-5")
        self.assertEqual(tiers["allowed"], ["claude-haiku-5-5"])
        self.assertIn("claude-opus-5-5", tiers["extra"])

    def test_models_human_output_names_the_opt_in(self):
        self.write({"engines": {"claude": {"models": {"claude-haiku-5-5": {}}}}})
        result = self.run_cli("models", "claude")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("allowed: claude-haiku-5-5", result.stdout)
        self.assertIn("--allow-model MODEL", result.stdout)

    def test_models_reports_every_engine(self):
        result = self.run_cli("models", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(json.loads(result.stdout)["engines"]), sorted(ENGINE_NAMES))

    def test_unknown_engine_is_refused(self):
        result = self.run_cli("models", "nope")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown worker engine", result.stderr)


if __name__ == "__main__":
    unittest.main()
