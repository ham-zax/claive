"""Codex forks through its installed App Server thread/fork protocol."""
import hermetic  # noqa: F401  (must run before claivelib reads the environment)
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
CODEX_APP_SERVER = REPO / "bin/claivelib/engines/codex_appserver.py"


class CodexForkAppServerChecks(unittest.TestCase):
    def test_thread_fork_binds_a_new_thread_before_sending_the_prompt(self):
        with tempfile.TemporaryDirectory(prefix="codex-fork-appserver-") as temporary:
            root = Path(temporary)
            prompt = root / "prompt.md"
            prompt.write_text("branch prompt\n")
            capture = root / "requests.jsonl"
            binary = root / "fake-codex"
            binary.write_text("""#!/usr/bin/python3
import json, os, sys
capture = os.environ["CODEX_FAKE_CAPTURE"]
def output(value):
    print(json.dumps(value), flush=True)
for line in sys.stdin:
    message = json.loads(line)
    with open(capture, "a") as stream:
        stream.write(json.dumps(message) + "\\n")
    identifier = message.get("id")
    if identifier == "initialize":
        output({"id": identifier, "result": {}})
    elif identifier == "thread":
        output({"id": identifier, "result": {"thread": {"id": "forked-thread"}}})
    elif identifier == "turn":
        output({"id": identifier, "result": {"turn": {"id": "fork-turn"}}})
        output({"method": "turn/started", "params": {"threadId": "forked-thread", "turn": {"id": "fork-turn"}}})
        output({"method": "item/completed", "params": {"threadId": "forked-thread", "item": {"type": "agentMessage", "text": "copied branch"}}})
        output({"method": "turn/completed", "params": {"threadId": "forked-thread", "turn": {"id": "fork-turn", "status": "completed"}}})
""")
            binary.chmod(0o700)
            env = dict(os.environ, CODEX_HOME=str(root / "codex-home"), CODEX_FAKE_CAPTURE=str(capture))
            result = subprocess.run(
                [sys.executable, str(CODEX_APP_SERVER), "--binary", str(binary), "--cwd", str(root),
                 "--model", "gpt-6-luna", "--effort", "max", "--sandbox", "read-only",
                 "--fork", "source-thread", "--initial-prompt-file", str(prompt)],
                input="", env=env, text=True, capture_output=True, timeout=10,
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            events = [json.loads(line) for line in result.stdout.splitlines()]
            self.assertIn({"type": "thread.started", "thread_id": "forked-thread"}, events)
            self.assertIn({"type": "turn.completed"}, events)
            self.assertIn({"type": "item.completed", "item": {"type": "agent_message",
                                                                   "text": "copied branch"}}, events)
            requests = [json.loads(line) for line in capture.read_text().splitlines()]
            fork_request = next(request for request in requests if request.get("method") == "thread/fork")
            self.assertEqual(fork_request["params"]["threadId"], "source-thread")
            turn_request = next(request for request in requests if request.get("method") == "turn/start")
            self.assertEqual(turn_request["params"]["threadId"], "forked-thread")
            self.assertEqual(turn_request["params"]["input"], [{"type": "text", "text": "branch prompt\n"}])


if __name__ == "__main__":
    unittest.main()
