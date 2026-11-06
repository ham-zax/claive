import hermetic  # noqa: F401  (must run before claivelib reads the environment)
"""Pi multi-provider support: models.json reads, discovery, the safe writer, and policy."""
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest

from claivelib import config, provider
from claivelib.cli import split_pi_model

CLI = Path(__file__).resolve().parents[1] / "bin/claive"

TABLE = """provider      model                        context  max-out  thinking  images
deepseek      deepseek-flash               1M       384K     yes       yes
mistral       codestral-2508               256K     8.2K     no        no
mistral       mistral-large-4              1.0M     262.1K   yes       yes
opencode2api  big-pickle                   200K     32K      yes       no
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="claive-multiprovider-")
        self.root = Path(self.temp.name)
        self.agent = self.root / "agent"
        self.agent.mkdir()
        self.models = self.agent / "models.json"
        self.config = self.root / "config.json"
        self.previous = {name: os.environ.get(name) for name in ("PI_CODING_AGENT_DIR", "CLAIVE_CONFIG")}
        os.environ["PI_CODING_AGENT_DIR"] = str(self.agent)
        os.environ["CLAIVE_CONFIG"] = str(self.config)
        self.write_models({
            "opencode2api": {"baseUrl": "http://127.0.0.1:13339/v1", "api": "openai-completions",
                             "apiKey": "sk-test", "models": [{"id": "big-pickle"}]},
            "mistral": {"baseUrl": "https://api.mistral.ai/v1", "api": "mistral-conversations",
                        "models": [{"id": "mistral-large-4"}]},
        })

    def tearDown(self):
        for name, value in self.previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self.temp.cleanup()

    def write_models(self, data):
        self.models.write_text(json.dumps({"providers": data}))

    def write_config(self, data):
        self.config.write_text(json.dumps(data))

    def fake_pi(self, body):
        path = self.root / "pi"
        path.write_text("#!/usr/bin/python3\n" + body)
        path.chmod(0o755)
        return str(path)


class Reads(Base):
    def test_reads_a_named_provider(self):
        _data, entry = provider.read_models(name="mistral")
        self.assertEqual(entry["baseUrl"], "https://api.mistral.ai/v1")

    def test_default_provider_is_still_opencode2api(self):
        _data, entry = provider.read_models()
        self.assertEqual(entry["baseUrl"], "http://127.0.0.1:13339/v1")

    def test_unknown_provider_is_refused(self):
        with self.assertRaises(ValueError):
            provider.read_models(name="absent")

    def test_provider_names_lists_every_entry(self):
        self.assertEqual(provider.provider_names(), ["mistral", "opencode2api"])

    def test_provider_names_is_empty_without_a_file(self):
        self.models.unlink()
        self.assertEqual(provider.provider_names(), [])


class Overlay(Base):
    def test_every_overridden_provider_is_rewritten_in_one_overlay(self):
        self.write_config({"providers": {"opencode2api": {"base_url": "http://127.0.0.1:9/v1"},
                                         "mistral": {"base_url": "http://localhost:8/v1"}}})
        overlay = provider.overlay_agent_dir(self.root / "state")
        data = json.loads((Path(overlay) / "models.json").read_text())
        self.assertEqual(data["providers"]["opencode2api"]["baseUrl"], "http://127.0.0.1:9/v1")
        self.assertEqual(data["providers"]["mistral"]["baseUrl"], "http://localhost:8/v1")
        self.assertEqual(stat.S_IMODE((Path(overlay) / "models.json").stat().st_mode), 0o600)
        # The real file is untouched.
        self.assertEqual(json.loads(self.models.read_text())["providers"]["mistral"]["baseUrl"],
                         "https://api.mistral.ai/v1")

    def test_an_override_for_an_undefined_provider_is_ignored(self):
        self.write_config({"providers": {"absent": {"base_url": "http://127.0.0.1:9/v1"}}})
        self.assertIsNone(provider.overlay_agent_dir(self.root / "state"))

    def test_no_override_builds_no_overlay(self):
        self.assertIsNone(provider.overlay_agent_dir(self.root / "state"))


class Discovery(Base):
    def test_catalog_parses_the_table(self):
        binary = self.fake_pi(f"import sys\nsys.stdout.write({TABLE!r})\n")
        catalog = provider.catalog_models(binary)
        self.assertEqual(catalog["mistral"], ["codestral-2508", "mistral-large-4"])
        self.assertEqual(catalog["deepseek"], ["deepseek-flash"])

    def test_catalog_degrades_on_an_unrecognized_table(self):
        binary = self.fake_pi("import sys\nsys.stdout.write('something else entirely\\n')\n")
        self.assertEqual(provider.catalog_models(binary), {})

    def test_catalog_degrades_when_pi_is_missing(self):
        self.assertEqual(provider.catalog_models(str(self.root / "absent")), {})

    def test_auth_check_parses_json(self):
        binary = self.fake_pi("import sys\nsys.stdout.write('{\"status\":\"ready\",\"provider\":\"mistral\","
                              "\"authType\":\"api_key\"}')\n")
        self.assertEqual(provider.auth_check("mistral", binary)["status"], "ready")

    def test_auth_check_returns_none_on_garbage(self):
        binary = self.fake_pi("import sys\nsys.stdout.write('not json')\n")
        self.assertIsNone(provider.auth_check("mistral", binary))

    def test_list_providers_merges_models_json_and_the_catalog(self):
        binary = self.fake_pi("import sys\n"
                              "if sys.argv[1:] == ['--list-models']:\n"
                              f"    sys.stdout.write({TABLE!r})\n"
                              "else:\n"
                              "    sys.stdout.write('{\"status\":\"ready\",\"authType\":\"api_key\"}')\n")
        rows = {row["provider"]: row for row in provider.list_providers(binary)}
        self.assertEqual(rows["mistral"]["source"], "models.json")
        self.assertEqual(rows["deepseek"]["source"], "built-in")
        self.assertTrue(rows["mistral"]["ready"])
        self.assertEqual(rows["mistral"]["auth_type"], "api_key")


class Writer(Base):
    def test_write_is_atomic_private_and_backed_up(self):
        provider.write_models({"providers": {"only": {"baseUrl": "http://127.0.0.1:1/v1"}}})
        self.assertEqual(stat.S_IMODE(self.models.stat().st_mode), 0o600)
        backups = list(self.agent.glob("models.json.bak-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(stat.S_IMODE(backups[0].stat().st_mode), 0o600)
        self.assertIn("mistral", backups[0].read_text())

    def test_add_creates_a_provider_with_an_environment_reference(self):
        provider.add_provider("acme", "https://api.acme.example/v1", "openai-completions",
                              ["acme-chat"], api_key_env="ACME_API_KEY", display_name="Acme AI")
        entry = json.loads(self.models.read_text())["providers"]["acme"]
        self.assertEqual(entry["apiKey"], "$ACME_API_KEY")
        self.assertEqual(entry["name"], "Acme AI")
        self.assertEqual(entry["models"], [{"id": "acme-chat"}])

    def test_add_refuses_an_existing_provider_without_replace(self):
        with self.assertRaises(ValueError):
            provider.add_provider("mistral", "https://api.mistral.ai/v1", "mistral-conversations",
                                  ["mistral-large-4"])
        provider.add_provider("mistral", "https://api.mistral.ai/v1", "mistral-conversations",
                              ["mistral-large-4"], replace=True)

    def test_add_refuses_bad_input(self):
        for arguments in (
            dict(name="Bad Name", base_url="https://x/v1", api="openai-completions", models=["m"]),
            dict(name="ok", base_url="ftp://x/v1", api="openai-completions", models=["m"]),
            dict(name="ok", base_url="https://x/v1", api="not-a-protocol", models=["m"]),
            dict(name="ok", base_url="https://x/v1", api="openai-completions", models=[]),
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                provider.add_provider(**arguments)

    def test_remove_deletes_the_entry_and_reports_a_builtin(self):
        # The fake catalog lists models.json providers plus one built-in, like Pi does.
        binary = self.fake_pi(
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "if sys.argv[1:] != ['--list-models']:\n"
            "    sys.exit(0)\n"
            "agent = Path(os.environ['PI_CODING_AGENT_DIR'])\n"
            "defined = list(json.loads((agent / 'models.json').read_text())['providers'])\n"
            "print('provider      model                        context  max-out  thinking  images')\n"
            "for name in sorted(set(defined) | {'deepseek'}):\n"
            "    print(f'{name:<13} {name}-model                   1M       1K       no        no')\n")
        previous = os.environ.get("PI_WORKER_BINARY")
        os.environ["PI_WORKER_BINARY"] = binary
        try:
            self.assertFalse(provider.remove_provider("mistral"))
            self.assertFalse(provider.remove_provider("opencode2api"))
            provider.add_provider("deepseek", "https://api.deepseek.com", "openai-completions", ["deepseek-flash"])
            self.assertTrue(provider.remove_provider("deepseek"))
        finally:
            if previous is None:
                os.environ.pop("PI_WORKER_BINARY", None)
            else:
                os.environ["PI_WORKER_BINARY"] = previous
        self.assertEqual(provider.provider_names(), [])

    def test_remove_refuses_an_undefined_provider(self):
        with self.assertRaises(ValueError):
            provider.remove_provider("absent")


class Policy(Base):
    def test_a_disabled_provider_is_refused_with_its_reason(self):
        self.write_config({"engines": {"pi": {"providers": {"opencode2api": {
            "enabled": False, "reason": "gateway retired"}}}}})
        with self.assertRaises(ValueError) as caught:
            config.check_provider("pi", "opencode2api")
        self.assertIn("gateway retired", str(caught.exception))
        config.check_provider("pi", "mistral")

    def test_default_provider_prefers_policy_then_settings(self):
        self.write_config({"engines": {"pi": {"default_provider": "mistral"}}})
        self.assertEqual(config.pi_default_provider(), "mistral")
        self.write_config({})
        (self.agent / "settings.json").write_text(json.dumps({"defaultProvider": "deepseek"}))
        self.assertEqual(config.pi_default_provider(), "deepseek")
        (self.agent / "settings.json").write_text("{}")
        self.assertEqual(config.pi_default_provider(), "opencode2api")

    def test_policy_validation_rejects_bad_shapes(self):
        for policy in ({"providers": {"mistral": {"enabled": "yes"}}},
                       {"providers": {"mistral": {"reason": 3}}},
                       {"providers": {"mistral": {"unknown": True}}},
                       {"providers": {"Bad Name": {"enabled": True}}},
                       {"default_provider": "Bad Name"}):
            with self.subTest(policy=policy):
                self.write_config({"engines": {"pi": policy}})
                with self.assertRaises(ValueError):
                    config.load()

    def test_provider_policy_applies_to_every_engine(self):
        self.write_config({"engines": {"claude": {"providers": {"bedrock": {"enabled": False}}}}})
        self.assertFalse(config.provider_policy("claude", "bedrock")["enabled"])
        with self.assertRaises(ValueError):
            config.check_provider("claude", "bedrock")
        config.check_provider("claude", "anthropic")


class Identity(unittest.TestCase):
    def test_a_bare_model_keeps_the_provider(self):
        self.assertEqual(split_pi_model("mistral-large-4", "mistral"), ("mistral", "mistral-large-4"))
        self.assertEqual(split_pi_model("mistral-large-4", None), (None, "mistral-large-4"))

    def test_a_slash_names_both(self):
        self.assertEqual(split_pi_model("mistral/mistral-large-4", None), ("mistral", "mistral-large-4"))
        self.assertEqual(split_pi_model("mistral/mistral-large-4", "mistral"), ("mistral", "mistral-large-4"))

    def test_a_conflicting_provider_is_refused(self):
        with self.assertRaises(ValueError):
            split_pi_model("mistral/mistral-large-4", "opencode2api")

    def test_a_malformed_slash_is_refused(self):
        for model in ("mistral/", "/mistral-large-4"):
            with self.subTest(model=model), self.assertRaises(ValueError):
                split_pi_model(model, None)


class Commands(Base):
    def claive(self, *arguments):
        return subprocess.run([str(CLI), *arguments], capture_output=True, text=True,
                              env=dict(os.environ), timeout=120)

    def test_list_shows_source_and_default(self):
        result = self.claive("pi", "provider", "list")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("opencode2api (default)", result.stdout)
        self.assertIn("models.json", result.stdout)

    def test_add_then_list_then_remove(self):
        result = self.claive("pi", "provider", "add", "acme", "--base-url", "https://api.acme.example/v1",
                             "--api", "openai-completions", "--api-key-env", "ACME_API_KEY",
                             "--model", "acme-chat")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("$ACME_API_KEY", result.stdout)
        self.assertIn("acme", self.claive("pi", "provider", "list").stdout)
        removed = self.claive("pi", "provider", "remove", "acme")
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertNotIn("acme", self.claive("pi", "provider", "list").stdout)

    def test_add_refuses_a_duplicate_without_replace(self):
        result = self.claive("pi", "provider", "add", "mistral", "--base-url", "https://api.mistral.ai/v1",
                             "--api", "mistral-conversations", "--model", "mistral-large-4")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--replace", result.stderr)

    def test_check_refuses_an_unknown_provider(self):
        result = self.claive("pi", "provider", "check", "absent")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown Pi provider", result.stderr)

    def test_check_reports_readiness(self):
        binary = self.fake_pi("import sys\n"
                              "if sys.argv[1:3] == ['auth', 'check']:\n"
                              "    sys.stdout.write('{\"status\":\"ready\",\"authType\":\"api_key\"}')\n"
                              "elif sys.argv[1:] == ['--list-models']:\n"
                              f"    sys.stdout.write({TABLE!r})\n")
        os.environ["PI_WORKER_BINARY"] = binary
        result = self.claive("pi", "provider", "check", "mistral", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ready"])
        self.assertEqual(payload["auth_type"], "api_key")


class Orchestration(Base):
    def test_the_arbiter_uses_the_configured_default_provider(self):
        from claivelib import orchestration
        self.write_config({"engines": {"pi": {"default_provider": "mistral"}}})
        self.assertEqual(orchestration.pi_provider(), "mistral")
        self.assertEqual(orchestration.implementer_choice(False, True, configured_engine="pi")["provider"],
                         "mistral")
        self.assertEqual(orchestration.implementer_choice(False, True)["provider"], "mistral")

    def test_the_arbiter_falls_back_to_opencode2api(self):
        from claivelib import orchestration
        self.write_config({})
        self.assertEqual(orchestration.pi_provider(), "opencode2api")


if __name__ == "__main__":
    unittest.main()
