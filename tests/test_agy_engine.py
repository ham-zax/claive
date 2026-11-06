import hermetic  # noqa: F401
import dataclasses
import errno
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
from claivelib import cli, advice
from claivelib.engine import TurnRequest, WorkerEngine
from claivelib.engines import ENGINE_NAMES, get_engine, register_engine, unregister_engine
from claivelib.engines.agy import AgyEngine, AGY_MODEL, default_binary, PROMPT_LIMIT
from claivelib.usage import aggregate_usage, record_cumulative, recent_usage


class AgyChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agy-check-")
        self.base = Path(self.temp.name)
        self.registry = self.base / "state"
        self.prompt = self.base / "prompt.md"
        self.prompt.write_text("Read the file and report.")
        self.engine = AgyEngine()
        self.env = dict(os.environ, CLAIVE_DIR=str(self.registry),
                        AGY_WORKER_BINARY=str(ROOT / "tests/fake_agy.py"),
                        FAKE_AGY_SCENARIO="lifecycle", FAKE_AGY_STORE=str(self.base / "sessions.json"))
        self.jobs = []

    def tearDown(self):
        for job in self.jobs:
            self.command("close", job)
            deadline = time.monotonic() + 10
            while self.state(job)["status"] in cli.ACTIVE and time.monotonic() < deadline:
                time.sleep(0.03)
        self.temp.cleanup()

    def request(self, **changes):
        request = TurnRequest(binary=self.env["AGY_WORKER_BINARY"], workspace=str(self.base),
                              prompt_file=str(self.prompt), session_id=None, provider="antigravity",
                              model=AGY_MODEL, reasoning_effort="high", max_model_steps=None,
                              read_only=False, web=False, output_schema=None, session_logging=True, isolation={})
        return dataclasses.replace(request, **changes)

    def command(self, *args):
        return subprocess.run([str(ROOT / "bin/claive"), *args], env=self.env,
                              capture_output=True, text=True, timeout=20)

    def launch(self, *args, action="run"):
        result = self.command(action, "--engine", "agy", "--workspace", str(self.base),
                              "--prompt-file", str(self.prompt), "--turn-timeout", "10", *args)
        found = re.search(r"Worker ([0-9a-f]{12})", result.stdout)
        job = found.group(1) if found else None
        if job and action == "open":
            self.jobs.append(job)
        return result, job

    def state(self, job):
        return json.loads((self.registry / job / "state.json").read_text())

    def idle(self, job, turn):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            state = self.state(job)
            if state["status"] == "idle" and state["turn"] == turn:
                return state
            time.sleep(0.025)
        self.fail(f"AGY did not become idle: {state}")

    def test_command_model_and_refusals(self):
        command = self.engine.build_command(self.request())
        self.assertEqual(command[-2:], ["-p", self.prompt.read_text()])
        self.assertIn("--dangerously-skip-permissions", command)
        for flag in ("--effort", "--mode", "--sandbox", "--print-timeout", "--conversation"):
            self.assertNotIn(flag, command)
        resumed = self.engine.build_command(self.request(session_id="retained"))
        self.assertEqual(resumed[resumed.index("--conversation") + 1], "retained")
        self.assertEqual(self.engine.default_effort_for(None), "high")
        self.assertEqual(self.engine.fallback_effort_for("gemini-3.8-flash-low", "high"), "low")
        for changes in (dict(model="gemini-3.8-flash"), dict(reasoning_effort="low"),
                        dict(web=True), dict(output_schema="{}"), dict(provider="other"),
                        dict(max_model_steps=1), dict(session_logging=False), dict(steering=True),
                        dict(fork_session_id="parent")):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.engine.build_command(self.request(**changes))
        with self.assertRaisesRegex(ValueError, "session-id"):
            self.engine.resolve_session_id("imported")
        self.assertIsNone(self.engine.resolve_session_id(None))
        with mock.patch.dict(os.environ, {"CLAIVE_AGY_YOLO": "0"}):
            self.assertNotIn("--dangerously-skip-permissions", self.engine.build_command(self.request()))
        self.prompt.write_text("é" * (PROMPT_LIMIT // 2 + 1))
        with self.assertRaisesRegex(ValueError, "UTF-8 bytes"):
            self.engine.build_command(self.request())

    def test_binary_resolution_and_missing_binary(self):
        with mock.patch.dict(os.environ, {"AGY_WORKER_BINARY": "/custom/agy"}):
            self.assertEqual(default_binary(), "/custom/agy")
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch("pathlib.Path.home", return_value=self.base):
            local = self.base / ".local/bin/agy"
            local.parent.mkdir(parents=True)
            local.write_text("fixture")
            self.assertEqual(default_binary(), str(local))
            local.unlink()
            with mock.patch("shutil.which", return_value="/path/agy"):
                self.assertEqual(default_binary(), "/path/agy")
        self.env["AGY_WORKER_BINARY"] = str(self.base / "missing")
        result, job = self.launch()
        self.assertIsNone(job)
        self.assertIn("absolute executable", result.stderr)

    def test_default_launch_and_soft_read(self):
        result, job = self.launch("--read-only")
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.state(job)
        answer = json.loads((self.registry / job / "result.txt").read_text())
        self.assertEqual(state["model"], AGY_MODEL)
        self.assertEqual(state["read_mode"], "soft")
        self.assertIn("Read-only task.", answer["prompt"])
        shown = self.command("show", job)
        self.assertIn("instruction only; not enforced", shown.stdout)

    def test_enforced_read_and_budget_refuse_before_job(self):
        result, job = self.launch("--require-read-only")
        self.assertIsNone(job)
        self.assertIn("cannot enforce read-only", result.stderr)
        config = self.base / "config.json"
        config.write_text(json.dumps({"engines": {"agy": {"budget": True}}}))
        self.env["CLAIVE_CONFIG"] = str(config)
        result, job = self.launch()
        self.assertIsNone(job)
        self.assertIn("budget", result.stderr)
        self.assertEqual(list(self.registry.glob("*/state.json")), [])

    def test_resume_usage_and_peek(self):
        result, job = self.launch("--detach", action="open")
        self.assertEqual(result.returncode, 0, result.stderr)
        first = self.idle(job, 1)
        self.assertNotIn("--conversation", first["command"])
        follow = self.command("followup", job, "--prompt-file", str(self.prompt))
        self.assertEqual(follow.returncode, 0, follow.stderr)
        second = self.idle(job, 2)
        self.assertEqual(first["session_id"], second["session_id"])
        self.assertIn(first["session_id"], second["command"])
        usage = json.loads(self.command("usage", job, "--json").stdout)
        self.assertIsNone(usage["model_calls"])
        self.assertEqual(usage["calls"], [])
        self.assertEqual(usage["totals"]["input_tokens"], 200)
        self.assertEqual(usage["turn"]["input_tokens"], 100)
        self.assertTrue(usage["usage_complete"])
        text = self.command("usage", job).stdout
        self.assertIn("model calls unknown", text)
        self.assertNotIn("Reasoning-heavy", text)
        peek = self.command("peek", job, "--json")
        self.assertEqual(peek.returncode, 0, peek.stderr)
        self.assertIn("Read the file", peek.stdout)

    def test_resume_mismatch_and_declining_usage_fail(self):
        for scenario in ("mismatch", "declining"):
            with self.subTest(scenario=scenario):
                self.env["FAKE_AGY_SCENARIO"] = scenario
                result, job = self.launch("--detach", action="open")
                self.assertEqual(result.returncode, 0, result.stderr)
                first = self.idle(job, 1)
                self.command("followup", job, "--prompt-file", str(self.prompt))
                second = self.idle(job, 2)
                self.assertEqual(second["last_turn_status"], "failed")
                self.assertEqual(second["failure_kind"], "protocol")
                self.assertEqual(second["session_id"], first["session_id"])
                self.assertEqual(second["usage_baseline"], first["usage_baseline"])
                self.assertEqual((self.registry / job / "result.txt").read_text(), "")

    def test_failures_and_banner_boundaries(self):
        for scenario, failure in (("timeout", "timeout"), ("missing", "protocol"),
                                  ("malformed", "protocol"), ("post-banner", "protocol"),
                                  ("unknown-status", "protocol"), ("invalid-usage", "protocol"),
                                  ("no-init-corrupt", "protocol")):
            with self.subTest(scenario=scenario):
                self.env["FAKE_AGY_SCENARIO"] = scenario
                result, job = self.launch()
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.state(job)["failure_kind"], failure)
        self.env["FAKE_AGY_SCENARIO"] = "pre-banner"
        result, job = self.launch("--detach", action="open")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.idle(job, 1)
        self.command("followup", job, "--prompt-file", str(self.prompt))
        self.assertEqual(self.idle(job, 2)["last_turn_status"], "completed")

    def test_fallback_persists_effort_and_resumes_low(self):
        self.env["FAKE_AGY_SCENARIO"] = "fallback"
        result, job = self.launch("--detach", "--fallback-models", "gemini-3.8-flash-low", action="open")
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.idle(job, 1)
        self.assertEqual(state["reasoning_effort"], "low")
        self.assertEqual(state["model"], "gemini-3.8-flash-low")
        policy = json.loads((self.registry / job / "policy.json").read_text())
        self.assertEqual(policy["reasoning_effort"], "low")
        self.assertEqual(state["turn_policy"], policy)
        self.assertEqual(len(state["usage_turns"]), 2)
        self.assertNotEqual(*[row["conversation_id"] for row in state["usage_turns"]])
        self.command("followup", job, "--prompt-file", str(self.prompt))
        second = self.idle(job, 2)
        self.assertEqual(second["last_turn_status"], "completed")
        self.assertEqual(second["reasoning_effort"], "low")
        self.assertEqual(second["model"], "gemini-3.8-flash-low")

    def test_policy_write_failure_recovers_from_state(self):
        folder = self.base / "policy"
        folder.mkdir()
        state = dict(model="gemini-3.8-flash-low")
        real_save = cli.save
        def save(path, value):
            if path.name == "policy.json":
                raise OSError("injected projection failure")
            real_save(path, value)
        policy = dict(reasoning_effort="low", max_model_steps=None)
        with mock.patch.object(cli, "save", side_effect=save):
            cli.save_policy(folder, state, policy)
        recovered = json.loads((folder / "state.json").read_text())
        self.assertEqual(cli.load_policy(folder, recovered), policy)
        self.assertIn("injected", recovered["policy_projection_error"])

    def test_os_argument_limit_is_a_clear_launch_failure(self):
        args = cli.parser().parse_args(["run", "--engine", "agy", "--workspace", str(self.base),
                                       "--prompt-file", str(self.prompt)])
        real_spawn = subprocess.Popen
        def spawn(command, *args, **kwargs):
            if command[0] == self.env["AGY_WORKER_BINARY"]:
                raise OSError(errno.E2BIG, "Argument list too long")
            return real_spawn(command, *args, **kwargs)
        with mock.patch.dict(os.environ, self.env), mock.patch.object(cli, "SCRIPT", str(ROOT / "bin/claive")):
            path, state = cli.create_job(args)
            with mock.patch.object(subprocess, "Popen", side_effect=spawn), mock.patch("signal.signal"):
                self.assertNotEqual(cli.supervise(state["id"]), 0)
        failed = json.loads((path / "state.json").read_text())
        self.assertEqual(failed["failure_kind"], "launch")
        self.assertIn("E2BIG", failed["error"])

    def test_unknown_usage_and_no_heavy_call_inference(self):
        state = dict(turn=1, attempt=1, model=AGY_MODEL, session_id="conversation")
        record_cumulative(state)
        usage = aggregate_usage(state)
        self.assertFalse(usage["usage_complete"])
        self.assertEqual(usage["totals"], {})
        self.assertNotIn("input_tokens", usage["turn"])
        self.assertIsNone(usage["cache_hit_ratio"])
        aggregate = [dict(granularity="attempt", input_tokens=100000, output_tokens=50000)]
        self.assertIsNone(advice.heavy_call_note(aggregate))
        self.assertEqual(recent_usage(aggregate)["input_tokens"], "unknown")

    def test_recorded_deltas_and_tool_results_reach_peek(self):
        from claivelib import peek
        fixture = ROOT / "tests/fixtures/agy/edit.ndjson"
        folder = self.base / "peek"
        folder.mkdir()
        (folder / "events.jsonl").write_text(fixture.read_text())
        steps, _, _ = peek.steps_window(folder, self.engine, 100)
        self.assertTrue(any(row["kind"] == "tool_call" for row in steps))
        self.assertTrue(any(row["kind"] == "tool_result" for row in steps))
        rows = [json.loads(line) for line in fixture.read_text().splitlines()]
        expected = [row["step_update"]["text_delta"] for row in rows
                    if row.get("step_update", {}).get("text_delta")]
        actual = [row["text"] for row in steps if row["kind"] == "text"]
        for text in expected:
            self.assertIn(text.strip(), actual)

    def test_soft_read_is_not_workspace_write_permission(self):
        config = self.base / "config.json"
        config.write_text(json.dumps({"workspaces": [{"path": str(self.base), "write": False}]}))
        self.env["CLAIVE_CONFIG"] = str(config)
        result, job = self.launch("--read-only")
        self.assertIsNone(job)
        self.assertIn("select enforced read-only", result.stderr)

    def test_prompt_limit_refuses_before_job_creation(self):
        self.prompt.write_text("x" * (PROMPT_LIMIT + 1))
        result, job = self.launch()
        self.assertIsNone(job)
        self.assertIn("100001 UTF-8 bytes", result.stderr)
        self.assertEqual(list(self.registry.glob("*/state.json")), [])

    def test_failed_terminal_and_session_mismatch_latch(self):
        state = dict(engine="agy", launch=dict(session_logging=True), session_id="expected",
                     model=AGY_MODEL, turn=1, attempt=1, steps=0)
        cli.event_update(state, dict(type="session_bound", session_id="different", strict=True))
        cli.event_update(state, dict(type="terminal_completed", terminal="completed", text="wrong answer"))
        self.assertEqual(state["terminal"], "failed")
        self.assertEqual(state["failure_kind"], "protocol")
        self.assertEqual(state["answer"], "")
        self.assertEqual(state["session_id"], "expected")

    def test_fallback_policy_rejection_keeps_original_launch(self):
        self.env["FAKE_AGY_SCENARIO"] = "fallback"
        config = self.base / "config.json"
        config.write_text(json.dumps({"engines": {"agy": {"disabled_models": ["gemini-3.8-flash-low"]}}}))
        self.env["CLAIVE_CONFIG"] = str(config)
        result, job = self.launch("--fallback-models", "gemini-3.8-flash-low")
        self.assertNotEqual(result.returncode, 0)
        state = self.state(job)
        self.assertEqual(state["model"], AGY_MODEL)
        self.assertEqual(state["reasoning_effort"], "high")
        self.assertIn("disabled", state["fallback_error"])

    def test_capabilities_and_dynamic_engine_workspace(self):
        self.assertIn("agy", ENGINE_NAMES)
        self.assertIsInstance(get_engine("agy"), AgyEngine)
        register_engine("dynamic", WorkerEngine())
        try:
            state = dict(schema_version=2, engine="dynamic", workspace="/parent", actual_workspace="/tree",
                         launch=dict(binary="/bin/true", provider="test", isolation={}),
                         prompt_file=str(self.prompt), reasoning_effort="high", max_model_steps=None)
            self.assertEqual(cli.turn_request(state).workspace, "/tree")
        finally:
            unregister_engine("dynamic")

    def test_explicit_queued_effort_adapts_after_fallback(self):
        gate = self.base / "release-high"
        self.env.update(FAKE_AGY_SCENARIO="fallback", FAKE_AGY_GATE=str(gate))
        result, job = self.launch("--detach", "--fallback-models", "gemini-3.8-flash-low", action="open")
        self.assertEqual(result.returncode, 0, result.stderr)
        queued = self.command("followup", job, "--prompt-file", str(self.prompt), "--reasoning-effort", "high")
        self.assertEqual(queued.returncode, 0, queued.stderr)
        gate.touch()
        state = self.idle(job, 2)
        self.assertEqual(state["last_turn_status"], "completed")
        self.assertEqual(state["model"], "gemini-3.8-flash-low")
        self.assertEqual(state["reasoning_effort"], "low")

    def test_malformed_result_does_not_hide_show_or_peek(self):
        result, job = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        events = self.registry / job / "events.jsonl"
        for body in (dict(status="SUCCESS", response="x"),
                     dict(conversation_id="id", status="SUCCESS", response={}),
                     dict(conversation_id="id", status="MAYBE", response="x")):
            with self.subTest(body=body):
                events.write_text(json.dumps(dict(event="result", result=body)) + "\n")
                shown = self.command("show", job, "--json")
                self.assertEqual(shown.returncode, 0, shown.stderr)
                peeked = self.command("peek", job, "--json")
                self.assertEqual(peeked.returncode, 0, peeked.stderr)
                self.assertIn('"error"', peeked.stdout)

    def test_partial_usage_retains_baselines_and_recovers_next_attempt(self):
        state = dict(turn=1, attempt=1, model=AGY_MODEL, session_id="id")
        def terminal(value, cache=10):
            return dict(conversation_id="id", usage=dict(input_tokens=value, output_tokens=value,
                        total_tokens=value * 2, thinking_tokens=value, cache_read_tokens=cache))
        record_cumulative(state, terminal(100))
        state["turn"] = 2
        partial = terminal(200)
        del partial["usage"]["thinking_tokens"]
        record_cumulative(state, partial)
        self.assertEqual(state["usage_baseline"]["id"]["thinking_tokens"], 100)
        state["turn"] = 3
        record_cumulative(state, terminal(300, cache=5))
        self.assertTrue(state["turn_usage"]["spans_unreported_attempts"])
        self.assertEqual(state["turn_usage"]["reasoning_tokens"], 200)
        self.assertEqual(state["turn_usage"]["native_cache_read_tokens"], 5)
        state["turn"] = 4
        record_cumulative(state, terminal(400, cache=2))
        self.assertTrue(state["turn_usage"]["complete"])
        self.assertNotIn("spans_unreported_attempts", state["turn_usage"])
        usage = aggregate_usage(state)
        self.assertTrue(usage["turn"]["complete"])
        self.assertFalse(usage["usage_complete"])
        self.assertEqual(usage["totals"]["reasoning_tokens"], 400)
        self.assertNotIn("cache_read_tokens", usage["totals"])

    def test_missing_result_gap_affects_one_recovery_record(self):
        state = dict(turn=1, attempt=1, model=AGY_MODEL, session_id="id")
        record_cumulative(state)
        for turn in (2, 3):
            state["turn"] = turn
            usage = dict(input_tokens=turn * 100, output_tokens=turn * 10,
                         total_tokens=turn * 110, thinking_tokens=turn * 5, cache_read_tokens=0)
            record_cumulative(state, dict(conversation_id="id", usage=usage))
        self.assertTrue(state["usage_turns"][1]["spans_unreported_attempts"])
        self.assertFalse(state["usage_turns"][1]["complete"])
        self.assertTrue(state["usage_turns"][2]["complete"])

    def test_invalid_fallback_model_refuses_before_job(self):
        result, job = self.launch("--fallback-models", "gemini-3.8-flash-bogus")
        self.assertIsNone(job)
        self.assertIn("unsupported AGY model", result.stderr)
        self.assertEqual(list(self.registry.glob("*/state.json")), [])

    def test_partial_projection_uses_canonical_defaults(self):
        folder = self.base / "projection"
        folder.mkdir()
        policy = dict(reasoning_effort="low", max_model_steps=None)
        state = dict(model="gemini-3.8-flash-low", turn_policy=policy)
        (folder / "policy.json").write_text(json.dumps(dict(override_for_model=state["model"],
                                                          reasoning_effort="low")))
        self.assertEqual(cli.load_policy(folder, state), policy)

    def test_show_retained_worker_when_adapter_is_missing(self):
        result, job = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.state(job)
        state["engine"] = "retired-engine"
        state["launch"]["read_only"] = True
        (self.registry / job / "state.json").write_text(json.dumps(state))
        shown = self.command("show", job, "--json")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertEqual(json.loads(shown.stdout)["read_mode"], "unknown")

    def test_all_soft_adapters_refuse_enforcement_and_write_prohibition(self):
        config = self.base / "read-policy.json"
        config.write_text(json.dumps({"workspaces": [{"path": str(self.base), "write": False}]}))
        original = self.env.get("CLAIVE_CONFIG")
        for engine in ("agy", "pi", "claude"):
            with self.subTest(engine=engine):
                base = ("run", "--engine", engine, "--workspace", str(self.base),
                        "--prompt-file", str(self.prompt))
                required = self.command(*base, "--require-read-only")
                self.assertIn("cannot enforce read-only", required.stderr)
                self.env["CLAIVE_CONFIG"] = str(config)
                soft = self.command(*base, "--read-only")
                self.assertIn("select enforced read-only", soft.stderr)
                # Restore, never delete: an absent CLAIVE_CONFIG falls back to the host's real
                # config.json, so a host that disables an engine would change this test's result.
                if original is None:
                    self.env.pop("CLAIVE_CONFIG", None)
                else:
                    self.env["CLAIVE_CONFIG"] = original
        self.assertEqual(list(self.registry.glob("*/state.json")), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
