"""Acceptance checks for doctor, worker reports, roles, parent questions and failure kinds.

Spec: docs/superpowers/specs/2026-10-06-worker-contract-features.md
"""
import hermetic  # noqa: F401  (must run before claivelib reads the environment)
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

from test_workers import CLI, FAKE, FIXTURE_CLI, FIXTURE_WORKER, module

REPORT_DONE = """Work finished.

```claive-report
{"status": "done", "summary": "Added the parser.", "changed_files": ["a.py"],
 "commands_run": [{"command": "pytest -q", "exit_code": 0}], "residual_risks": ["none known"]}
```
"""
REPORT_ASK = """I need a choice.

```claive-report
{"status": "needs_decision", "summary": "Two valid designs.", "question": "Use design A or B?"}
```
"""


class FeatureChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="claive-features-")
        self.path = Path(self.temp.name)
        self.registry = self.path / "registry"
        self.prompt = self.path / "task.md"
        self.prompt.write_text("ORIGINAL TASK TEXT")
        self.text = self.path / "answer.txt"
        self.captured = self.path / "captured.txt"
        self.fake = self.path / "fake-muse"
        self.fake.write_text(FAKE)
        self.fake.chmod(0o700)
        self.pi_dir = self.path / "pi-agent"
        self.pi_dir.mkdir()
        self.env = dict(os.environ, CLAIVE_DIR=str(self.registry), TERM="dumb",
                        MUSE_WORKER_BINARY=str(self.fake), PI_WORKER_BINARY=str(self.path / "no-pi"),
                        PI_CODING_AGENT_DIR=str(self.pi_dir),
                        FIXTURE_WORKER_BINARY=FIXTURE_WORKER, FIXTURE_MODE="success",
                        FIXTURE_TEXT_FILE=str(self.text), FIXTURE_PROMPT_OUT=str(self.captured))
        self.text.write_text("fixture result")
        self.processes = []

    def tearDown(self):
        if self.registry.exists():
            for folder in self.registry.iterdir():
                if (folder / "state.json").exists():
                    state = json.loads((folder / "state.json").read_text())
                    for field in ("worker", "supervisor"):
                        pid = state.get(field + "_pid")
                        if pid and module.identity(pid) == state.get(field + "_identity"):
                            try:
                                os.killpg(pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
        for process in self.processes:
            process.communicate(timeout=5)
        self.temp.cleanup()

    def run_cli(self, *args, cli=FIXTURE_CLI, timeout=15, **env):
        return subprocess.run([cli, *args], env=dict(self.env, **env), text=True,
                              capture_output=True, timeout=timeout)

    def fixture_run(self, *extra, action="run", **env):
        result = self.run_cli(action, "--engine", "fixture", "--workspace", str(self.path),
                              "--prompt-file", str(self.prompt), *extra, **env)
        found = re.search(r"Worker ([0-9a-f]{12})", result.stdout)
        self.assertIsNotNone(found, result.stdout + result.stderr)
        return result, self.state(found.group(1))

    def state(self, job):
        return json.loads((self.registry / job / "state.json").read_text())

    def open_worker(self, *extra, **env):
        before = set(self.registry.glob("*/state.json"))
        process = subprocess.Popen([FIXTURE_CLI, "open", "--engine", "fixture", "--workspace", str(self.path),
                                    "--prompt-file", str(self.prompt), *extra],
                                   env=dict(self.env, **env), stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, start_new_session=True)
        self.processes.append(process)
        for _ in range(400):
            new = set(self.registry.glob("*/state.json")) - before
            if new:
                return process, json.loads(next(iter(new)).read_text())["id"]
            time.sleep(0.025)
        self.fail("reusable worker did not start")

    def until(self, job, predicate, message):
        for _ in range(600):
            state = self.state(job)
            if predicate(state):
                return state
            time.sleep(0.025)
        self.fail(message + ": " + json.dumps(self.state(job))[:800])

    def captured_turns(self):
        return [part for part in self.captured.read_text().split("\n----\n") if part.strip()]

    # 1. doctor ---------------------------------------------------------------
    def test_doctor_checks_without_launching_models(self):
        result = self.run_cli("doctor", "--json", cli=CLI)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        names = [check["name"] for check in report["checks"]]
        self.assertEqual(names, ["python", "state_dir", "config", "muse", "pi", "engines", "pi_provider", "opencode2api",
                                 "git", "quota", "interrupted"])
        checks = {check["name"]: check for check in report["checks"]}
        self.assertTrue(report["ok"])
        self.assertTrue(checks["muse"]["ok"])
        self.assertFalse(checks["pi"]["ok"])
        self.assertFalse(checks["pi"]["required"])
        self.assertTrue(checks["engines"]["ok"])
        self.assertFalse(checks["opencode2api"]["ok"])
        self.assertTrue(checks["quota"]["ok"])
        self.assertFalse(self.captured.exists(), "doctor must not launch workers")

        text = self.run_cli("doctor", cli=CLI)
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertRegex(text.stdout, r"(?m)^warn\s+pi\b")
        self.assertRegex(text.stdout, r"(?m)^ok\s+python\b")

    def test_doctor_reports_quota_and_required_failures(self):
        quota = self.run_cli("run", "--workspace", str(self.path), "--prompt-file", str(self.prompt),
                             cli=CLI, MUSE_TEST_MODE="quota")
        self.assertNotEqual(quota.returncode, 0)
        job = re.search(r"Worker ([0-9a-f]{12})", quota.stdout).group(1)
        self.assertEqual(self.state(job)["failure_kind"], "quota")
        self.assertIn("Failure kind: quota", quota.stdout)
        doctor = json.loads(self.run_cli("doctor", "--json", cli=CLI).stdout)
        checks = {check["name"]: check for check in doctor["checks"]}
        self.assertFalse(checks["quota"]["ok"])
        self.assertIn("2099", checks["quota"]["detail"])
        self.assertIn("muse-spark-1.3-contributor-free", checks["quota"]["detail"])

        blocker = self.path / "not-a-dir"
        blocker.write_text("file")
        broken = self.run_cli("doctor", "--json", cli=CLI, CLAIVE_DIR=str(blocker / "state"),
                              MUSE_WORKER_BINARY=str(self.path / "missing-muse"))
        self.assertEqual(broken.returncode, 1, broken.stdout + broken.stderr)
        checks = {check["name"]: check for check in json.loads(broken.stdout)["checks"]}
        self.assertFalse(checks["state_dir"]["ok"])
        self.assertFalse(checks["engines"]["ok"])

    def test_disallowed_models_are_refused(self):
        for model in ("nvidia-nemotron-3-free", "ling-3.1-flash-free"):
            result = self.run_cli("run", "--engine", "fixture", "--workspace", str(self.path),
                                  "--prompt-file", str(self.prompt), "--model", model)
            self.assertNotEqual(result.returncode, 0, model)
            self.assertIn("disallowed", result.stderr)
        self.assertFalse(self.registry.exists() and any(self.registry.glob("*/state.json")))

    # 3. structured report ----------------------------------------------------
    def test_report_contract_is_opt_in_and_parsed(self):
        result, state = self.fixture_run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.captured_turns()[0].strip(), "ORIGINAL TASK TEXT")
        self.assertNotIn("report_state", state)

        self.captured.unlink()
        self.text.write_text(REPORT_DONE)
        result, state = self.fixture_run("--report")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        prompt = self.captured_turns()[0]
        self.assertIn("ORIGINAL TASK TEXT", prompt)
        self.assertIn("claive-report", prompt)
        self.assertIn("needs_decision", prompt)
        self.assertEqual(state["source_prompt_file"], str(self.prompt))
        self.assertNotEqual(state["prompt_file"], str(self.prompt))
        self.assertTrue(state["report_contract"])
        self.assertEqual(state["report_state"], "ok")
        self.assertEqual(state["report"]["changed_files"], ["a.py"])
        self.assertEqual(state["report"]["commands_run"], [{"command": "pytest -q", "exit_code": 0}])
        self.assertIsNone(state["report"]["question"])
        self.assertIn("Added the parser.", result.stdout)
        self.assertIn("a.py", result.stdout)
        self.assertEqual((self.registry / state["id"] / "result.txt").read_text(), REPORT_DONE)

    def test_report_missing_invalid_and_last_block_wins(self):
        self.text.write_text("no report here")
        result, state = self.fixture_run("--report")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["report_state"], "missing")
        self.assertIn("missing", result.stdout)

        self.text.write_text('```claive-report\n{"status": "maybe", "summary": "x"}\n```\n')
        result, state = self.fixture_run("--report")
        self.assertEqual(state["report_state"], "invalid")
        self.assertTrue(state["report_error"])

        self.text.write_text('```claive-report\n{"status": "needs_decision", "summary": "x"}\n```\n')
        _result, state = self.fixture_run("--report")
        self.assertEqual(state["report_state"], "invalid", "question is required unless done")

        self.text.write_text(REPORT_ASK + "\nUpdate:\n" + REPORT_DONE)
        result, state = self.fixture_run("--report")
        self.assertEqual(state["report_state"], "ok")
        self.assertEqual(state["report"]["status"], "done")
        self.assertNotIn("needs_parent", state)
        self.assertEqual(result.returncode, 0)

    # 5. roles ----------------------------------------------------------------
    def test_roles_set_defaults_and_explicit_flags_win(self):
        self.text.write_text(REPORT_DONE)
        result, state = self.fixture_run("--role", "reviewer")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(state["role"], "reviewer")
        self.assertEqual(state["model"], "big-pickle")
        self.assertEqual(state["reasoning_effort"], "max")
        self.assertTrue(state["launch"]["read_only"])
        self.assertTrue(state["report_contract"])
        prompt = self.captured_turns()[-1]
        self.assertIn("ORIGINAL TASK TEXT", prompt)
        self.assertIn("claive-report", prompt)
        self.assertGreater(len(prompt), len("ORIGINAL TASK TEXT") + 200, "role preamble expected")

        _result, state = self.fixture_run("--role", "worker", "--model", "custom-model")
        self.assertEqual(state["model"], "custom-model")
        self.assertEqual(state["reasoning_effort"], "xhigh")
        self.assertEqual(state["max_model_steps"], 100)
        self.assertFalse(state["launch"]["read_only"])

        _result, state = self.fixture_run("--role", "scout", "--reasoning-effort", "high")
        self.assertEqual(state["model"], "mimo-v2.6-flash-free")
        self.assertEqual(state["reasoning_effort"], "high")
        self.assertTrue(state["launch"]["read_only"])

        _result, state = self.fixture_run("--role", "oracle")
        self.assertEqual(state["model"], "space-bunny-free")

        bad = self.run_cli("run", "--role", "boss", "--engine", "fixture", "--workspace", str(self.path),
                           "--prompt-file", str(self.prompt))
        self.assertNotEqual(bad.returncode, 0)

    def test_role_engine_defaults_without_engine_flag(self):
        sys.path.insert(0, str(Path(CLI).parent))
        from claivelib.roles import ROLES
        self.assertEqual(ROLES["worker"]["engine"], "muse")
        self.assertEqual(ROLES["worker"]["model"], "muse-spark-1.3-contributor")
        for name in ("scout", "reviewer", "oracle"):
            self.assertEqual(ROLES[name]["engine"], "pi")
            self.assertTrue(ROLES[name]["read_only"])
        result = self.run_cli("run", "--role", "worker", "--workspace", str(self.path),
                              "--prompt-file", str(self.prompt), cli=CLI)
        job = re.search(r"Worker ([0-9a-f]{12})", result.stdout).group(1)
        state = self.state(job)
        self.assertEqual(state["engine"], "muse")
        self.assertEqual(state["reasoning_effort"], "xhigh")

    def test_config_sets_default_engine_and_role_overrides(self):
        config = self.path / "config.json"
        config.write_text(json.dumps({"default_engine": "fixture", "roles": {"worker": {
            "engine": "fixture", "model": "config-model", "reasoning_effort": "high"}}}))

        def launch(*extra, **env):
            result = self.run_cli("run", "--workspace", str(self.path), "--prompt-file", str(self.prompt),
                                  *extra, CLAIVE_CONFIG=str(config), **env)
            found = re.search(r"Worker ([0-9a-f]{12})", result.stdout)
            self.assertIsNotNone(found, result.stdout + result.stderr)
            return self.state(found.group(1))

        self.assertEqual(launch()["engine"], "fixture")
        state = launch("--role", "worker")
        self.assertEqual((state["engine"], state["model"], state["reasoning_effort"]),
                         ("fixture", "config-model", "high"))
        self.assertTrue(state["report_contract"])
        self.assertEqual(launch(CLAIVE_ENGINE="muse")["engine"], "muse")
        self.assertEqual(launch("--engine", "muse")["engine"], "muse")

        for bad in ({"roles": {"wrker": {}}}, {"rolez": {}}, {"roles": {"worker": {"max_model_steps": 0}}},
                    {"roles": {"worker": {"read_only": "yes"}}}, {"default_engine": ""}):
            config.write_text(json.dumps(bad))
            before = sorted(self.registry.iterdir()) if self.registry.exists() else []
            refused = self.run_cli("run", "--workspace", str(self.path), "--prompt-file", str(self.prompt),
                                   CLAIVE_CONFIG=str(config))
            self.assertNotEqual(refused.returncode, 0, bad)
            self.assertIn("invalid claive config", refused.stderr, bad)
            self.assertEqual(sorted(self.registry.iterdir()) if self.registry.exists() else [], before, bad)
            doctor = json.loads(self.run_cli("doctor", "--json", cli=CLI, CLAIVE_CONFIG=str(config)).stdout)
            checks = {check["name"]: check for check in doctor["checks"]}
            self.assertFalse(checks["config"]["ok"], bad)
            self.assertTrue(checks["config"]["required"])

    def test_workspace_allowlist_limits_where_and_how_workers_run(self):
        allowed, outside = self.path / "allowed", self.path / "outside"
        (allowed / "rw" / "deep").mkdir(parents=True)
        outside.mkdir()
        (allowed / "escape").symlink_to(outside)
        config = self.path / "config.json"
        config.write_text(json.dumps({"default_engine": "fixture", "workspaces": [
            {"path": str(allowed)}, {"path": str(allowed / "rw"), "write": True}]}))

        def launch(workspace, *extra):
            before = sorted(self.registry.iterdir()) if self.registry.exists() else []
            result = self.run_cli("run", "--workspace", str(workspace), "--prompt-file", str(self.prompt),
                                  *extra, CLAIVE_CONFIG=str(config))
            created = sorted(self.registry.iterdir()) if self.registry.exists() else []
            return result, created != before

        for workspace, extra, message in (
                (outside, (), "outside the claive config workspaces allowlist"),
                (allowed / "escape", ("--read-only",), "outside the claive config workspaces allowlist"),
                (allowed, (), "read-only in the claive config workspaces allowlist")):
            result, created = launch(workspace, *extra)
            self.assertNotEqual(result.returncode, 0, workspace)
            self.assertIn(message, result.stderr, workspace)
            self.assertFalse(created, workspace)
        for workspace, extra in ((allowed, ("--read-only",)), (allowed / "rw" / "deep", ())):
            result, created = launch(workspace, *extra)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue(created)
        doctor = json.loads(self.run_cli("doctor", "--json", cli=CLI, CLAIVE_CONFIG=str(config)).stdout)
        detail = {check["name"]: check for check in doctor["checks"]}["config"]["detail"]
        self.assertIn("workspaces allowlist: 2 entries, 1 writable", detail)
        for bad in ([{"path": "relative/dir"}], [{"path": str(allowed), "write": "yes"}], {"path": "/"}):
            config.write_text(json.dumps({"workspaces": bad}))
            refused, created = launch(allowed, "--read-only")
            self.assertIn("invalid claive config", refused.stderr, bad)

    # 7. parent questions -----------------------------------------------------
    def test_single_turn_question_exits_3(self):
        self.text.write_text(REPORT_ASK)
        result, state = self.fixture_run("--report")
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertEqual(state["needs_parent"], {"kind": "needs_decision", "question": "Use design A or B?"})
        self.assertIn("Use design A or B?", result.stdout)
        waited = self.run_cli("wait", state["id"])
        self.assertEqual(waited.returncode, 3)
        refused = self.run_cli("answer", state["id"], "--message", "A")
        self.assertNotEqual(refused.returncode, 0)

    def test_reusable_question_answer_round_trip(self):
        self.text.write_text(REPORT_ASK)
        process, job = self.open_worker("--report")
        self.until(job, lambda s: s["status"] == "idle" and s["turn"] == 1, "first turn")
        state = self.state(job)
        self.assertEqual(state["needs_parent"]["kind"], "needs_decision")
        self.assertEqual(self.run_cli("wait", job).returncode, 3)
        listing = self.run_cli("list")
        self.assertIn("ASK", listing.stdout)
        self.assertIn("1 need you", self.run_cli("status-line").stdout)

        self.text.write_text(REPORT_DONE)
        answered = self.run_cli("answer", job, "--message", "Use design B.")
        self.assertEqual(answered.returncode, 0, answered.stderr)
        self.assertIn(f"Answer queued for {job}", answered.stdout)
        state = self.until(job, lambda s: s["status"] == "idle" and s["turn"] == 2, "answer turn")
        self.assertNotIn("needs_parent", state)
        self.assertEqual(state["report_state"], "ok")
        second = self.captured_turns()[1]
        self.assertIn("Use design B.", second)
        self.assertIn("Use design A or B?", second)
        self.assertIn("claive-report", second)
        self.assertEqual(self.run_cli("wait", job).returncode, 0)
        again = self.run_cli("answer", job, "--message", "again")
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("not waiting", again.stderr)
        self.assertEqual(self.run_cli("close", job).returncode, 0)
        process.communicate(timeout=10)

    # 8. failure kinds --------------------------------------------------------
    def test_failure_kinds(self):
        expected = {"malformed": "protocol", "missing": "protocol",
                    "terminal-failure": "worker", "exit-failure": "worker"}
        for mode, kind in expected.items():
            result, state = self.fixture_run(FIXTURE_MODE=mode)
            self.assertEqual(state["status"], "failed", mode)
            self.assertEqual(state.get("failure_kind"), kind, mode)
            self.assertIn(f"Failure kind: {kind}", result.stdout, mode)
        _result, state = self.fixture_run()
        self.assertNotIn("failure_kind", state)

    def test_rejected_follow_up_kind(self):
        process, job = self.open_worker(FIXTURE_MODE="brief")
        self.until(job, lambda s: s.get("worker_pid"), "first turn running")
        doomed = self.path / "doomed.md"
        doomed.write_text("follow-up")
        queued = self.run_cli("followup", job, "--prompt-file", str(doomed))
        self.assertEqual(queued.returncode, 0, queued.stderr)
        doomed.unlink()
        state = self.until(job, lambda s: s.get("failure_kind") == "rejected", "rejected follow-up")
        self.assertEqual(state["last_turn_status"], "failed")
        self.assertEqual(self.run_cli("close", job).returncode, 0)
        process.communicate(timeout=10)


if __name__ == "__main__":
    unittest.main()
