import hermetic  # noqa: F401  (must run before claivelib reads the environment)
"""Doctor's Pi checks (binary runs, models listed, endpoint latency, --live) and the loopback override."""
import http.server
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import unittest

REPO = Path(__file__).resolve().parents[1]
FAKE_PI = REPO / "tests/fake_pi.py"
MODELS = ["muse-spark-1.3-contributor-free", "big-pickle", "mimo-v2.6-flash-free", "space-bunny-free"]


class Models(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"data": [{"id": name} for name in MODELS]}).encode()
        self.send_response(200 if self.path.endswith("/models") else 404)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.server.hits.append(self.headers.get("Authorization"))

    def log_message(self, *_args):
        pass


class ProviderChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="claive-provider-")
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.prompt = self.root / "task.md"
        self.prompt.write_text("look around\n")
        self.agent = self.root / "agent"
        self.agent.mkdir()
        (self.agent / "settings.json").write_text("{}")
        self.servers = []
        self.remote = self.serve()
        self.write_models(f"http://127.0.0.1:{self.remote.server_port}/v1", MODELS)
        self.config = self.root / "config.json"
        self.env = dict(os.environ, CLAIVE_DIR=str(self.root / "state"), CLAIVE_CONFIG=str(self.config),
                        PI_CODING_AGENT_DIR=str(self.agent), PI_WORKER_BINARY=str(FAKE_PI))

    def tearDown(self):
        for server in self.servers:
            server.shutdown()
            server.server_close()
        self.temp.cleanup()

    def serve(self):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Models)
        server.hits = []
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.servers.append(server)
        return server

    def write_models(self, url, models):
        self.models_text = json.dumps({"providers": {"opencode2api": {
            "baseUrl": url, "api": "openai-completions", "apiKey": "sk-test-secret",
            "models": [{"id": name} for name in models]}}})
        (self.agent / "models.json").write_text(self.models_text)

    def claive(self, *args, **env):
        return subprocess.run([str(REPO / "bin/claive"), *args], env=dict(self.env, **env), text=True,
                              capture_output=True, timeout=30)

    def doctor(self, *args, **env):
        result = self.claive("doctor", "--json", *args, **env)
        report = json.loads(result.stdout)
        return result.returncode, {check["name"]: check for check in report["checks"]}

    def test_doctor_runs_pi_and_requires_its_models_when_pi_is_default(self):
        code, checks = self.doctor(CLAIVE_ENGINE="pi")
        self.assertEqual(code, 0, checks)
        self.assertIn("(1.0.4)", checks["pi"]["detail"])
        self.assertTrue(checks["pi"]["required"])
        self.assertTrue(checks["pi_provider"]["ok"])
        self.assertTrue(checks["pi_provider"]["required"])
        self.assertNotIn("sk-test-secret", json.dumps(checks))

        self.write_models(f"http://127.0.0.1:{self.remote.server_port}/v1", ["big-pickle"])
        code, checks = self.doctor(CLAIVE_ENGINE="pi")
        self.assertEqual(code, 1)
        self.assertIn("not listed: muse-spark-1.3-contributor-free", checks["pi_provider"]["detail"])
        code, checks = self.doctor()  # Muse default: Pi problems only warn
        self.assertFalse(checks["pi_provider"]["required"])
        self.assertFalse(checks["pi"]["required"])

        broken = self.root / "pi-broken"
        broken.write_text("#!/bin/sh\nexit 9\n")
        broken.chmod(0o755)
        code, checks = self.doctor(CLAIVE_ENGINE="pi", PI_WORKER_BINARY=str(broken))
        self.assertEqual(code, 1)
        self.assertIn("--version exited 9", checks["pi"]["detail"])

    def test_endpoint_latency_and_loopback_override(self):
        _code, checks = self.doctor()
        self.assertTrue(checks["opencode2api"]["ok"], checks["opencode2api"])
        self.assertRegex(checks["opencode2api"]["detail"], r"models\.json http://127\.0\.0\.1:\d+/v1: \d+ ms, 4 models")
        self.assertEqual(self.remote.hits, ["Bearer sk-test-secret"])

        local = self.serve()
        override = f"http://localhost:{local.server_port}/v1"
        self.config.write_text(json.dumps({"providers": {"opencode2api": {"base_url": override}}}))
        _code, checks = self.doctor()
        detail = checks["opencode2api"]["detail"]
        self.assertIn(f"override {override}:", detail)
        self.assertIn("Pi turns use the override", detail)
        self.assertEqual(len(local.hits), 1)

        for bad in ("http://89-168-87-96.sslip.io/v1", "ftp://127.0.0.1/v1", "http://10.0.0.1/v1"):
            self.config.write_text(json.dumps({"providers": {"opencode2api": {"base_url": bad}}}))
            code, checks = self.doctor()
            self.assertEqual(code, 1, bad)
            self.assertIn("base_url", checks["config"]["detail"], bad)
        self.config.write_text(json.dumps({"providers": {"other": {"base_url": override}}}))
        self.assertFalse(self.doctor()[1]["config"]["ok"])

    def test_pi_turns_use_the_override_without_editing_models_json(self):
        override = "http://127.0.0.1:9/v1"
        self.config.write_text(json.dumps({"providers": {"opencode2api": {"base_url": override}}}))
        record = self.root / "seen-url"
        result = self.claive("run", "--engine", "pi", "--read-only", "--workspace", str(self.workspace),
                             "--prompt-file", str(self.prompt), PI_TEST_RECORD=str(record))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(record.read_text(), override)
        self.assertEqual((self.agent / "models.json").read_text(), self.models_text)
        self.assertIn(f"Provider override: {override}", result.stdout)
        job = re.search(r"Worker ([0-9a-f]{12})", result.stdout).group(1)
        state = json.loads((self.root / "state" / job / "state.json").read_text())
        self.assertEqual((state["provider_base_url"], state["launch"]["provider_base_url"]), (override, override))
        overlay = self.root / "state/pi-agent-overlay"
        self.assertEqual(oct((overlay / "models.json").stat().st_mode & 0o777), "0o600")
        self.assertEqual(os.readlink(overlay / "settings.json"), str(self.agent.resolve() / "settings.json"))
        # From inside a Pi worker the agent dir is the overlay itself; its links must survive.
        result = self.claive("run", "--engine", "pi", "--read-only", "--workspace", str(self.workspace),
                             "--prompt-file", str(self.prompt), PI_TEST_RECORD=str(record),
                             PI_CODING_AGENT_DIR=str(overlay))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(os.readlink(overlay / "settings.json"), str(self.agent.resolve() / "settings.json"))
        self.assertEqual(record.read_text(), override)

        self.config.write_text("{}")
        result = self.claive("run", "--engine", "pi", "--read-only", "--workspace", str(self.workspace),
                             "--prompt-file", str(self.prompt), PI_TEST_RECORD=str(record))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(record.read_text(), f"http://127.0.0.1:{self.remote.server_port}/v1")
        self.assertNotIn("Provider override", result.stdout)

    def test_live_ping_sends_one_sessionless_read_only_turn(self):
        record = self.root / "seen-url"
        code, checks = self.doctor("--live", CLAIVE_ENGINE="pi", PI_TEST_RECORD=str(record))
        self.assertEqual(code, 0, checks)
        self.assertTrue(checks["live"]["ok"], checks["live"])
        self.assertTrue(checks["live"]["required"])
        self.assertRegex(checks["live"]["detail"], r"^muse-spark-1\.3-contributor-free answered in \d")
        self.assertTrue(record.exists())
        _code, checks = self.doctor()
        self.assertNotIn("live", checks)
        code, checks = self.doctor("--live", CLAIVE_ENGINE="pi", PI_TEST_MODE="failure")
        self.assertEqual(code, 1)
        self.assertFalse(checks["live"]["ok"])


if __name__ == "__main__":
    unittest.main()
