"""The config file: YAML and JSON, discovery, validation and the config commands."""
import hermetic  # noqa: F401  (must run before claivelib reads the environment)
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from claivelib import config

CLI = Path(__file__).resolve().parents[1] / "bin" / "claive"


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="claive-config-file-")
        self.root = Path(self.temp.name)
        self.xdg = self.root / "xdg"
        self.directory = self.xdg / "claive"
        self.directory.mkdir(parents=True)
        self.previous = {name: os.environ.get(name) for name in ("CLAIVE_CONFIG", "XDG_CONFIG_HOME")}
        os.environ.pop("CLAIVE_CONFIG", None)
        os.environ["XDG_CONFIG_HOME"] = str(self.xdg)

    def tearDown(self):
        config.set_overrides()
        for name, value in self.previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self.temp.cleanup()

    def write(self, name, text):
        path = self.directory / name
        path.write_text(text, encoding="utf-8")
        return path

    def run_cli(self, *arguments):
        return subprocess.run([sys.executable, str(CLI), *arguments], capture_output=True, text=True,
                              timeout=120, env=dict(os.environ))


class Discovery(Base):
    def test_the_default_name_is_yaml(self):
        self.assertEqual(config.config_path(), self.directory / "config.yaml")

    def test_yaml_wins_over_json(self):
        self.write("config.json", json.dumps({"default_engine": "codex"}))
        self.write("config.yaml", "default_engine: pi\n")
        self.assertEqual(config.config_path().name, "config.yaml")
        self.assertEqual(config.load()["default_engine"], "pi")

    def test_yml_is_accepted(self):
        self.write("config.yml", "default_engine: pi\n")
        self.assertEqual(config.config_path().name, "config.yml")
        self.assertEqual(config.load()["default_engine"], "pi")

    def test_json_still_loads(self):
        self.write("config.json", json.dumps({"default_engine": "pi"}))
        self.assertEqual(config.load()["default_engine"], "pi")

    def test_explicit_path_wins(self):
        self.write("config.yaml", "default_engine: pi\n")
        elsewhere = self.root / "other.yaml"
        elsewhere.write_text("default_engine: codex\n")
        os.environ["CLAIVE_CONFIG"] = str(elsewhere)
        self.assertEqual(config.config_path(), elsewhere)
        self.assertEqual(config.load()["default_engine"], "codex")

    def test_an_unknown_suffix_is_sniffed(self):
        path = self.root / "config"
        path.write_text("default_engine: pi\n")
        self.assertEqual(config.load_text(path.read_text(), path)["default_engine"], "pi")
        path.write_text(json.dumps({"default_engine": "codex"}))
        self.assertEqual(config.load_text(path.read_text(), path)["default_engine"], "codex")

    def test_shadowed_files_are_listed(self):
        self.write("config.yaml", "default_engine: pi\n")
        self.write("config.json", "{}")
        self.assertEqual([item.name for item in config.config_files()], ["config.yaml", "config.json"])


class Parsing(Base):
    def test_empty_file_is_an_empty_config(self):
        self.write("config.yaml", "")
        self.assertEqual(config.load(), {})

    def test_a_non_mapping_is_refused(self):
        self.write("config.yaml", "- a\n- b\n")
        with self.assertRaises(ValueError) as caught:
            config.load()
        self.assertIn("must be a YAML or JSON object", str(caught.exception))

    def test_a_duplicate_key_is_refused(self):
        self.write("config.yaml", "default_engine: pi\ndefault_engine: codex\n")
        with self.assertRaises(ValueError) as caught:
            config.load()
        self.assertIn("duplicate key 'default_engine'", str(caught.exception))

    def test_invalid_yaml_names_the_file(self):
        self.write("config.yaml", "engines: [\n")
        with self.assertRaises(ValueError) as caught:
            config.load()
        self.assertIn(str(self.directory / "config.yaml"), str(caught.exception))

    def test_yaml_without_pyyaml_names_the_install_command(self):
        self.write("config.yaml", "default_engine: pi\n")
        with mock.patch.object(config, "yaml", None):
            with self.assertRaises(ValueError) as caught:
                config.load()
        self.assertIn("PyYAML is not installed", str(caught.exception))
        self.assertIn("python3-yaml", str(caught.exception))

    def test_json_without_pyyaml_still_loads(self):
        self.write("config.json", json.dumps({"default_engine": "pi"}))
        with mock.patch.object(config, "yaml", None):
            self.assertEqual(config.load()["default_engine"], "pi")

    def test_the_template_validates(self):
        data = config.load_text(config.TEMPLATE, self.directory / "config.yaml")
        self.assertEqual(sorted(data["engines"]), ["agy", "claude", "codex", "muse", "pi"])


