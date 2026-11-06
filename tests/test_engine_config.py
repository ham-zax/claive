import hermetic  # noqa: F401  (must run before claivelib reads the environment)
"""Uniform engine configuration: declared defaults, resolution, and the headless surface."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from claivelib import config
from claivelib.engines import ENGINE_NAMES, engine_binary, get_engine

CLI = Path(__file__).resolve().parents[1] / "bin/claive"
UNIFORM = ("binary", "provider", "default_model", "env", "prompt_limit")


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="claive-engine-config-")
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


class DeclaredDefaults(Base):
    def test_every_engine_declares_the_uniform_keys(self):
        for name in ENGINE_NAMES:
            defaults = get_engine(name).defaults()
            for key in UNIFORM:
                self.assertIn(key, defaults, f"{name} does not declare {key}")

    def test_declared_binary_is_absolute(self):
        for name in ENGINE_NAMES:
            self.assertTrue(Path(get_engine(name).defaults()["binary"]).is_absolute(), name)

    def test_declared_defaults_match_the_engine_attributes(self):
        for name in ENGINE_NAMES:
            engine, defaults = get_engine(name), get_engine(name).defaults()
            self.assertTrue(defaults["default_model"], name)
            if getattr(engine, "default_model", None) is not None:
                self.assertEqual(defaults["default_model"], engine.default_model, name)

    def test_agy_declares_its_prompt_limit(self):
        self.assertEqual(get_engine("agy").defaults()["prompt_limit"], 100000)

    def test_engines_without_a_limit_declare_none(self):
        for name in ("claude", "codex", "muse", "pi"):
            self.assertIsNone(get_engine(name).defaults()["prompt_limit"], name)


class Resolution(Base):
    def test_config_overrides_the_declared_default(self):
        self.write({"engines": {"claude": {"binary": "/opt/claude", "provider": "bedrock"}}})
        self.assertEqual(config.engine_setting("claude", "binary", "/default"), "/opt/claude")
        self.assertEqual(config.engine_setting("claude", "provider", "anthropic"), "bedrock")

    def test_absent_key_falls_back_to_the_declared_default(self):
        self.write({"engines": {"claude": {"provider": "bedrock"}}})
        self.assertEqual(config.engine_setting("claude", "binary", "/default"), "/default")

    def test_engine_env_is_empty_without_config(self):
        self.assertEqual(config.engine_env("claude"), {})

    def test_engine_env_returns_the_configured_mapping(self):
        self.write({"engines": {"claude": {"env": {"ANTHROPIC_LOG": "debug"}}}})
        self.assertEqual(config.engine_env("claude"), {"ANTHROPIC_LOG": "debug"})

    def test_engine_binary_uses_the_configured_path(self):
        self.write({"engines": {"codex": {"binary": "/opt/codex"}}})
        self.assertEqual(engine_binary("codex"), "/opt/codex")

    def test_engine_binary_falls_back_to_the_declared_default(self):
        self.assertEqual(engine_binary("codex"), get_engine("codex").defaults()["binary"])

    def test_effective_reports_the_source_of_every_key(self):
        self.write({"engines": {"claude": {"provider": "bedrock"}}})
        effective = config.engine_effective("claude", get_engine("claude"))
        self.assertEqual(effective["provider"], {"value": "bedrock", "source": "config"})
        self.assertEqual(effective["binary"]["source"], "default")
        self.assertEqual(effective["binary"]["value"], get_engine("claude").defaults()["binary"])

    def test_effective_includes_engine_only_keys(self):
        self.write({"engines": {"pi": {"default_provider": "mistral"}}})
        effective = config.engine_effective("pi", get_engine("pi"))
        self.assertEqual(effective["default_provider"], {"value": "mistral", "source": "config"})


class LaunchPath(Base):
    def test_claude_launch_uses_the_configured_binary_and_provider(self):
        self.write({"engines": {"claude": {"binary": "/opt/claude", "provider": "bedrock"}}})
        launch = get_engine("claude").resolve_launch()
        self.assertEqual(launch["binary"], "/opt/claude")
        self.assertEqual(launch["provider"], "bedrock")

    def test_an_explicit_provider_flag_still_wins(self):
        self.write({"engines": {"claude": {"provider": "bedrock"}}})
        self.assertEqual(get_engine("claude").resolve_launch(provider="vertex")["provider"], "vertex")

    def test_codex_launch_uses_the_configured_binary(self):
        self.write({"engines": {"codex": {"binary": "/opt/codex"}}})
        self.assertEqual(get_engine("codex").resolve_launch()["binary"], "/opt/codex")

    def test_agy_launch_uses_the_configured_binary_and_provider(self):
        self.write({"engines": {"agy": {"binary": "/opt/agy", "provider": "vertex"}}})
        launch = get_engine("agy").resolve_launch()
        self.assertEqual((launch["binary"], launch["provider"]), ("/opt/agy", "vertex"))

    def test_muse_launch_uses_the_configured_binary(self):
        self.write({"engines": {"muse": {"binary": "/opt/muse"}}})
        self.assertEqual(get_engine("muse").resolve_launch()["binary"], "/opt/muse")

    def test_pi_launch_uses_the_configured_binary(self):
        self.write({"engines": {"pi": {"binary": "/opt/pi"}}})
        self.assertEqual(get_engine("pi").resolve_launch()["binary"], "/opt/pi")

    def test_child_env_merges_the_configured_environment(self):
        self.write({"engines": {"claude": {"env": {"ANTHROPIC_LOG": "debug"}}}})
        merged = get_engine("claude").child_env_for_turn(None)
        self.assertEqual(merged["ANTHROPIC_LOG"], "debug")
        self.assertEqual(merged["CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD"], "1")

    def test_configured_environment_overrides_the_engine_default(self):
        self.write({"engines": {"claude": {"env": {"CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD": "0"}}}})
        self.assertEqual(get_engine("claude").child_env_for_turn(None)["CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD"], "0")

    def test_codex_web_turn_keeps_its_home_and_gains_the_configured_environment(self):
        self.write({"engines": {"codex": {"env": {"OPENAI_LOG": "debug"}}}})
        engine = get_engine("codex")
        request = type("Request", (), {"model": "chatgpt-web"})()
        merged = engine.child_env_for_turn(request)
        self.assertEqual(merged["OPENAI_LOG"], "debug")
        self.assertIn("CODEX_HOME", merged)

    def test_agy_prompt_limit_comes_from_the_config(self):
        self.write({"engines": {"agy": {"prompt_limit": 10}}})
        with self.assertRaises(ValueError) as caught:
            get_engine("agy").validate_prompt("x" * 11)
        self.assertIn("limit is 10 bytes", str(caught.exception))

    def test_agy_prompt_limit_default_still_applies(self):
        get_engine("agy").validate_prompt("x" * 1000)


class Validation(Base):
    def assert_invalid(self, data, fragment):
        self.write(data)
        with self.assertRaises(ValueError) as caught:
            config.load()
        self.assertIn(fragment, str(caught.exception))

    def test_relative_binary_is_refused(self):
        self.assert_invalid({"engines": {"claude": {"binary": "bin/claude"}}}, "binary must be an absolute")

    def test_empty_binary_is_refused(self):
        self.assert_invalid({"engines": {"claude": {"binary": ""}}}, "binary must be an absolute")

    def test_empty_provider_is_refused(self):
        self.assert_invalid({"engines": {"claude": {"provider": ""}}}, "provider must be a nonempty name")

    def test_provider_with_a_space_is_refused(self):
        self.assert_invalid({"engines": {"claude": {"provider": "two words"}}}, "without spaces")

    def test_invalid_environment_name_is_refused(self):
        self.assert_invalid({"engines": {"claude": {"env": {"1BAD": "x"}}}}, "not an environment variable name")

    def test_non_string_environment_value_is_refused(self):
        self.assert_invalid({"engines": {"claude": {"env": {"GOOD": 1}}}}, "must be a string")

    def test_zero_prompt_limit_is_refused(self):
        self.assert_invalid({"engines": {"agy": {"prompt_limit": 0}}}, "prompt_limit must be a positive integer")

    def test_boolean_prompt_limit_is_refused(self):
        self.assert_invalid({"engines": {"agy": {"prompt_limit": True}}}, "prompt_limit must be a positive integer")

    def test_uniform_keys_are_accepted_by_every_engine(self):
        self.write({"engines": {name: {"binary": f"/opt/{name}", "provider": "p",
                                       "env": {"A": "b"}, "prompt_limit": 5} for name in ENGINE_NAMES}})
        self.assertEqual(sorted(config.load()["engines"]), sorted(ENGINE_NAMES))


class HeadlessSurface(Base):
    def test_engine_config_reports_every_engine(self):
        result = self.run_cli("engine", "config", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        engines = json.loads(result.stdout)["engines"]
        self.assertEqual(sorted(engines), sorted(ENGINE_NAMES))
        for name, settings in engines.items():
            self.assertEqual(settings["binary"]["source"], "default", name)

    def test_engine_config_reports_one_engine_with_its_sources(self):
        self.write({"engines": {"claude": {"provider": "bedrock"}}})
        result = self.run_cli("engine", "config", "claude", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        settings = json.loads(result.stdout)["engines"]["claude"]
        self.assertEqual(settings["provider"], {"value": "bedrock", "source": "config"})

    def test_engine_config_human_output_names_the_source(self):
        self.write({"engines": {"claude": {"provider": "bedrock"}}})
        result = self.run_cli("engine", "config", "claude")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("provider = bedrock (config)", result.stdout)
        self.assertIn("binary = ", result.stdout)

    def test_unknown_engine_is_refused(self):
        result = self.run_cli("engine", "config", "nope")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown worker engine", result.stderr)

    def test_engines_json_carries_the_effective_config(self):
        self.write({"engines": {"codex": {"binary": "/opt/codex"}}})
        result = self.run_cli("engines", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = {row["engine"]: row for row in json.loads(result.stdout)["engines"]}
        self.assertEqual(rows["codex"]["config"]["binary"], {"value": "/opt/codex", "source": "config"})

    def test_doctor_checks_the_configured_binary(self):
        self.write({"engines": {"claude": {"binary": str(self.root / "absent-claude")}}})
        result = self.run_cli("doctor", "--json")
        rows = {row["name"]: row for row in json.loads(result.stdout)["checks"]}
        self.assertFalse(rows["claude"]["ok"])
        self.assertIn("absent-claude", rows["claude"]["detail"])


if __name__ == "__main__":
    unittest.main()
