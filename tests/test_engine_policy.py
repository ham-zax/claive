"""The user's per-engine policy in the claive config is enforced, not advisory."""
import hermetic  # noqa: F401  (must run before claivelib reads the environment)
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import unittest

from test_workers import FIXTURE_CLI, FIXTURE_WORKER


class EnginePolicyChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.registry = self.path / "registry"
        self.prompt = self.path / "task.md"
        self.prompt.write_text("Do the task.\n")
        self.text = self.path / "text.txt"
        self.text.write_text("fixture result")
        self.config = self.path / "config.json"
        self.env = dict(os.environ, CLAIVE_DIR=str(self.registry), CLAIVE_CONFIG=str(self.config), TERM="dumb",
                        FIXTURE_WORKER_BINARY=FIXTURE_WORKER, FIXTURE_MODE="success",
                        FIXTURE_TEXT_FILE=str(self.text))
        self.addCleanup(self.temp.cleanup)

    def policy(self, **engines):
        self.config.write_text(json.dumps({"engines": engines}))

    def cli(self, *args, timeout=15):
        return subprocess.run([FIXTURE_CLI, *args], env=self.env, text=True, capture_output=True, timeout=timeout)

    def launch(self, *extra, action="run"):
        return self.cli(action, "--engine", "fixture", "--workspace", str(self.path),
                        "--prompt-file", str(self.prompt), *extra)

    def jobs(self):
        return list(self.registry.glob("*/state.json")) if self.registry.exists() else []

    def test_disabled_engine_is_refused_with_the_reason(self):
        self.policy(fixture={"enabled": False, "reason": "quota is for interactive use"})
        result = self.launch()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("worker engine fixture is explicitly disabled by the user", result.stderr)
        self.assertIn("quota is for interactive use", result.stderr)
        self.assertFalse(self.jobs())
        self.policy(fixture={"enabled": False})
        self.assertNotIn("reason:", self.launch().stderr)
        self.policy(fixture={"enabled": True})
        self.assertEqual(self.launch().returncode, 0)

    def test_disabled_default_engine_is_refused(self):
        self.policy(fixture={"enabled": False})
        self.env["CLAIVE_ENGINE"] = "fixture"
        result = self.cli("run", "--workspace", str(self.path), "--prompt-file", str(self.prompt))
        self.assertIn("explicitly disabled by the user", result.stderr)
        self.config.write_text(json.dumps({"default_engine": "fixture", "engines": {"fixture": {"enabled": False}}}))
        self.assertIn("default_engine fixture is disabled", self.launch().stderr)

    def test_disabled_models_and_efforts_are_refused(self):
        self.policy(fixture={"disabled_models": {"Big-Model": "too costly"}, "disabled_reasoning_efforts": ["max"]})
        result = self.launch("--model", "big-model")
        self.assertIn("model big-model on worker engine fixture is explicitly disabled", result.stderr)
        self.assertIn("too costly", result.stderr)
        result = self.launch("--reasoning-effort", "max")
        self.assertIn("reasoning effort max on worker engine fixture is explicitly disabled", result.stderr)
        self.assertFalse(self.jobs())
        self.assertEqual(self.launch("--model", "small", "--reasoning-effort", "high").returncode, 0)

    def test_configured_defaults_apply_below_flags(self):
        self.policy(fixture={"default_model": "chosen", "default_reasoning_effort": "low"})
        self.assertEqual(self.launch().returncode, 0)
        state = json.loads(self.jobs()[0].read_text())
        self.assertEqual((state["launch"]["model"], state["reasoning_effort"]), ("chosen", "low"))
        self.assertEqual(self.launch("--model", "other", "--reasoning-effort", "high").returncode, 0)
        pairs = {(s["launch"]["model"], s["reasoning_effort"]) for s in map(json.loads, (p.read_text() for p in self.jobs()))}
        self.assertEqual(pairs, {("chosen", "low"), ("other", "high")})

    def test_policy_applies_to_a_worker_that_is_already_open(self):
        process = subprocess.Popen([FIXTURE_CLI, "open", "--engine", "fixture", "--workspace", str(self.path),
                                    "--prompt-file", str(self.prompt)], env=self.env, text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(process.kill)
        deadline = time.time() + 10
        while time.time() < deadline and not any(
                json.loads(p.read_text()).get("status") == "idle" for p in self.jobs()):
            time.sleep(0.05)
        job = self.jobs()[0].parent.name
        self.policy(fixture={"disabled_reasoning_efforts": ["max"]})
        self.assertIn("explicitly disabled", self.cli("effort", job, "--reasoning-effort", "max").stderr)
        self.policy(fixture={"enabled": False, "reason": "paused"})
        refused = self.cli("followup", job, "--prompt-file", str(self.prompt))
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("paused", refused.stderr)
        self.assertEqual(self.cli("close", job).returncode, 0)
        process.communicate(timeout=10)

    def test_invalid_policy_fails_closed(self):
        for engines, message in (
                ({"fixtrue": {"enabled": False}}, "engines.fixtrue: unknown worker engine"),
                ({"fixture": {"enable": False}}, "unknown key enable"),
                ({"fixture": {"enabled": "no"}}, "wrong type"),
                ({"fixture": {"default_model": "m", "disabled_models": ["M"]}}, "does not disable"),
                ({"fixture": {"disabled_reasoning_efforts": [3]}}, "nonempty names")):
            self.policy(**engines)
            result = self.launch()
            self.assertNotEqual(result.returncode, 0, engines)
            self.assertIn(message, result.stderr)
        self.assertFalse(self.jobs())

    def test_models_allowlist_limits_models_and_efforts(self):
        self.policy(fixture={"models": {"small": {"efforts": ["low", "high"]}, "open": {}}})
        result = self.launch("--model", "big")
        self.assertIn("model big on worker engine fixture is not in the models allowlist", result.stderr)
        self.assertIn("allowed: small, open", result.stderr)
        result = self.launch("--model", "small", "--reasoning-effort", "max")
        self.assertIn("reasoning effort max for model small on worker engine fixture is not allowed", result.stderr)
        self.assertIn("allowed: low, high", result.stderr)
        self.assertIn("pass --model", self.launch().stderr)
        self.assertFalse(self.jobs())
        self.assertEqual(self.launch("--model", "small", "--reasoning-effort", "high").returncode, 0)
        self.assertEqual(self.launch("--model", "open", "--reasoning-effort", "max").returncode, 0)
        self.policy(fixture={"models": {"small": {"efforts": ["low"]}}, "default_model": "small",
                             "default_reasoning_effort": "low"})
        self.assertEqual(self.launch().returncode, 0)

    def test_models_allowlist_applies_to_an_open_worker(self):
        self.policy(fixture={"models": {"small": {}}})
        process = subprocess.Popen([FIXTURE_CLI, "open", "--engine", "fixture", "--workspace", str(self.path),
                                    "--prompt-file", str(self.prompt), "--model", "small"], env=self.env,
                                   text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(process.kill)
        deadline = time.time() + 10
        while time.time() < deadline and not any(
                json.loads(p.read_text()).get("status") == "idle" for p in self.jobs()):
            time.sleep(0.05)
        job = self.jobs()[0].parent.name
        self.policy(fixture={"models": {"other": {}}})
        refused = self.cli("followup", job, "--prompt-file", str(self.prompt))
        self.assertIn("not in the models allowlist", refused.stderr)
        self.assertEqual(self.cli("close", job).returncode, 0)
        process.communicate(timeout=10)

    def test_models_allowlist_cannot_widen_the_engine(self):
        for engines, message in (
                ({"codex": {"models": {"gpt-9": {}}}}, "gpt-9 not supported by the engine"),
                ({"codex": {"models": {"gpt-6-luna": {"efforts": ["max", "high"]}}}},
                 "high not supported by the engine for gpt-6-luna"),
                ({"claude": {"models": {"claude-haiku-5-5": {"efforts": ["ultra"]}}}}, "ultra not supported"),
                ({"fixture": {"models": {"a": {"effort": ["low"]}}}}, "must be {\"efforts\""),
                ({"fixture": {"models": {"a": {"efforts": [""]}}}}, "must be {\"efforts\""),
                ({"fixture": {"models": {"a": {}}, "default_model": "b"}}, "default_model must be one of"),
                ({"fixture": {"models": {"a": {"efforts": ["low"]}}, "default_model": "a",
                              "default_reasoning_effort": "max"}}, "efforts of default_model")):
            self.policy(**engines)
            result = self.launch("--model", "a")
            self.assertNotEqual(result.returncode, 0, engines)
            self.assertIn(message, result.stderr)
        self.assertFalse(self.jobs())

    def test_engines_command_reports_the_models(self):
        self.policy(codex={"models": {"gpt-6-luna": {}, "gpt-6.1-sol": {"efforts": ["low", "xhigh"]}}})
        rows = {row["engine"]: row for row in json.loads(self.cli("engines", "--json").stdout)["engines"]}
        self.assertEqual(rows["codex"]["models"], {"gpt-6-luna": ["max"], "gpt-6.1-sol": ["low", "xhigh"]})
        efforts = ["low", "medium", "high", "xhigh", "max"]
        self.assertEqual(rows["claude"]["models"], {model: efforts for model in
                         ("claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-1")})
        self.assertIsNone(rows["pi"]["models"])
        self.assertIn("models gpt-6-luna[max], gpt-6.1-sol[low,xhigh]", self.cli("engines").stdout)

    def test_engines_command_reports_the_policy(self):
        self.policy(claude={"enabled": False, "reason": "paused"},
                    pi={"default_reasoning_effort": "high", "disabled_reasoning_efforts": ["max"]})
        rows = {row["engine"]: row for row in json.loads(self.cli("engines", "--json").stdout)["engines"]}
        self.assertEqual((rows["claude"]["enabled"], rows["claude"]["reason"]), (False, "paused"))
        self.assertEqual((rows["pi"]["default_reasoning_effort"], rows["pi"]["steering"]), ("high", "pi-rpc"))
        text = self.cli("engines").stdout
        self.assertTrue(re.search(r"^claude DISABLED \(paused\)$", text, re.M), text)
        self.assertIn("no effort max", text)
        doctor = {c["name"]: c for c in json.loads(self.cli("doctor", "--json").stdout)["checks"]}
        self.assertEqual(doctor["claude"]["detail"], "disabled by user: paused")

    def test_extra_args_reach_the_command_before_the_prompt_separator(self):
        self.policy(fixture={"extra_args": ["--extra-flag", "value"]})
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads(self.jobs()[0].read_text())
        command = state["command"]
        start = command.index("--extra-flag")
        self.assertEqual(command[start + 1], "value")
        if "--" in command:
            self.assertLess(start, command.index("--"))

    def test_extra_args_refuse_owned_flags_and_bad_values(self):
        cases = [("claude", ["--model=other"], "claive owns --model"),
                 ("codex", ["-m", "x"], "claive owns -m"),
                 ("pi", ["--thinking", "low"], "claive owns --thinking"),
                 ("muse", ["--session-id"], "claive owns --session-id"),
                 ("claude", ["--"], "claive owns --"),
                 ("claude", [""], "extra_args must hold nonempty strings"),
                 ("claude", "--tools", "extra_args has the wrong type")]
        for engine, value, message in cases:
            with self.subTest(engine=engine, value=value):
                self.policy(**{engine: {"extra_args": value}})
                result = self.cli("engines", "--json")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
        self.policy(claude={"extra_args": ["--tools", "Read,Grep,Glob,Bash", "--permission-mode", "plan"]})
        self.assertEqual(self.cli("engines", "--json").returncode, 0)

    def test_codex_user_config_accepts_only_booleans(self):
        for value in (True, False):
            with self.subTest(value=value):
                self.policy(codex={"user_config": value})
                result = self.cli("engines", "--json")
                self.assertEqual(result.returncode, 0, result.stderr)
        for value in ("true", 1, 0, None, [], {}):
            with self.subTest(value=value):
                self.policy(codex={"user_config": value})
                result = self.cli("engines", "--json")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"invalid claive config {self.config}: engines.codex.user_config has the wrong type",
                              result.stderr)

    def test_user_config_is_rejected_on_other_engines(self):
        for engine in ("claude", "pi", "muse", "fixture"):
            for value in (True, False):
                with self.subTest(engine=engine, value=value):
                    self.policy(**{engine: {"user_config": value}})
                    result = self.cli("engines", "--json")
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(f"invalid claive config {self.config}: engines.{engine}.user_config is only for codex",
                                  result.stderr)

    def test_orchestration_picks_skip_models_the_user_disabled(self):
        script = ("import sys; sys.path.insert(0, sys.argv[1])\n"
                  "from claivelib import orchestration as o\n"
                  "print(o.pick_critic({}, 'none', 0, roster=['alpha', 'beta'], reserve=[])['model'])")
        self.policy(pi={"disabled_models": {"alpha": "too slow"}})
        result = subprocess.run(["python3", "-c", script, str(Path(__file__).resolve().parents[1] / "bin")], env=self.env,
                                text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "beta")
        self.policy(pi={"models": {"beta": {}}})
        result = subprocess.run(["python3", "-c", script, str(Path(__file__).resolve().parents[1] / "bin")], env=self.env,
                                text=True, capture_output=True, timeout=15)
        self.assertEqual(result.stdout.strip(), "beta", result.stderr)


if __name__ == "__main__":
    unittest.main()