class Providers(Base):
    def test_a_disabled_provider_is_refused_on_every_engine(self):
        for engine in ("claude", "codex", "agy", "muse", "pi"):
            with self.subTest(engine=engine):
                self.write("config.yaml", f"engines:\n  {engine}:\n    providers:\n      label:\n"
                                          "        enabled: false\n        reason: retired\n")
                with self.assertRaises(ValueError) as caught:
                    config.check_provider(engine, "label")
                self.assertIn("retired", str(caught.exception))
                config.check_provider(engine, "other")

    def test_an_enabled_provider_is_accepted(self):
        self.write("config.yaml", "engines:\n  claude:\n    providers:\n      bedrock:\n        enabled: true\n")
        config.check_provider("claude", "bedrock")

    def test_a_disabled_provider_cannot_be_the_engine_provider(self):
        self.write("config.yaml", "engines:\n  claude:\n    provider: bedrock\n    providers:\n"
                                  "      bedrock:\n        enabled: false\n")
        with self.assertRaises(ValueError) as caught:
            config.load()
        self.assertIn("engines.claude.provider bedrock is disabled", str(caught.exception))

    def test_a_disabled_default_provider_is_refused(self):
        self.write("config.yaml", "engines:\n  pi:\n    default_provider: mistral\n    providers:\n"
                                  "      mistral:\n        enabled: false\n")
        with self.assertRaises(ValueError) as caught:
            config.load()
        self.assertIn("engines.pi.default_provider mistral is disabled", str(caught.exception))


