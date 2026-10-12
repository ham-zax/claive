"""Exercise the real Claive supervisor against a deterministic Codex App Server protocol fixture."""
import hermetic  # noqa: F401
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
from claivelib.engines.codex import CodexEngine
from claivelib.engine import TurnRequest

THREAD = "01a12e77-d81b-7c61-8ceb-22cbea15eb32"
TURN = "01a12e77-d81b-7c61-8ceb-22cbea15eb33"

FIXTURE = '''#!/usr/bin/env python3
import json,sys
THREAD="01a12e77-d81b-7c61-8ceb-22cbea15eb32"
TURN="01a12e77-d81b-7c61-8ceb-22cbea15eb33"
def emit(v):print(json.dumps(v),flush=True)
for line in sys.stdin:
 m=json.loads(line);method=m.get("method");identifier=m.get("id")
 if method=="initialize":emit(dict(id=identifier,result={"userAgent":"fixture"}))
 if method in ("thread/start","thread/resume"):
  assert m["params"]["model"]=="gpt-6-luna" and m["params"]["modelProvider"]=="openai"
  if method=="thread/resume":assert m["params"]["threadId"]==THREAD
  emit(dict(id=identifier,result={"thread":{"id":THREAD}}))
 if method=="turn/start":
  assert m["params"]["threadId"]==THREAD
  emit(dict(id=identifier,result={"turn":{"id":TURN,"status":"inProgress"}}))
  emit(dict(method="turn/started",params={"threadId":THREAD,"turn":{"id":TURN,"status":"inProgress"}}))
  emit(dict(method="item/started",params={"threadId":THREAD,"turnId":TURN,"item":{"type":"commandExecution","id":"toolA","command":"echo running"}}))
 if method=="turn/steer":
  assert m["params"]["threadId"]==THREAD and m["params"]["expectedTurnId"]==TURN
  message=m["params"]["input"][0]["text"]
  if message=="reject":emit(dict(id=identifier,error={"code":-32602,"message":"not steerable"}));continue
  if message=="no-ack":
   emit(dict(method="turn/completed",params={"threadId":THREAD,"turn":{"id":TURN,"status":"completed"}}));break
  emit(dict(id=identifier,result={"turnId":TURN}))
  emit(dict(method="item/completed",params={"threadId":THREAD,"turnId":TURN,"item":{"type":"agentMessage","id":"messageA","text":"steered "+message}}))
  emit(dict(method="turn/completed",params={"threadId":THREAD,"turn":{"id":TURN,"status":"completed"}}));break
'''


class CodexSteeringChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="claive-codex-steer-")
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name)
        self.binary = self.workspace / "codex"
        self.binary.write_text(FIXTURE)
        self.binary.chmod(0o755)
        self.prompt = self.workspace / "prompt.md"
        self.prompt.write_text("work on task")
        self.home = self.workspace / "codex-home"
        self.home.mkdir()
        self.registry = self.workspace / "state"
        self.env = dict(os.environ, CLAIVE_DIR=str(self.registry), CODEX_HOME=str(self.home),
                        CODEX_WORKER_BINARY=str(self.binary))
        self.launcher = str(Path(__file__).resolve().parents[1] / "bin" / "claive")
        self.identifiers = []
        self.addCleanup(self.stop)

    def cli(self, *args):
        return subprocess.run([self.launcher, *args], env=self.env, capture_output=True, text=True, timeout=16)

    def stop(self):
        for identifier in self.identifiers:
            self.cli("cancel", identifier)
            self.cli("wait", identifier, "--timeout", "4")

    def launch(self, action="start"):
        args = [action, "--engine", "codex", "--enable-steering", "--workspace", str(self.workspace),
                "--prompt-file", str(self.prompt)]
        if action == "open":
            args.append("--detach")
        result = self.cli(*args)
        self.assertEqual(result.returncode, 0, result.stderr)
        identifier = re.search(r"Worker ([0-9a-f]{12})", result.stdout)[1]
        self.identifiers.append(identifier)
        for _ in range(100):
            state = json.loads(self.cli("show", identifier, "--json").stdout)
            if state.get("last_event") == "item.started" and state.get("status") == "running":
                return identifier
            time.sleep(0.03)
        self.fail(f"Codex app server turn did not start: {state}")

    def test_codex_app_server_same_turn_steering(self):
        identifier = self.launch()
        result = self.cli("steer", identifier, "--message", "new requirement", "--wait", "5", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "acknowledged")
        self.assertEqual(self.cli("wait", identifier, "--timeout", "5").returncode, 0)
        self.assertIn("steered new requirement", (self.registry / identifier / "result.txt").read_text())
        state = json.loads(self.cli("show", identifier, "--json").stdout)
        self.assertEqual(state["steering"]["acknowledged"], 1)
        self.assertEqual(state["queue_depth"], 0)
        self.assertEqual(state["session_id"], THREAD)
        self.assertTrue((self.home / "sessions").is_dir())

    def test_codex_rejected_steering_is_not_accepted(self):
        identifier = self.launch()
        result = self.cli("steer", identifier, "--message", "reject", "--wait", "5", "--json")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "rejected")
        self.assertEqual(json.loads(self.cli("show", identifier, "--json").stdout)["status"], "running")

    def test_codex_turn_end_before_ack_is_unconfirmed(self):
        identifier = self.launch()
        result = self.cli("steer", identifier, "--message", "no-ack", "--wait", "5", "--json")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "unconfirmed")

    def test_codex_resume_reuses_thread_with_new_receipt(self):
        identifier = self.launch("open")
        self.assertEqual(self.cli("steer", identifier, "--message", "first", "--wait", "5").returncode, 0)
        self.assertEqual(self.cli("wait", identifier, "--timeout", "5").returncode, 0)
        rollout = self.home / "sessions" / f"rollout-{THREAD}.jsonl"
        rollout.parent.mkdir(parents=True, exist_ok=True)
        rollout.write_text("")
        self.assertEqual(self.cli("followup", identifier, "--prompt-file", str(self.prompt)).returncode, 0)
        for _ in range(100):
            state = json.loads(self.cli("show", identifier, "--json").stdout)
            if state.get("turn") == 2 and state.get("last_event") == "item.started" and state.get("status") == "running":
                break
            time.sleep(0.03)
        self.assertEqual(state["turn"], 2)
        self.assertEqual(self.cli("steer", identifier, "--message", "second", "--wait", "5").returncode, 0)
        self.assertEqual(self.cli("wait", identifier, "--timeout", "5").returncode, 0)
        self.assertEqual((self.registry / identifier / "turn-0002.txt").read_text(), "steered second")
        self.assertEqual(json.loads(self.cli("show", identifier, "--json").stdout)["steering"]["acknowledged"], 1)

    def test_opted_user_config_is_explicit_and_rules_are_not_linked(self):
        from claivelib.engines.codex_appserver import home_for_server
        source = self.workspace / "source-home"
        source.mkdir()
        (source / "auth.json").write_text("fixture auth")
        (source / "config.toml").write_text("fixture config")
        (source / "skills").mkdir()
        (source / "rules").mkdir()
        default = self.workspace / "default-home"
        default.mkdir()
        home_for_server(source, False, default)
        self.assertTrue((default / "auth.json").is_symlink())
        self.assertTrue((default / "sessions").is_symlink())
        self.assertFalse((default / "config.toml").exists())
        self.assertFalse((default / "rules").exists())
        opted = self.workspace / "opted-home"
        opted.mkdir()
        home_for_server(source, True, opted)
        self.assertTrue((opted / "config.toml").is_symlink())
        self.assertTrue((opted / "skills").is_symlink())
        self.assertFalse((opted / "rules").exists())

    def test_command_switches_transport_only_with_opt_in(self):
        engine = CodexEngine()
        base = dict(binary=str(self.binary), workspace=str(self.workspace), prompt_file=str(self.prompt),
                    session_id=None, provider="openai", model="gpt-6-luna", reasoning_effort="max",
                    max_model_steps=None, read_only=True, web=False, output_schema=None,
                    session_logging=False, isolation={})
        plain = engine.build_command(TurnRequest(**base))
        self.assertEqual(plain[1], "exec")
        command = engine.build_command(TurnRequest(**base, steering=True))
        self.assertIn("codex_appserver.py", command[1])
        self.assertIn("read-only", command)
        self.assertIn("--ephemeral", command)


if __name__ == "__main__":
    unittest.main()
