"""End-to-end duplex steering, distinct from queued reusable turns."""
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
from claivelib import cli
from claivelib.engines.pi import PiEngine
from claivelib.engine import TurnRequest

RPC_FIXTURE = '''#!/usr/bin/python3
import json,sys,time
from pathlib import Path
mode = sys.argv[sys.argv.index('--mode')+1]
def emit(value):
 print(json.dumps(value),flush=True)
if mode != 'rpc':
 emit(dict(type='message_end',message=dict(role='assistant',stopReason='stop',content=[dict(type='text',text='default turn')])))
 emit(dict(type='agent_settled'))
 sys.exit(0)
initial=json.loads(sys.stdin.readline())
emit(dict(type='response',id=initial['id'],command='prompt',success=True))
emit(dict(type='agent_start'))
emit(dict(type='message_start',message=dict(role='assistant')))
if initial['message'] == 'settle':
 emit(dict(type='message_end',message=dict(role='assistant',stopReason='stop',content=[dict(type='text',text='settled')])))
 emit(dict(type='agent_settled'))
 for line in sys.stdin: pass
 sys.exit(0)
for line in sys.stdin:
 request=json.loads(line)
 if request['type']!='steer': continue
 ok=request['message']!='reject'
 if request['message']=='no-ack':
  emit(dict(type='message_end',message=dict(role='assistant',stopReason='stop',content=[dict(type='text',text='unconfirmed')])))
  emit(dict(type='agent_settled'))
  continue
 emit(dict(type='response',id=request['id'],command='steer',success=ok,data=dict(disposition='queued') if ok else None,error=None if ok else 'rejected input'))
 if not ok: continue
 emit(dict(type='message_end',message=dict(role='assistant',stopReason='stop',content=[dict(type='text',text='steered: '+request['message'])])))
 emit(dict(type='agent_settled'))
'''


class SteeringChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.binary = self.root / "pi"
        self.binary.write_text(RPC_FIXTURE)
        self.binary.chmod(0o755)
        self.prompt = self.root / "prompt"
        self.prompt.write_text("wait for steering")
        self.registry = self.root / "state"
        self.env = dict(os.environ, CLAIVE_DIR=str(self.registry), PI_WORKER_BINARY=str(self.binary))
        self.launcher = str(Path(__file__).resolve().parents[1] / "bin" / "claive")
        self.identifiers = []
        self.addCleanup(self.stop)

    def run_cli(self, *args):
        return subprocess.run([self.launcher, *args], env=self.env, text=True, capture_output=True, timeout=15)

    def stop(self):
        for identifier in self.identifiers:
            self.run_cli("cancel", identifier)
            self.run_cli("wait", identifier, "--timeout", "5")

    def start(self, enabled=True, action="start"):
        command = [action, "--engine", "pi", "--workspace", str(self.root), "--prompt-file", str(self.prompt)]
        if action == "open":
            command.append("--detach")
        if enabled:
            command.append("--enable-steering")
        result = self.run_cli(*command)
        self.assertEqual(result.returncode, 0, result.stderr)
        identifier = re.search(r"Worker ([0-9a-f]{12})", result.stdout)[1]
        self.identifiers.append(identifier)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = json.loads(self.run_cli("show", identifier, "--json").stdout)
            if state.get("last_event") == "message_start" or state["status"] not in {"starting", "running"}:
                return identifier
            time.sleep(0.03)
        self.fail("fixture did not start")

    def test_steer_acknowledged_and_changes_same_turn_result(self):
        identifier = self.start()
        result = self.run_cli("steer", identifier, "--message", "updated task", "--wait", "5", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["status"], "acknowledged")
        self.assertEqual(receipt["turn"], 1)
        self.assertEqual(self.run_cli("wait", identifier, "--timeout", "5").returncode, 0)
        state = json.loads(self.run_cli("show", identifier, "--json").stdout)
        self.assertEqual(state["turn"], 1)
        self.assertEqual(state["queue_depth"], 0)
        self.assertEqual(state["steering"]["acknowledged"], 1)
        self.assertIn("steered: updated task", (self.registry / identifier / "result.txt").read_text())

    def test_rejected_provider_input_is_not_claimed_delivered(self):
        identifier = self.start()
        result = self.run_cli("steer", identifier, "--message", "reject", "--wait", "5", "--json")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["status"], "rejected")
        state = json.loads(self.run_cli("show", identifier, "--json").stdout)
        self.assertEqual(state["status"], "running")
        self.assertEqual(state["queue_depth"], 0)

    def test_no_ack_terminal_is_unconfirmed(self):
        identifier = self.start()
        result = self.run_cli("steer", identifier, "--message", "no-ack", "--wait", "5", "--json")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["status"], "unconfirmed")
        self.assertEqual(self.run_cli("wait", identifier, "--timeout", "5").returncode, 0)

    def test_default_transport_remains_print_and_rejects_steering(self):
        self.prompt.write_text("settle")
        identifier = self.start(False)
        state = json.loads(self.run_cli("show", identifier, "--json").stdout)
        self.assertIn("--print", state["command"])
        self.assertFalse(state["steering"]["enabled"])
        self.assertNotEqual(self.run_cli("steer", identifier, "--message", "later").returncode, 0)

    def test_reusable_new_turn_requires_new_steering_receipt(self):
        identifier = self.start(action="open")
        first = self.run_cli("steer", identifier, "--message", "one", "--wait", "5", "--json")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(self.run_cli("wait", identifier, "--timeout", "5").returncode, 0)
        self.assertNotEqual(self.run_cli("steer", identifier, "--message", "idle").returncode, 0)
        self.assertEqual(self.run_cli("followup", identifier, "--prompt-file", str(self.prompt)).returncode, 0)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = json.loads(self.run_cli("show", identifier, "--json").stdout)
            if state["turn"] == 2 and state.get("last_event") == "message_start":
                break
            time.sleep(0.03)
        second = self.run_cli("steer", identifier, "--message", "two", "--wait", "5", "--json")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(json.loads(second.stdout)["turn"], 2)
        self.assertEqual(self.run_cli("wait", identifier, "--timeout", "5").returncode, 0)
        self.assertEqual((self.registry / identifier / "turn-0002.txt").read_text(), "steered: two")

    def test_unsupported_engine_rejects_launch_opt_in(self):
        result = self.run_cli("start", "--engine", "muse", "--enable-steering", "--workspace", str(self.root), "--prompt-file", str(self.prompt))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no", result.stderr.lower())

    def test_tool_completion_clears_phase_from_real_normalized_pi_event(self):
        state = dict(steps=0, task_failures=0)
        engine = PiEngine()
        for native in (dict(type="tool_execution_start", toolName="bash"), dict(type="tool_execution_end", toolName="bash", isError=False)):
            for event in engine.normalize_event(native):
                cli.event_update(state, event)
        self.assertEqual(state["phase"], "waiting for model")

    def test_overlapping_tools_keep_remaining_tool_active(self):
        state = dict(steps=0, task_failures=0)
        engine = PiEngine()
        for native in (dict(type="tool_execution_start", toolName="build", toolCallId="A"),
                       dict(type="tool_execution_start", toolName="lint", toolCallId="B"),
                       dict(type="tool_execution_end", toolName="build", toolCallId="A", isError=False)):
            for event in engine.normalize_event(native):
                cli.event_update(state, event)
        self.assertEqual(state["phase"], "tool: lint")
        self.assertEqual(state["active_tools"], {"B": "lint"})
        for event in engine.normalize_event(dict(type="tool_execution_end", toolName="lint", toolCallId="B", isError=False)):
            cli.event_update(state, event)
        self.assertEqual(state["phase"], "waiting for model")

    def test_muse_model_completion_does_not_finish_running_tool(self):
        from claivelib.engines.muse import MuseEngine
        state = dict(steps=0, task_failures=0)
        engine = MuseEngine()
        for kind, detail in (("task.lifecycle.proposed", dict(task_kind="tool.bash", task_id="A")),
                             ("task.lifecycle.proposed", dict(task_kind="model.meta.response", task_id="M")),
                             ("task.lifecycle.completed", dict(task_id="M")),
                             ("task.lifecycle.completed", dict(task_id="A"))):
            for event in engine.normalize_event(dict(payload_type=kind, payload=dict(event=detail))):
                cli.event_update(state, event)
            if detail.get("task_id") == "M":
                self.assertEqual(state["active_tools"], {"A": "tool.bash"})
        self.assertEqual(state["active_tools"], {})

    def test_claude_multi_tool_message_preserves_all_tool_ids(self):
        from claivelib.engines.claude import ClaudeEngine
        engine = ClaudeEngine()
        state = dict(steps=0, task_failures=0)
        for event in engine.normalize_event(dict(type="assistant", message=dict(content=[
                dict(type="tool_use", name="Read", id="A"), dict(type="tool_use", name="Bash", id="B")]))):
            cli.event_update(state, event)
        for event in engine.normalize_event(dict(type="user", message=dict(content=[dict(type="tool_result", tool_use_id="A", content="ok")]))):
            cli.event_update(state, event)
        self.assertEqual(state["phase"], "tool: Bash")