class ModelEfforts(Base):
    def test_an_effort_is_disabled_for_one_model_only(self):
        self.write("config.yaml", "engines:\n  claude:\n    disabled_model_efforts:\n"
                                  "      claude-haiku-5-5: {max: too slow}\n")
        with self.assertRaises(ValueError) as caught:
            config.check_engine("claude", "claude-haiku-5-5", "max")
        self.assertIn("reasoning effort max for model claude-haiku-5-5 on worker engine claude",
                      str(caught.exception))
        self.assertIn("too slow", str(caught.exception))
        config.check_engine("claude", "claude-haiku-5-5", "high")
        config.check_engine("claude", "claude-sonnet-5-5", "max")

    def test_the_engine_wide_key_names_the_replacement(self):
        self.write("config.yaml", "engines:\n  fixture:\n    disabled_reasoning_efforts: [max]\n")
        with self.assertRaises(ValueError) as caught:
            config.load()
        self.assertIn("disabled_model_efforts", str(caught.exception))

    def test_a_default_effort_cannot_be_disabled_for_its_model(self):
        self.write("config.yaml", "engines:\n  fixture:\n    default_model: small\n"
                                  "    default_model_efforts:\n      small: max\n"
                                  "    disabled_model_efforts:\n      small: [max]\n")
        with self.assertRaises(ValueError) as caught:
            config.load()
        self.assertIn("default_model_efforts.small max is disabled", str(caught.exception))

    def test_disabling_every_supported_effort_is_refused(self):
        self.write("config.yaml", "engines:\n  codex:\n    disabled_model_efforts:\n"
                                  "      gpt-6-luna: [medium, max]\n")
        result = self.run_cli("config", "check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("disables every effort", result.stdout)

    def test_an_unknown_effort_for_a_known_model_is_refused(self):
        self.write("config.yaml", "engines:\n  codex:\n    disabled_model_efforts:\n"
                                  "      gpt-6-luna: [ultra]\n")
        result = self.run_cli("config", "check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("ultra not supported by the engine", result.stdout)


class Overrides(Base):
    def test_an_override_merges_over_the_file(self):
        self.write("config.yaml", "engines:\n  pi:\n    enabled: true\n")
        config.set_overrides(config.parse_override("{engines: {pi: {default_model: chosen}}}"))
        data = config.load()
        self.assertEqual(data["engines"]["pi"]["default_model"], "chosen")
        self.assertIs(data["engines"]["pi"]["enabled"], True)

    def test_a_later_layer_wins(self):
        config.set_overrides(config.parse_override("{default_engine: pi}"),
                             config.parse_override("{default_engine: codex}"))
        self.assertEqual(config.load()["default_engine"], "codex")

    def test_a_list_replaces_instead_of_merging(self):
        self.write("config.yaml", "engines:\n  pi:\n    disabled_models: [a, b]\n")
        config.set_overrides(config.parse_override("{engines: {pi: {disabled_models: [c]}}}"))
        self.assertEqual(config.load()["engines"]["pi"]["disabled_models"], ["c"])

    def test_an_invalid_merged_config_is_refused(self):
        config.set_overrides(config.parse_override("{engines: {pi: {enabled: 'yes'}}}"))
        with self.assertRaises(ValueError) as caught:
            config.load()
        self.assertIn("with --set overrides", str(caught.exception))

    def test_a_non_object_override_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            config.parse_override("[1, 2]")
        self.assertIn("must be a YAML or JSON object", str(caught.exception))

    def test_an_override_does_not_change_the_file(self):
        path = self.write("config.yaml", "default_engine: pi\n")
        config.set_overrides(config.parse_override("{default_engine: codex}"))
        self.assertEqual(config.load()["default_engine"], "codex")
        self.assertEqual(path.read_text(encoding="utf-8"), "default_engine: pi\n")


class Commands(Base):
    def test_path_prints_the_active_file(self):
        self.write("config.yaml", "default_engine: pi\n")
        result = self.run_cli("config", "path")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), str(self.directory / "config.yaml"))

    def test_check_reports_ok(self):
        self.write("config.yaml", "default_engine: pi\n")
        result = self.run_cli("config", "check")
        self.assertEqual(result.returncode, 0)
        self.assertIn("ok", result.stdout)
        self.assertIn("yaml", result.stdout)

    def test_check_fails_with_the_error(self):
        self.write("config.yaml", "engines: {claude: {enabled: 'yes'}}\n")
        result = self.run_cli("config", "check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("FAIL", result.stdout)
        self.assertIn("engines.claude.enabled", result.stdout)

    def test_check_json(self):
        self.write("config.yaml", "default_engine: pi\n")
        result = self.run_cli("config", "check", "--json")
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["default_engine"], "pi")

    def test_check_runs_the_allowlist_cross_check(self):
        self.write("config.yaml", "engines:\n  claude:\n    models:\n"
                                  "      not-a-claude-model:\n        efforts: [high]\n")
        result = self.run_cli("config", "check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("not supported by the engine", result.stdout)

    def test_show_prints_the_file(self):
        self.write("config.yaml", "default_engine: pi\n")
        result = self.run_cli("config", "show")
        self.assertEqual(result.stdout.strip(), "default_engine: pi")

    def test_show_json_prints_the_object(self):
        self.write("config.yaml", "default_engine: pi\n")
        result = self.run_cli("config", "show", "--json")
        self.assertEqual(json.loads(result.stdout), {"default_engine": "pi"})

    def test_show_refuses_a_missing_file(self):
        result = self.run_cli("config", "show")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no claive config file", result.stderr)

    def test_init_writes_a_valid_template_and_refuses_overwrite(self):
        result = self.run_cli("config", "init")
        self.assertEqual(result.returncode, 0)
        self.assertTrue((self.directory / "config.yaml").is_file())
        self.assertIs(config.load()["engines"]["pi"]["enabled"], True)
        again = self.run_cli("config", "init")
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("already exists", again.stderr)
        self.assertEqual(self.run_cli("config", "init", "--force").returncode, 0)

    def test_init_refuses_to_shadow_an_existing_file(self):
        self.write("config.json", json.dumps({"default_engine": "pi"}))
        result = self.run_cli("config", "init")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("config migrate", result.stderr)

    def test_migrate_converts_and_keeps_a_backup(self):
        self.write("config.json", json.dumps({"default_engine": "pi",
                                              "engines": {"pi": {"enabled": True}}}))
        result = self.run_cli("config", "migrate")
        self.assertEqual(result.returncode, 0)
        self.assertTrue((self.directory / "config.yaml").is_file())
        self.assertFalse((self.directory / "config.json").exists())
        self.assertEqual(len(list(self.directory.glob("config.json.bak-*"))), 1)
        self.assertEqual(config.load()["default_engine"], "pi")
        self.assertEqual(self.run_cli("config", "check").returncode, 0)

    def test_migrate_refuses_a_yaml_source(self):
        self.write("config.yaml", "default_engine: pi\n")
        result = self.run_cli("config", "migrate")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already YAML", result.stderr)
