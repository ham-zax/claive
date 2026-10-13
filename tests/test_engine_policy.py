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

    def test_orchestration_picks_skip_models_the_user_disabled(self):
        script = ("import sys; sys.path.insert(0, sys.argv[1])\n"
                  "from claivelib import orchestration as o\n"
                  "print(o.pick_critic({}, 'none', 0, roster=['alpha', 'beta'], reserve=[])['model'])")
        self.policy(pi={"disabled_models": {"alpha": "too slow"}})
        result = subprocess.run(["python3", "-c", script, str(Path(__file__).resolve().parents[1] / "bin")], env=self.env,
                                text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "beta")


if __name__ == "__main__":
    unittest.main()
