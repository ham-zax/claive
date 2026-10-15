"""Exercise the real claive supervisor against a deterministic Claude stream-json fixture."""
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
from claivelib.engines.claude import ClaudeEngine
from claivelib.engine import TurnRequest

# Replays every stdin user message like `claude --replay-user-messages`, keeping a supplied uuid.
FIXTURE = '''#!/usr/bin/env python3
import json,sys
def emit(v):print(json.dumps(v),flush=True)
assert "--input-format" in sys.argv and "--replay-user-messages" in sys.argv and "--" not in sys.argv
emit(dict(type="system",subtype="init"))
first=json.loads(sys.stdin.readline())
emit(dict(first,isReplay=True,uuid="00000000-0000-4000-8000-000000000001"))
emit(dict(type="assistant",message={"content":[{"type":"tool_use","name":"Bash","id":"toolA"}]}))
steer=json.loads(sys.stdin.readline())
text=steer["message"]["content"]
if text!="no-ack":emit(dict(steer,isReplay=True))
emit(dict(type="assistant",message={"content":[{"type":"text","text":"steered "+text}]}))
emit(dict(type="result",subtype="success",is_error=False,result="steered "+text))
'''


class ClaudeSteeringChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="claive-claude-steer-")
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name)
        self.binary = self.workspace / "claude"
        self.binary.write_text(FIXTURE)
        self.binary.chmod(0o755)
        self.prompt = self.workspace / "prompt.md"
        self.prompt.write_text("work on task")
        self.registry = self.workspace / "state"
        self.env = dict(os.environ, CLAIVE_DIR=str(self.registry), CLAUDE_WORKER_BINARY=str(self.binary),
                        CLAUDE_CONFIG_DIR=str(self.workspace / "config"))
        self.launcher = str(Path(__file__).resolve().parents[1] / "bin" / "claive")
        self.identifiers = []
        self.addCleanup(self.stop)

    def cli(self, *args):
        return subprocess.run([self.launcher, *args], env=self.env, capture_output=True, text=True, timeout=16)

    def stop(self):
        for identifier in self.identifiers:
            self.cli("cancel", identifier)
            self.cli("wait", identifier, "--timeout", "4")

    def launch(self):
        result = self.cli("start", "--engine", "claude", "--enable-steering", "--workspace", str(self.workspace),
                          "--prompt-file", str(self.prompt))
        self.assertEqual(result.returncode, 0, result.stderr)
        identifier = re.search(r"Worker ([0-9a-f]{12})", result.stdout)[1]
        self.identifiers.append(identifier)
        for _ in range(100):
            state = json.loads(self.cli("show", identifier, "--json").stdout)
            if state.get("active_tools") or state.get("last_event") == "assistant":
                return identifier
            time.sleep(0.03)
        self.fail(f"Claude turn did not start: {state}")

    def test_same_turn_steering_is_acknowledged_by_replay(self):
        identifier = self.launch()
        result = self.cli("steer", identifier, "--message", "new requirement", "--wait", "5", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual((receipt["status"], receipt["disposition"]), ("acknowledged", "entered the conversation"))
        self.assertEqual(self.cli("wait", identifier, "--timeout", "5").returncode, 0)
        self.assertEqual((self.registry / identifier / "result.txt").read_text().strip(), "steered new requirement")
        state = json.loads(self.cli("show", identifier, "--json").stdout)
        self.assertEqual(state["steering"]["acknowledged"], 1)
        self.assertEqual(state["steering"]["capability"]["mechanism"], "claude-stream-json")

    def test_turn_end_before_replay_is_unconfirmed(self):
        identifier = self.launch()
        result = self.cli("steer", identifier, "--message", "no-ack", "--wait", "5", "--json")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "unconfirmed")

    def test_command_switches_transport_only_with_opt_in(self):
        engine = ClaudeEngine()
        base = dict(binary=str(self.binary), workspace=str(self.workspace), prompt_file=str(self.prompt),
                    session_id=None, provider="anthropic", model="claude-haiku-5-5", reasoning_effort="high",
                    max_model_steps=None, read_only=True, web=False, output_schema=None,
                    session_logging=False, isolation={})
        plain = engine.build_command(TurnRequest(**base))
        self.assertEqual(plain[-2:], ["--", "work on task"])
        self.assertNotIn("--input-format", plain)
        self.assertIsNone(engine.initial_input(TurnRequest(**base)))
        duplex = TurnRequest(**base, steering=True)
        command = engine.build_command(duplex)
        self.assertEqual(command[-3:], ["--input-format", "stream-json", "--replay-user-messages"])
        self.assertEqual(command[:-3], plain[:-2])
        for read_only in (False, True):
            with self.subTest(read_only=read_only):
                options = dict(base, read_only=read_only, steering=True)
                steered = engine.build_command(TurnRequest(**options))
                self.assertEqual(steered[steered.index("--tools") + 1], "default")
                self.assertEqual(steered[steered.index("--permission-mode") + 1],
                                 "plan" if read_only else "bypassPermissions")
                self.assertEqual(steered[steered.index("--setting-sources") + 1], "")
                self.assertNotIn("--strict-mcp-config", steered)
                self.assertNotIn("--disable-slash-commands", steered)
                self.assertEqual(engine.build_command(TurnRequest(**dict(options, web=True))), steered)
        self.assertNotIn("work on task", command)
        self.assertEqual(json.loads(engine.initial_input(duplex)),
                         {"type": "user", "message": {"role": "user", "content": "work on task"}})
        frame = json.loads(engine.steering_frame("0123456789abcdef0123456789abcdef", "go left"))
        self.assertEqual(frame["uuid"], "01234567-89ab-cdef-0123-456789abcdef")
        echoed = engine.normalize_event(dict(frame, isReplay=True))[0]
        self.assertEqual((echoed["type"], echoed["id"], echoed["success"]),
                         ("control_response", "0123456789abcdef0123456789abcdef", True))


if __name__ == "__main__":
    unittest.main()
