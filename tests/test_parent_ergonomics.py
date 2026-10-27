"""Parent-facing launch, diagnosis, pruning and report checks."""
import hermetic  # noqa: F401
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from test_workers import FIXTURE_CLI, FIXTURE_WORKER, module
from claivelib import advice


class ParentErgonomicsChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="claive-parent-")
        self.base = Path(self.temp.name)
        self.registry = self.base / "registry"
        self.registry.mkdir()
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        subprocess.run(["git", "init", "-q", str(self.workspace)], check=True)
        self.prompt = self.base / "task.md"
        self.prompt.write_text("task")
        self.env = dict(os.environ, CLAIVE_DIR=str(self.registry),
                        FIXTURE_WORKER_BINARY=FIXTURE_WORKER, FIXTURE_MODE="success")

    def tearDown(self):
        self.temp.cleanup()

    def job(self, identifier, age, label="label", **extra):
        path = self.registry / identifier
        path.mkdir()
        state = dict(id=identifier, label=label, status="interrupted", started_at=time.time() - age,
                     workspace=str(self.workspace), launch={}, **extra)
        module.save(path / "state.json", state)
        return path

    def cli(self, *args):
        return subprocess.run([FIXTURE_CLI, *args], env=self.env, text=True,
                              capture_output=True, timeout=15)

    def launch(self, *args):
        return self.cli("run", "--engine", "fixture", "--workspace", str(self.workspace),
                        "--prompt-file", str(self.prompt), *args)

    def test_doctor_lists_newest_and_only_recent_fails(self):
        self.job("111111111111", 180000, "old")
        self.job("222222222222", 7200, "middle")
        self.job("333333333333", 2700, "newest")
        with mock.patch.dict(os.environ, {"CLAIVE_DIR": str(self.registry)}):
            check = next(item for item in module.doctor_checks() if item["name"] == "interrupted")
        self.assertFalse(check["ok"])
        self.assertIn("333333333333 newest 45m", check["detail"])
        self.assertLess(check["detail"].index("newest"), check["detail"].index("middle"))
        self.assertIn("1 older than 24h; run claive prune --interrupted", check["detail"])
        self.assertLessEqual(len(check["detail"]), 200)
        for identifier in ("222222222222", "333333333333"):
            state_path = self.registry / identifier / "state.json"
            state = json.loads(state_path.read_text())
            state["finished_at"] = time.time() - 172800
            module.save(state_path, state)
        with mock.patch.dict(os.environ, {"CLAIVE_DIR": str(self.registry)}):
            check = next(item for item in module.doctor_checks() if item["name"] == "interrupted")
        self.assertTrue(check["ok"])
        self.assertIn("3 older than 24h", check["detail"])

    def test_doctor_long_label_keeps_age_and_fits_limit(self):
        self.job("111111111111", 7200, "x" * 500)
        with mock.patch.dict(os.environ, {"CLAIVE_DIR": str(self.registry)}):
            check = next(item for item in module.doctor_checks() if item["name"] == "interrupted")
        self.assertLessEqual(len(check["detail"]), 200)
        self.assertIn(" 2h; 0 older than 24h", check["detail"])

    def test_prune_dry_run_delete_keeps_worktrees_and_removes_aliases(self):
        deleted = self.job("111111111111", 172800)
        kept = self.job("222222222222", 172800, retained_worktree="/tmp/saved")
        contained = self.job("333333333333", 172800)
        (contained / "worktree").mkdir()
        self.job("444444444444", 60)
        alias_path = self.registry / "aliases.json"
        alias_path.write_text(json.dumps({"old": deleted.name, "safe": kept.name}))
        dry = self.cli("prune", "--interrupted", "--dry-run", "--json")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        data = json.loads(dry.stdout)
        self.assertEqual(data["total"], 3)
        self.assertTrue(deleted.exists())
        self.assertIn("old", json.loads(alias_path.read_text()))
        result = self.cli("prune", "--interrupted")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("deleted 111111111111", result.stdout)
        self.assertIn("kept 222222222222: worktree", result.stdout)
        self.assertIn("kept 333333333333: worktree", result.stdout)
        self.assertFalse(deleted.exists())
        self.assertTrue(kept.exists())
        self.assertEqual(json.loads(alias_path.read_text()), {"safe": kept.name})
        self.assertNotEqual(self.cli("prune", "--interrupted", "--older-than", "bad").returncode, 0)
        self.assertNotEqual(self.cli("prune").returncode, 0)

    def test_fit_checked_suppresses_fit_and_scope_but_not_bound(self):
        notes = advice.launch_notes({"fit_checked": True}, 5000)
        self.assertEqual(len(notes), 1)
        self.assertTrue(notes[0].startswith("Bound:"))
        result = self.launch("--fit-checked")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Fit:", result.stderr)
        self.assertIn("Bound:", result.stderr)
        state = json.loads(next(self.registry.glob("*/state.json")).read_text())
        self.assertTrue(state["fit_checked"])

    def test_inline_checks_merge_and_validate(self):
        checks = self.base / "checks.json"
        checks.write_text(json.dumps([{"name": "file", "command": ["true"]}]))
        result = self.launch("--checks-file", str(checks), "--check", "unit=python3 -m unittest 'a b'")
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads(next(self.registry.glob("*/state.json")).read_text())
        self.assertEqual(state["acceptance_contract"]["checks"], [
            {"name": "file", "command": ["true"]},
            {"name": "unit", "command": ["python3", "-m", "unittest", "a b"], "required": True}])
        duplicate = self.launch("--checks-file", str(checks), "--check", "file=true")
        self.assertNotEqual(duplicate.returncode, 0)
        self.assertIn("duplicate check name", duplicate.stderr)
        for value in ("missing", "=true", "name=", "name=   ", "name=\"unterminated"):
            with self.subTest(value=value):
                self.assertNotEqual(self.launch("--check", value).returncode, 0)

    def test_failure_header_and_last_result_suppressed_without_success_events(self):
        state = {"id": "111111111111", "label": "test", "status": "completed", "steps": 0,
                 "turn": 2, "task_failures": 1, "previous_turn_task_failures": 1}
        module.event_update(state, {"type": "task_warning", "command": "false", "reason": "exit 1"})
        self.assertEqual(state["turn_command_results"], {"false": "fail"})
        self.assertIn("turn_command_results", module.PER_TURN_RESET_KEYS)
        for number in range(21):
            module.event_update(state, {"type": "task_warning", "command": f"cmd-{number}"})
        self.assertEqual(len(state["turn_command_results"]), 20)
        self.assertNotIn("false", state["turn_command_results"])
        state.update(task_failures=2, turn_task_failures=1)
        evidence = {"verification": {"status": "not_run", "checks": []},
                    "acceptance": {"status": "pending"}}
        output = io.StringIO()
        with mock.patch.object(module, "job_path", return_value=self.base), \
             mock.patch.object(module.acceptance, "view", return_value=evidence), \
             mock.patch.object(module.steering, "summary", return_value={"enabled": False, "pending": 0, "acknowledged": 0}), \
             contextlib.redirect_stdout(output):
            module.report(state)
        text = output.getvalue()
        self.assertIn("Command failures during the turn (attempts, not the final result): 2 cumulative; 1 this turn; 1 previous turn", text)
        self.assertIn("exit 1", text)
        self.assertNotIn("Last result per command:", text)
