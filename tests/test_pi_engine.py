import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
import unittest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "bin"))
from codex_workers import cli
from codex_workers.engines.pi import PiEngine


class PiChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pi-worker-check-")
        self.root = Path(self.temp.name)
        self.registry = self.root / "registry"
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.prompt = self.root / "task;$(touch INJECTED).md"
        self.prompt.write_text("first prompt\n")
        self.agent_dir = self.root / "pi-config"
        self.agent_dir.mkdir()
        self.settings = self.agent_dir / "settings.json"
        self.settings.write_text(json.dumps({"defaultProvider": "opencode2api", "theme": "custom",
                                            "defaultThinkingLevel": "max"}))
        self.env = dict(os.environ, CODEX_WORKERS_DIR=str(self.registry),
                        PI_CODING_AGENT_DIR=str(self.agent_dir),
                        PI_WORKER_BINARY=str(REPO / "tests/fake_pi.py"))
        self.processes = []

    def tearDown(self):
        for path in self.registry.glob("*/state.json"):
            state = json.loads(path.read_text())
            for field in ("worker", "supervisor"):
                pid = state.get(field + "_pid")
                if pid and cli.identity(pid) == state.get(field + "_identity"):
                    try:
                        os.killpg(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
        for process in self.processes:
            process.communicate(timeout=5)
        self.temp.cleanup()

    def command(self, *args, mode="success"):
        return subprocess.run([str(REPO / "bin/codex-workers"), *args],
                              env=dict(self.env, PI_TEST_MODE=mode), text=True,
                              capture_output=True, timeout=15)

    def launch(self, *extra, mode="success"):
        result = self.command("run", "--engine", "pi", "--workspace", str(self.workspace),
                              "--prompt-file", str(self.prompt), *extra, mode=mode)
        match = re.search(r"Worker ([0-9a-f]{12})", result.stdout)
        self.assertIsNotNone(match, result.stderr)
        state = self.state(match.group(1))
        return result, state

    def state(self, job):
        return json.loads((self.registry / job / "state.json").read_text())

    def idle(self, job, turn=1):
        for _ in range(300):
            state = self.state(job)
            if state["status"] == "idle" and state["turn"] == turn:
                return state
            time.sleep(0.025)
        self.fail("Pi worker did not become idle")

    def open(self):
        process = subprocess.Popen([
            str(REPO / "bin/codex-workers"), "open", "--engine", "pi",
            "--workspace", str(self.workspace), "--prompt-file", str(self.prompt),
        ], env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True)
        self.processes.append(process)
        for _ in range(300):
            paths = list(self.registry.glob("*/state.json"))
            if paths:
                return process, self.idle(paths[0].parent.name)
            if process.poll() is not None:
                self.fail(str(process.communicate()))
            time.sleep(0.025)
        self.fail("Pi worker did not create state")

    def test_captured_success_tool_failure_and_cancel_events(self):
        for name in ("success", "resume", "tool", "failure", "cancel"):
            with self.subTest(name=name):
                engine = PiEngine()
                normalized = []
                for line in (REPO / f"tests/fixtures/pi/{name}.jsonl").read_text().splitlines():
                    normalized.extend(engine.normalize_event(json.loads(line)))
                terminals = [event for event in normalized if event["type"].startswith("terminal_")]
                if name == "cancel":
                    self.assertEqual(terminals, [])
                else:
                    self.assertEqual(len(terminals), 1)
                    self.assertEqual(terminals[0]["terminal"], "failed" if name == "failure" else "completed")
                if name == "tool":
                    self.assertTrue(any(event["type"] == "tool_started" for event in normalized))

    def test_retry_does_not_complete_until_settled_and_parser_resets(self):
        engine = PiEngine()
        engine.normalize_event({"type": "agent_start"})
        engine.normalize_event({"type": "message_end", "message": {
            "role": "assistant", "stopReason": "error", "errorMessage": "transient", "content": []}})
        self.assertEqual(engine.normalize_event({"type": "agent_end", "willRetry": True})[0]["type"], "activity")
        engine.normalize_event({"type": "agent_start"})
        engine.normalize_event({"type": "message_end", "message": {
            "role": "assistant", "stopReason": "stop", "content": [{"type": "text", "text": "recovered"}]}})
        self.assertEqual(engine.normalize_event({"type": "agent_settled"})[0]["text"], "recovered")
        engine.normalize_event({"type": "session"})
        self.assertEqual(engine.normalize_event({"type": "agent_settled"})[0]["terminal"], "failed")
        for stop in ("length", "aborted", "toolUse", "deferred", "pending"):
            engine.normalize_event({"type": "message_end", "message": {
                "role": "assistant", "stopReason": stop, "content": []}})
            self.assertEqual(engine.normalize_event({"type": "agent_settled"})[0]["terminal"], "failed")
        self.assertEqual(engine.normalize_event({"type": "tool_execution_end", "isError": True})[0]["type"], "task_warning")

    def test_prompt_tool_policy_and_non_uuid_session_id(self):
        result, state = self.launch("--read-only", "--session-id", "pi-session.unit_7", "--reasoning-effort", "low")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(state["max_model_steps"])
        self.assertEqual(state["launch"]["provider"], "opencode2api")
        self.assertEqual(state["launch"]["model"], "muse-spark-1.3-contributor-free")
        self.assertEqual(state["session_id"], "pi-session.unit_7")
        received = json.loads((self.registry / state["id"] / "result.txt").read_text())
        self.assertEqual(received["prompt"], self.prompt.read_text())
        self.assertEqual(received["tools"], "read,grep,find,ls")
        self.assertEqual(received["effort"], "low")
        self.assertIn("--no-extensions", state["command"])
        self.assertFalse((self.workspace / "INJECTED").exists())

    def test_shared_model_selection_across_agents_and_registries(self):
        process, original = self.open()
        result, selected = self.launch("--model", "big-pickle")
        self.assertEqual(result.returncode, 0, result.stderr)
        settings = json.loads(self.settings.read_text())
        self.assertEqual(settings["defaultModel"], "big-pickle")
        self.assertEqual(settings["theme"], "custom")
        self.assertEqual(selected["reasoning_effort"], "max")
        self.assertEqual(settings["defaultThinkingLevel"], "max")
        self.assertEqual(self.command("followup", original["id"], "--prompt-file", str(self.prompt)).returncode, 0)
        resumed = self.idle(original["id"], turn=2)
        self.assertEqual(resumed["launch"]["model"], "muse-spark-1.3-contributor-free")
        self.assertEqual(json.loads(self.settings.read_text())["defaultModel"], "big-pickle")
        self.assertEqual(self.command("close", original["id"]).returncode, 0)
        process.communicate(timeout=5)
        self.env["CODEX_WORKERS_DIR"] = str(self.root / "other-registry")
        self.registry = self.root / "other-registry"
        result, inherited = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(inherited["launch"]["model"], selected["launch"]["model"])
        rejected = self.command("run", "--engine", "pi", "--workspace", str(self.workspace),
                                "--prompt-file", str(self.prompt), "--model", "rejected-model", "--max-model-steps", "1")
        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(json.loads(self.settings.read_text())["defaultModel"], "big-pickle")
        result, override = self.launch("--model", "muse-spark-1.3-contributor-free")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(override["launch"]["model"], "muse-spark-1.3-contributor-free")
        result, later = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(later["launch"]["model"], override["launch"]["model"])

    def test_failure_with_zero_exit_and_transport_validation(self):
        for mode in ("failure", "missing", "malformed", "exit-failure"):
            with self.subTest(mode=mode):
                result, state = self.launch(mode=mode)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(state["status"], "failed")
                if mode == "failure":
                    self.assertEqual(state["exit_code"], 0)
                    self.assertIn("PI_CHARACTERIZATION_FAILURE", state["terminal_reason"])

    def test_resume_followup_reopen_usage_and_rejection_keep_worker_alive(self):
        process, state = self.open()
        job = state["id"]
        self.assertEqual(self.command("wait", job).returncode, 0)
        before_policy = (self.registry / job / "policy.json").read_bytes()
        for action in ("followup", "effort"):
            args = [action, job, "--max-model-steps", "100"]
            args += ["--prompt-file", str(self.prompt)] if action == "followup" else ["--reasoning-effort", "high"]
            rejected = self.command(*args)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("does not support", rejected.stderr)
        self.assertFalse(list((self.registry / job / "requests").glob("*.json")))
        self.assertEqual((self.registry / job / "policy.json").read_bytes(), before_policy)
        self.assertIsNone(process.poll())
        self.prompt.write_text("second prompt with different content\n")
        self.assertEqual(self.command("followup", job, "--prompt-file", str(self.prompt),
                                      "--reasoning-effort", "minimal").returncode, 0)
        second = self.idle(job, 2)
        self.assertIn("--session", second["command"])
        self.assertNotIn("--session-id", second["command"])
        received = json.loads((self.registry / job / "result.txt").read_text())
        self.assertTrue(received["resumed"])
        self.assertEqual(received["prompt"], self.prompt.read_text())
        self.assertEqual(received["effort"], "minimal")
        # A retained-session setup error must reject just that request.
        session_file = Path(second["command"][second["command"].index("--session") + 1])
        duplicate = session_file.with_name("duplicate.jsonl")
        duplicate.write_bytes(session_file.read_bytes())
        self.assertEqual(self.command("followup", job, "--prompt-file", str(self.prompt)).returncode, 0)
        for _ in range(200):
            if not list((self.registry / job / "requests").glob("*.json")):
                break
            time.sleep(0.025)
        self.assertIsNone(process.poll())
        self.assertEqual(self.state(job)["status"], "idle")
        self.assertIn("multiple Pi session files", self.state(job)["error"])
        duplicate.unlink()
        usage = self.command("usage", job, "--json")
        self.assertEqual(usage.returncode, 0, usage.stderr)
        self.assertEqual(json.loads(usage.stdout)["model_calls"], 2)
        self.command("close", job)
        process.communicate(timeout=5)
        result, reopened = self.launch("--session-id", state["session_id"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(reopened["launch"]["session_dir"], state["launch"]["session_dir"])
        self.assertTrue(json.loads((self.registry / reopened["id"] / "result.txt").read_text())["resumed"])

    def test_unsupported_initial_options_do_not_launch(self):
        for options in (("--max-model-steps", "100"), ("--output-schema", str(self.prompt)),
                        ("--provider", "openai"), ("--provider", "meta"),
                        ("--web",), ("--worktree",), ("--worktree-existing", str(self.workspace)),
                        ("--reasoning-effort", "invalid"), ("--session-id", "../escape")):
            with self.subTest(options=options):
                result = self.command("run", "--engine", "pi", "--workspace", str(self.workspace),
                                      "--prompt-file", str(self.prompt), *options)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("Worker ", result.stdout)
        self.assertFalse(list(self.registry.glob("*/state.json")))
        result, state = self.launch("--no-session-log")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--no-session", state["command"])
        self.assertIsNone(state["session_id"])
        self.assertNotEqual(self.command("usage", state["id"]).returncode, 0)

    def test_session_workspace_and_usage_unknowns(self):
        result, state = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        usage = PiEngine().session_usage(state)
        self.assertEqual(usage["totals"]["input_tokens"], 1139)
        self.assertEqual(usage["totals"]["cached_tokens"], 70)
        self.assertAlmostEqual(usage["cache_hit_ratio"], 70 / 1139)
        call = PiEngine._usage_call({"usage": {"input": 10}})
        self.assertNotIn("cached_tokens", call)
        self.assertNotIn("input_tokens", call)
        other = dict(state, workspace=str(self.root))
        with self.assertRaisesRegex(ValueError, "different workspace"):
            PiEngine().session_usage(other)

    def test_detached_cancel_stops_process_group(self):
        result = self.command("start", "--engine", "pi", "--workspace", str(self.workspace),
                              "--prompt-file", str(self.prompt), mode="slow")
        self.assertEqual(result.returncode, 0, result.stderr)
        job = re.search(r"Worker ([0-9a-f]{12})", result.stdout).group(1)
        log = self.registry / job / "events.jsonl"
        child = None
        for _ in range(300):
            if log.exists():
                for line in log.read_text().splitlines():
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if record.get("type") == "probe_child":
                        child = record["pid"]
            if child:
                break
            time.sleep(0.025)
        self.assertIsNotNone(child)
        state = self.state(job)
        self.assertEqual(self.command("cancel", job).returncode, 0)
        self.assertEqual(self.command("wait", job).returncode, 130)
        self.assertIsNone(cli.identity(state["worker_pid"]))
        self.assertIsNone(cli.identity(child))


if __name__ == "__main__":
    unittest.main(verbosity=2)
