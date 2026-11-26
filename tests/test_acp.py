"""Exercise the Local ACP exchange through Claive and a fixture Codex CLI."""
import hermetic  # noqa: F401
import json
import os
from pathlib import Path
from queue import Queue
import subprocess
import tempfile
import threading
import time
import unittest


ADAPTER = Path(__file__).resolve().parents[1] / "bin" / "claive-acp"
MODEL = "codex/gpt-6.2-sol"
CODEX = """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys
import time
from uuid import uuid4

args = sys.argv[1:]
resumed = args[:2] == ["exec", "resume"]
identifier = args[-3] if resumed else str(uuid4())
sessions = Path(os.environ["CODEX_HOME"]) / "sessions"
sessions.mkdir(parents=True, exist_ok=True)
rollout = sessions / f"rollout-fixture-{identifier}.jsonl"
previous = rollout.read_text() if rollout.exists() else ""
rollout.write_text(previous + args[-1] + "\\n")
print(json.dumps({"type": "thread.started", "thread_id": identifier}), flush=True)
print(json.dumps({"type": "turn.started"}), flush=True)
if args[-1] == "slow":
    Path("worker-running").touch()
    time.sleep(60)
if args[-1] == "first" and 'sandbox_mode="read-only"' not in args:
    Path("worker-wrote").write_text("ok")
answer = "What should I name it?" if args[-1] == "first" else (
    "remembered the question" if resumed and "first" in previous else "no earlier context")
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": answer}}), flush=True)
print(json.dumps({"type": "turn.completed"}), flush=True)
"""


class ACPChecks(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix="claive-acp-test-")
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.work = self.root / "work"
        self.work.mkdir()
        self.registry = self.root / "registry"
        binary = self.root / "codex"
        binary.write_text(CODEX)
        binary.chmod(0o700)
        config = self.root / "config.json"
        config.write_text(json.dumps({"default_engine": "codex", "engines": {"codex": {
            "binary": str(binary), "service_tier": "fast",
            "extra_models": {"gpt-6.2-sol": {"efforts": ["low", "medium", "high", "xhigh"]}},
            "disabled_model_efforts": {"gpt-6.2-sol": ["high"]},
        }}}))
        env = dict(os.environ, CLAIVE_CONFIG=str(config), CLAIVE_DIR=str(self.registry),
                   CODEX_HOME=str(self.root / "codex-home"), CLAIVE_CODEX_YOLO="0")
        self.process = subprocess.Popen([str(ADAPTER), "--engine", "codex"], env=env, text=True,
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(self.stop)
        self.messages = Queue()
        threading.Thread(target=self.read_messages, daemon=True).start()
        self.serial = 0
        self.call("initialize", protocolVersion=1)

    def read_messages(self):
        for line in self.process.stdout:
            self.messages.put(json.loads(line))

    def stop(self):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        finally:
            self.process.stdout.close()
            self.process.stderr.close()

    def send(self, **message):
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
        self.process.stdin.flush()

    def receive(self, matches):
        until = time.monotonic() + 10
        while True:
            message = self.messages.get(timeout=max(0, until - time.monotonic()))
            if matches(message):
                return message

    def request(self, method, **params):
        self.serial += 1
        self.send(id=self.serial, method=method, params=params)
        return self.serial

    def response(self, request):
        return self.receive(lambda msg: msg.get("id") == request)

    def call(self, method, **params):
        result = self.response(self.request(method, **params))
        self.assertIn("result", result)
        return result["result"]

    def configure(self, session, key, value):
        return self.call("session/set_config_option", sessionId=session, configId=key, value=value)

    def prompt(self, session, text, permit=True):
        request = self.request("session/prompt", sessionId=session, prompt=[{"type": "text", "text": text}])
        permission = self.receive(lambda msg: msg.get("method") == "session/request_permission")
        if permit:
            self.send(id=permission["id"], result={"outcome": {"outcome": "selected", "optionId": "allow"}})
        return request

    def workers(self):
        return [json.loads(path.read_text()) for path in self.registry.glob("*/state.json")]

    def test_t3_selects_writable_low_and_reuses_the_worker_for_clarification(self):
        setup = self.call("session/new", cwd=str(self.work), mcpServers=[])
        session = setup["sessionId"]
        available = next(model for model in setup["models"]["availableModels"] if model["modelId"] == MODEL)
        self.assertEqual([row["value"] for row in available["_meta"]["reasoningEfforts"]], ["low", "medium", "xhigh"])
        self.configure(session, "model", MODEL)
        options = self.configure(session, "mode", "code")["configOptions"]
        effort = next(option for option in options if option["category"] == "thought_level")
        invalid = self.response(self.request("session/set_config_option", sessionId=session,
                                             configId=effort["id"], value="high"))
        self.assertEqual(invalid["error"]["code"], -32602)
        self.configure(session, effort["id"], "low")
        request = self.prompt(session, "first")
        question = self.receive(lambda msg: msg.get("params", {}).get("update", {}).get("sessionUpdate")
                                == "agent_message_chunk")
        self.assertIn("What should I name it?", question["params"]["update"]["content"]["text"])
        self.assertEqual(self.response(request)["result"]["stopReason"], "end_turn")
        self.assertEqual((self.work / "worker-wrote").read_text(), "ok")
        first, = self.workers()
        self.assertEqual((first["model"], first["reasoning_effort"], first["read_mode"]),
                         ("gpt-6.2-sol", "low", "write"))
        self.assertIn('service_tier="fast"', first["command"])

        self.configure(session, effort["id"], "medium")
        request = self.prompt(session, "Call it beam")
        reply = self.receive(lambda msg: msg.get("params", {}).get("update", {}).get("sessionUpdate")
                             == "agent_message_chunk")
        self.assertEqual(reply["params"]["update"]["content"]["text"], "remembered the question")
        self.assertEqual(self.response(request)["result"]["stopReason"], "end_turn")
        second, = self.workers()
        self.assertEqual((second["id"], second["session_id"], second["turn"]),
                         (first["id"], first["session_id"], 2))
        self.assertEqual(second["reasoning_effort"], "medium")
        self.call("session/close", sessionId=session)
        self.assertEqual(self.workers()[0]["status"], "completed")

    def test_cancel_before_approval_and_during_native_turn(self):
        session = self.call("session/new", cwd=str(self.work), mcpServers=[])["sessionId"]
        request = self.prompt(session, "first", permit=False)
        self.send(method="session/cancel", params={"sessionId": session})
        self.assertEqual(self.response(request)["result"]["stopReason"], "cancelled")
        self.assertEqual(self.workers(), [])
        request = self.prompt(session, "slow")
        until = time.monotonic() + 8
        while not (self.work / "worker-running").exists():
            self.assertLess(time.monotonic(), until)
            time.sleep(0.03)
        self.send(method="session/cancel", params={"sessionId": session})
        self.assertEqual(self.response(request)["result"]["stopReason"], "cancelled")
        state, = self.workers()
        self.assertEqual((state["status"], state["read_mode"]), ("cancelled", "enforced"))
