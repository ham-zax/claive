"""Supervisor regressions using local streams, without model/API calls."""
import hermetic  # noqa: F401
import json
import fcntl
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from test_workers import FIXTURE_CLI, FIXTURE_WORKER, module as cli
from claivelib import runtime_limits, idle_sessions, acceptance, turns


class WorkerReliabilityChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="claive-reliability-")
        self.path = Path(self.temp.name)
        self.registry = self.path / "state"
        self.workspace = self.path / "repo"
        self.workspace.mkdir()
        self.prompt = self.path / "prompt.md"
        self.prompt.write_text("Do a fixture task.")
        self.env = dict(os.environ, CLAIVE_DIR=str(self.registry), FIXTURE_WORKER_BINARY=FIXTURE_WORKER)
        self.workers = []

    def tearDown(self):
        for file in self.registry.glob("*/state.json"):
            state = json.loads(file.read_text())
            for field in ("worker", "supervisor"):
                pid = state.get(field + "_pid")
                if pid and cli.identity(pid) == state.get(field + "_identity"):
                    try:
                        os.killpg(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
        self.temp.cleanup()

    def run_cli(self, *args, **env):
        return subprocess.run([FIXTURE_CLI, *args], env=dict(self.env, **env),
                              text=True, capture_output=True, timeout=12)

    def launch(self, *args, action="run", **env):
        result = self.run_cli(action, "--engine", "fixture", "--workspace", str(self.workspace),
                              "--prompt-file", str(self.prompt), *args, **env)
        files = list(self.registry.glob("*/state.json"))
        self.assertTrue(files, result.stderr)
        return result, files[0].parent

    def state(self, path):
        return json.loads((path / "state.json").read_text())

    def until(self, path, predicate):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            state = self.state(path)
            if predicate(state):
                return state
            time.sleep(.025)
        self.fail(str(self.state(path)))

    def custom_worker(self, body):
        path = self.path / "custom-worker"
        path.write_text("#!/usr/bin/python3\nimport json,time,os\nfrom pathlib import Path\n" + body)
        path.chmod(0o700)
        return str(path)

    def git_repo(self):
        for args in (["init", "-q"], ["config", "user.email", "test@example.invalid"],
                     ["config", "user.name", "Test"]):
            subprocess.run(["git", "-C", str(self.workspace), *args], check=True)
        (self.workspace / "tracked").write_text("before\n")
        subprocess.run(["git", "-C", str(self.workspace), "add", "tracked"], check=True)
        subprocess.run(["git", "-C", str(self.workspace), "commit", "-qm", "seed"], check=True)

    def test_watchdog_separates_stalls_wrapup_and_hard_budget(self):
        action = runtime_limits.turn_action
        self.assertEqual(action(90, 1, {}, 100, 20), "wrap_up")
        self.assertIsNone(action(90, 1, {}, 100, 20, wrapped=True))
        self.assertEqual(action(10, 8, {}, None, 5), "stall_timeout")
        self.assertIsNone(action(10, 8, {"tool": "bash"}, None, 5))
        self.assertEqual(action(100, 0, {"tool": "bash"}, 100, 5), "timeout")
        self.assertIsNone(action(100, 0, {}, 100, 5, grace=10, wrapped=True))
        for value in ("nan", "inf", -1, True):
            with self.assertRaises(ValueError):
                runtime_limits.seconds(value, "limit")

    def test_quiet_stream_stalls_but_progressing_stream_finishes(self):
        script = self.custom_worker('print(json.dumps({"kind":"model"}),flush=True)\ntime.sleep(3)\n')
        result, path = self.launch("--stall-timeout", ".15", FIXTURE_WORKER_BINARY=script)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.state(path)["failure_kind"], "stall_timeout")
        # Reuse the registry only after recording the first result.
        script = self.custom_worker('for _ in range(8):\n print(json.dumps({"kind":"activity"}),flush=True)\n time.sleep(.08)\nprint(json.dumps({"kind":"terminal","status":"completed","text":"done"}),flush=True)\n')
        result = self.run_cli("run", "--engine", "fixture", "--workspace", str(self.workspace),
                              "--prompt-file", str(self.prompt), "--stall-timeout", ".25",
                              FIXTURE_WORKER_BINARY=script)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_timeout_harvests_tracked_and_untracked_work(self):
        self.git_repo()
        script = self.custom_worker('Path("tracked").write_text("after\\nextra\\n")\nPath("new-file").write_text("new\\n")\nprint(json.dumps({"kind":"model"}),flush=True)\ntime.sleep(3)\n')
        result, path = self.launch("--turn-timeout", ".3", "--report", FIXTURE_WORKER_BINARY=script)
        state = self.state(path)
        self.assertNotEqual(result.returncode, 0)
        delta = state["workspace_delta"]
        self.assertEqual((delta["files"], delta["untracked"], delta["added"], delta["deleted"]), (2, 1, 2, 1))
        self.assertIn("new-file", delta["status_short"])
        self.assertEqual(state["report_state"], "missing")
        self.assertIn("Workspace changes:", self.run_cli("status", path.name).stdout)
        self.assertIn("workspace +2/-1", self.run_cli("list").stdout)
        self.assertEqual(acceptance.workspace_delta(self.path)["scope"], "workspace")

    def test_idle_ttl_send_resumes_without_replaying_previous_turn(self):
        result, path = self.launch("--detach", "--idle-ttl", ".25", action="open")
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.until(path, lambda s: s.get("phase") == idle_sessions.PHASE)
        session = state["session_id"]
        sent = self.run_cli("send", path.name, "--mode", "wake", "--message", "second task")
        self.assertEqual(sent.returncode, 0, sent.stdout + sent.stderr)
        state = self.until(path, lambda s: s["turn"] == 2 and s.get("phase") == idle_sessions.PHASE)
        self.assertEqual(state["session_id"], session)
        log = Path(state["launch"]["session_dir"]) / "session.json"
        self.assertEqual(len(json.loads(log.read_text())["messages"]), 2)

    def test_idle_guards_cover_parent_messages_monitors_and_schedules(self):
        path = self.path / "worker"
        path.mkdir()
        self.assertIsNotNone(idle_sessions.protected(path, {"needs_parent": {"question": "choice"}}))
        for directory, payload in [("requests", {}), ("messages/pending", {}),
                                   ("monitors", {}), ("messages/schedules", {"status": "scheduled"})]:
            file = path / directory / "test.json"
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(json.dumps(payload))
            self.assertIsNotNone(idle_sessions.protected(path, {}), directory)
            file.unlink()
        self.assertIsNone(idle_sessions.protected(path, {}))

    def test_idle_worktrees_are_recreated_when_clean_and_retained_when_dirty(self):
        self.git_repo()
        result, path = self.launch("--detach", "--worktree", "--idle-ttl", ".25", action="open")
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.until(path, lambda s: s.get("phase") == idle_sessions.PHASE)
        target = Path(state["launch"]["isolation"]["existing_path"])
        self.assertFalse(target.exists())
        self.assertEqual(self.run_cli("send", path.name, "--mode", "wake", "--message", "next").returncode, 0)
        self.until(path, lambda s: s["turn"] == 2 and s.get("phase") == idle_sessions.PHASE)
        self.assertFalse(target.exists())
        # A manually closed suspended session must not be restarted by send.
        self.assertEqual(self.run_cli("close", path.name).returncode, 0)
        self.assertNotEqual(self.run_cli("send", path.name, "--message", "closed").returncode, 0)
        script = self.custom_worker('Path("kept").write_text("work\\n")\nprint(json.dumps({"kind":"terminal","status":"completed","text":"done"}),flush=True)\n')
        result = self.run_cli("open", "--detach", "--engine", "fixture", "--workspace", str(self.workspace),
                              "--prompt-file", str(self.prompt), "--worktree", "--idle-ttl", ".25",
                              FIXTURE_WORKER_BINARY=script)
        self.assertEqual(result.returncode, 0, result.stderr)
        second = next(file.parent for file in self.registry.glob("*/state.json") if file.parent != path)
        state = self.until(second, lambda s: s.get("phase") == idle_sessions.PHASE)
        self.assertEqual((Path(state["retained_worktree"]) / "kept").read_text(), "work\n")

    def test_cleanup_dry_run_and_close_preserve_history(self):
        result, path = self.launch("--detach", "--idle-ttl", "0", action="open")
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.until(path, lambda s: s["status"] == "idle")
        state["last_turn_ended_at"] = time.time() - 100
        cli.save(path / "state.json", state)
        dry = self.run_cli("cleanup", "--idle-older-than", "1s", "--dry-run", "--json")
        self.assertEqual(json.loads(dry.stdout)[0]["action"], "would_close")
        self.assertFalse((path / "close.request").exists())
        actual = self.run_cli("prune", "--idle-older-than", "1s", "--json")
        self.assertEqual(json.loads(actual.stdout)[0]["action"], "closing")
        self.until(path, lambda s: s.get("phase") == "session closed")
        self.assertTrue((path / "result.txt").exists())

    def pi_run(self, body, *args):
        header = '''import sys
initial = json.loads(sys.stdin.readline())
def emit(value):
 print(json.dumps(value), flush=True)
def finish():
 emit(dict(type="message_end", message=dict(role="assistant", stopReason="stop", content=[dict(type="text", text="done")])))
 emit(dict(type="agent_settled"))
def tool(index, error=True):
 emit(dict(type="tool_execution_start", toolName="write", toolCallId=str(index), args={}))
 emit(dict(type="tool_execution_end", toolName="write", toolCallId=str(index), isError=error,
           result=dict(content=[dict(type="text", text="hit the output token limit" if error else "ok")])))
'''
        binary = self.custom_worker(header + body)
        result = self.run_cli("run", "--engine", "pi", "--workspace", str(self.workspace),
                              "--prompt-file", str(self.prompt), "--no-session-log", *args,
                              PI_WORKER_BINARY=binary)
        path = next(self.registry.glob("*/state.json")).parent
        return result, self.state(path), path

    def test_default_pi_rpc_receives_one_time_wrapup(self):
        result, state, path = self.pi_run('frame=json.loads(sys.stdin.readline())\nassert "Turn time budget" in frame["message"]\nfinish()\n',
                                          "--turn-timeout", "3")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(state["launch"]["steering"])
        self.assertTrue(state["time_wrap_up_sent"])
        receipts = [json.loads(f.read_text()) for f in (path / "steering" / "receipts").glob("*.json")]
        self.assertEqual([r["source"] for r in receipts], ["time_budget"])

    def test_pi_truncation_guidance_and_consecutive_failure_limit(self):
        result, state, path = self.pi_run('tool(0)\nframe=json.loads(sys.stdin.readline())\nassert "150 lines" in frame["message"]\ntool(1)\ntool(2)\ntime.sleep(4)\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(state["failure_kind"], "output_truncated")
        self.assertEqual(state["consecutive_truncations"], 3)
        self.assertTrue(state["truncation_hint_sent"])

    def test_success_breaks_consecutive_truncation_streak(self):
        result, state, _ = self.pi_run('tool(0)\ntool(1)\ntool(2,False)\ntool(3)\ntool(4)\nfinish()\n')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(state["consecutive_truncations"], 2)

    def test_stall_shutdown_corruption_retains_failure_and_evidence(self):
        corrupt = '''receipts=Path(os.environ["CLAIVE_DIR"])/os.environ["CLAIVE_WORKER_ID"]/"steering"/"receipts"
receipts.mkdir(parents=True,exist_ok=True)
(receipts/("0"*32+".json")).write_text("{")
emit(dict(type="agent_start"))
time.sleep(4)
'''
        result, state, path = self.pi_run(corrupt, "--stall-timeout", ".2")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["failure_kind"], "stall_timeout")
        self.assertIn("stalled", state["error"])
        self.assertIn("invalid steering receipt", state["shutdown_error"])
        self.assertIn("ended_at", state)
        self.assertIn("workspace_delta", state)
        self.assertTrue((path / "result.txt").exists())
        self.assertTrue((path / "evidence" / "turn-0001" / "execution.json").exists())

    def test_truncation_shutdown_corruption_preserves_limit_cause(self):
        body = '''tool(0)
frame=json.loads(sys.stdin.readline())
# Corrupt an unrelated receipt: the active hint receipt is still being marked
# sent, so corrupting it would test a delivery failure before the limit fires.
receipt=Path(os.environ["CLAIVE_DIR"])/os.environ["CLAIVE_WORKER_ID"]/"steering"/"receipts"/("0"*32+".json")
receipt.write_text("{")
tool(1)
tool(2)
time.sleep(4)
'''
        result, state, path = self.pi_run(body)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(state["failure_kind"], "output_truncated")
        self.assertIn("output token limit", state["error"])
        self.assertIn("invalid steering receipt", state["shutdown_error"])
        self.assertIn("ended_at", state)
        self.assertTrue((path / "result.txt").exists())

    def test_nonsteerable_turn_does_not_claim_wrapup_delivery(self):
        script = self.custom_worker('print(json.dumps({"kind":"model"}),flush=True)\ntime.sleep(4)\n')
        _, path = self.launch("--turn-timeout", "3", FIXTURE_WORKER_BINARY=script)
        state = self.state(path)
        self.assertTrue(state["time_wrap_up_attempted"])
        self.assertFalse(state.get("time_wrap_up_sent", False))
        self.assertIn("not delivered", state["time_wrap_up_error"])

    def test_probe_completion_clears_active_tool_without_failure(self):
        state = {"steps": 0}
        cli.event_update(state, {"type": "tool_started", "tool_id": "A", "tool": "bash"})
        cli.event_update(state, {"type": "tool_probe", "tool_id": "A", "command": "rg absent"})
        self.assertEqual(state["active_tools"], {})
        self.assertEqual(state.get("task_failures", 0), 0)
        self.assertEqual(state["turn_command_results"]["rg absent"], "ok")

    def test_uncorrelated_tool_warning_releases_stall_exemption(self):
        state = {"steps": 0}
        cli.event_update(state, {"type": "tool_started", "tool": "bash"})
        cli.event_update(state, {"type": "task_warning", "tool": "bash", "reason": "boom"})
        self.assertEqual(state["active_tools"], {})
        self.assertEqual(runtime_limits.turn_action(1000, 999, state["active_tools"], None, 180),
                         "stall_timeout")

    def test_followup_admission_cannot_race_idle_suspension(self):
        _, path = self.launch()
        state = self.state(path)
        state.update(status="idle", reusable=True, idle_ttl=1, last_turn_status="completed")
        cli.save(path / "state.json", state)
        releasing, proceed, queued = threading.Event(), threading.Event(), threading.Event()
        results = {}

        def release(_state):
            releasing.set()
            self.assertTrue(proceed.wait(3))

        def expire():
            results["expired"] = idle_sessions.expire(path, 2)

        def followup():
            queued.set()
            try:
                turns.queue(path, str(self.prompt))
                results["admitted"] = True
            except ValueError:
                results["admitted"] = False

        with mock.patch.object(cli, "load", side_effect=lambda p, **kw: self.state(p)), \
                mock.patch.object(cli, "release_worktree", side_effect=release):
            closer = threading.Thread(target=expire)
            sender = threading.Thread(target=followup)
            closer.start()
            try:
                self.assertTrue(releasing.wait(3))
                with (path / ".requests.lock").open("w") as admission:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(admission, fcntl.LOCK_EX | fcntl.LOCK_NB)
                sender.start()
                self.assertTrue(queued.wait(3))
            finally:
                proceed.set()
                closer.join(3)
                if sender.ident is not None:
                    sender.join(3)
            self.assertFalse(closer.is_alive())
            self.assertFalse(sender.is_alive())
        self.assertTrue(results["expired"])
        self.assertFalse(results["admitted"])
        self.assertEqual(list((path / "requests").glob("*.json")), [])

    def test_other_tool_failures_do_not_reset_truncation_streak(self):
        ordinary = 'emit(dict(type="tool_execution_end", toolName="edit", toolCallId="other", isError=True, result=dict(content=[dict(type="text",text="old text missing")])))\n'
        result, state, _ = self.pi_run('tool(0)\n' + ordinary + 'tool(1)\n' + ordinary + 'tool(2)\ntime.sleep(4)\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(state["failure_kind"], "output_truncated")
        self.assertEqual(state["consecutive_truncations"], 3)

    def test_list_filters_and_limits_and_status_alias(self):
        result, path = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.run_cli("status", path.name, "--json").stdout,
                         self.run_cli("show", path.name, "--json").stdout)
        self.assertEqual(json.loads(self.run_cli("list", "--active", "--limit", "1", "--json").stdout), [])
        self.assertEqual(len(json.loads(self.run_cli("list", "--limit", "1", "--json").stdout)), 1)
        self.assertNotEqual(self.run_cli("list", "--limit", "0").returncode, 0)


if __name__ == "__main__":
    unittest.main()
