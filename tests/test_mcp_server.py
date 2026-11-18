"""Flat-schema MCP server and zero-shell status.json tests."""
import hermetic  # noqa: F401
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "bin"))
from claivelib import mcp_server

CLI_MCP = str(REPO / "bin/claive-mcp")


class McpServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="claive-mcp-test-")
        self.base = Path(self.temp.name)
        self.registry = self.base / "registry"
        self.workspace = self.base / "repo"
        self.workspace.mkdir()
        subprocess.run(["git", "init", "-q", str(self.workspace)], check=True)
        (self.workspace / "app.py").write_text("x = 1\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.workspace), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(self.workspace), "-c", "user.name=Test", "-c", "user.email=t@e.invalid",
             "commit", "-qm", "init"],
            check=True,
        )
        self.worker_bin = self.base / "fake_claude.py"
        self.worker_bin.write_text(
            "#!/usr/bin/python3\n"
            "import json\n"
            "from pathlib import Path\n"
            "Path('app.py').write_text('x = 2\\n')\n"
            "report = {'status': 'done', 'summary': 'updated app.py', 'changed_files': ['app.py'], 'verification': [], 'risks': []}\n"
            "ans = 'Done.\\n```claive-report\\n' + json.dumps(report) + '\\n```'\n"
            "print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 's1'}), flush=True)\n"
            "print(json.dumps({'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'id': 'u1', 'name': 'Edit', 'input': {'file_path': str(Path('app.py').resolve())}}]}}), flush=True)\n"
            "print(json.dumps({'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'u1', 'content': 'ok'}]}}), flush=True)\n"
            "print(json.dumps({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': ans}]}}), flush=True)\n"
            "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': ans}), flush=True)\n",
            encoding="utf-8",
        )
        self.worker_bin.chmod(0o755)
        self.env = dict(
            os.environ,
            CLAIVE_DIR=str(self.registry),
            CLAUDE_WORKER_BINARY=str(self.worker_bin),
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_flat_schemas_have_zero_combinators_or_constraints(self):
        names = mcp_server.validate_flat_schemas()
        self.assertEqual(
            names,
            ["claive_start", "claive_peek", "claive_steer", "claive_wait",
             "claive_cancel", "claive_verify", "claive_list",
             "claive_followup", "claive_apply", "claive_close"],
        )
        serialized = json.dumps(mcp_server.TOOL_DEFINITIONS)
        for forbidden in mcp_server.FORBIDDEN_SCHEMA_KEYS:
            self.assertNotIn(f'"{forbidden}"', serialized)
        with self.assertRaisesRegex(ValueError, "forbidden JSON Schema keywords"):
            mcp_server._validate_schema_node({"anyOf": [{"type": "string"}]}, "test")
        with self.assertRaisesRegex(ValueError, "scalar string"):
            mcp_server._validate_schema_node({"type": ["string", "null"]}, "test")

    def test_nested_worker_guard(self):
        blocked = subprocess.run(
            [CLI_MCP],
            env=dict(self.env, CLAIVE_WORKER_ID="abcdef012345"),
            input="",
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(blocked.returncode, 1)
        self.assertIn("not permitted inside a claive worker turn", blocked.stderr)

        allowed_check = subprocess.run(
            [CLI_MCP, "--check", "--json"],
            env=dict(self.env, CLAIVE_WORKER_ID="abcdef012345"),
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(allowed_check.returncode, 0, allowed_check.stderr)
        self.assertTrue(json.loads(allowed_check.stdout)["flat_schemas"])

    def test_stdio_ndjson_and_lsp_end_to_end_with_status_json(self):
        proc = subprocess.Popen(
            [CLI_MCP],
            env=self.env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            def send_ndjson(payload):
                proc.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
                proc.stdin.flush()
                line = proc.stdout.readline()
                self.assertTrue(line)
                return json.loads(line.decode("utf-8"))

            def send_lsp(payload):
                raw = json.dumps(payload).encode("utf-8")
                proc.stdin.write(f"Content-Length: {len(raw)}\r\n\r\n".encode("ascii") + raw)
                proc.stdin.flush()
                header = proc.stdout.readline().decode("ascii")
                self.assertTrue(header.lower().startswith("content-length:"))
                length = int(header.split(":", 1)[1].strip())
                blank = proc.stdout.readline()
                self.assertEqual(blank, b"\r\n")
                body = proc.stdout.read(length)
                return json.loads(body.decode("utf-8"))

            init_resp = send_ndjson({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
            self.assertEqual(init_resp["result"]["serverInfo"]["name"], "claive-mcp")
            proc.stdin.write((json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n").encode("utf-8"))
            proc.stdin.flush()

            tools_resp = send_lsp({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            self.assertEqual(len(tools_resp["result"]["tools"]), 10)

            start_resp = send_ndjson({
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "claive_start",
                    "arguments": {
                        "workspace": str(self.workspace),
                        "prompt": "Update app.py",
                        "engine": "claude",
                        "alias": "mcp-worker",
                        "report": True,
                        "enable_steering": False,
                        "check": f"unit={sys.executable} -c 'import pathlib; assert \"x = 2\" in pathlib.Path(\"app.py\").read_text()'",
                        "verify_in_worktree": True,
                        "fit_checked": True,
                    },
                },
            })
            self.assertFalse(start_resp["result"]["isError"], start_resp)
            started_data = json.loads(start_resp["result"]["content"][0]["text"])
            worker_id = started_data["worker"]["id"]
            self.assertEqual(started_data["worker"]["alias"], "mcp-worker")

            wait_resp = send_ndjson({
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "claive_wait", "arguments": {"id": "@mcp-worker", "timeout": 10}},
            })
            self.assertFalse(wait_resp["result"]["isError"], wait_resp)
            waited_data = json.loads(wait_resp["result"]["content"][0]["text"])
            self.assertEqual(waited_data["worker_status"]["status"], "completed")

            status_path = self.registry / worker_id / "status.json"
            self.assertTrue(status_path.is_file())
            disk_status = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(disk_status["id"], worker_id)
            self.assertEqual(disk_status["alias"], "mcp-worker")
            self.assertEqual(disk_status["status"], "completed")
            self.assertIn("app.py", disk_status["changed_files"])
            self.assertEqual(disk_status["report"]["summary"], "updated app.py")

            peek_resp = send_ndjson({
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "claive_peek", "arguments": {"id": worker_id, "steps": 5}},
            })
            self.assertFalse(peek_resp["result"]["isError"])
            peek_data = json.loads(peek_resp["result"]["content"][0]["text"])
            self.assertGreaterEqual(peek_data["total_steps"], 1)

            verify_resp = send_lsp({
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {"name": "claive_verify", "arguments": {"id": worker_id}},
            })
            self.assertFalse(verify_resp["result"]["isError"], verify_resp)
            verify_data = json.loads(verify_resp["result"]["content"][0]["text"])
            self.assertEqual(verify_data["status"], "passed")
            disk_after_verify = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(disk_after_verify["verification_status"], "passed")

            list_resp = send_ndjson({
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {"name": "claive_list", "arguments": {"limit": 5}},
            })
            self.assertFalse(list_resp["result"]["isError"])
            listed = json.loads(list_resp["result"]["content"][0]["text"])
            self.assertEqual(listed["workers"][0]["id"], worker_id)
        finally:
            proc.stdin.close()
            proc.wait(timeout=5)
            proc.stdout.close()
            proc.stderr.close()


if __name__ == "__main__":
    unittest.main()
