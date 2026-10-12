"""Independent acceptance and ownership regression cases."""
import hermetic  # noqa: F401
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
from claivelib import acceptance as a, cli
from claivelib.usage import recent_usage


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "repo"
        self.workspace.mkdir()
        self.job = self.root / "job"
        subprocess.run(["git", "init", "-q", str(self.workspace)], check=True)
        (self.workspace / "owned.txt").write_text("baseline")
        subprocess.run(["git", "-C", str(self.workspace), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.workspace), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "baseline"], check=True)
        self.state = dict(status="running", workspace=str(self.workspace), turn=1,
                          acceptance_contract={"owned_paths": ["owned.txt"], "checks": [
                              dict(name="required", command=[sys.executable, "-c", "print('verified')"])]})
        a.begin(self.job, self.state)

    def finish(self):
        self.state["status"] = "completed"
        a.finish(self.job, self.state)

    def test_completed_does_not_mean_accepted(self):
        self.finish()
        self.assertEqual(a.view(self.job, self.state)["acceptance"]["status"], "pending")
        pending = a.view(self.job, self.state)["verification"]["checks"]
        self.assertEqual([(item["name"], item["status"]) for item in pending], [("required", "not_run")])
        with self.assertRaisesRegex(ValueError, "not_run"):
            a.decide(self.job, self.state)
        a.verify(self.job, self.state)
        self.assertEqual(a.decide(self.job, self.state)["status"], "accepted")

    def test_missing_analyzer_blocks_until_explicit_exception(self):
        self.state["acceptance_contract"]["checks"][0]["command"] = ["/nonexistent/analyzer"]
        self.finish()
        self.assertEqual(a.verify(self.job, self.state)["status"], "blocked")
        with self.assertRaisesRegex(ValueError, "blocked"):
            a.decide(self.job, self.state)
        self.assertEqual(a.decide(self.job, self.state, exception="Approved alternate environment check")["status"], "accepted")

    def test_failed_required_check_cannot_be_excepted(self):
        self.finish()
        a.verify(self.job, self.state, "required", "failed", "exit 1", "test host")
        with self.assertRaisesRegex(ValueError, "failed"):
            a.decide(self.job, self.state, exception="override")

    def test_untracked_file_requires_explicit_scope_change(self):
        (self.workspace / "MANIFEST.in").write_text("outside assignment")
        self.finish()
        a.verify(self.job, self.state)
        with self.assertRaisesRegex(ValueError, "ownership"):
            a.decide(self.job, self.state)
        a.scope(self.job, self.state, ["owned.txt", "MANIFEST.in"], "Packaging requires manifest")
        self.assertEqual(a.decide(self.job, self.state)["status"], "accepted")

    def test_dirty_baseline_deletion_and_same_status_edit_detected(self):
        (self.workspace / "owned.txt").write_text("dirty before")
        a.begin(self.job, self.state)
        (self.workspace / "owned.txt").write_text("dirty after")
        (self.workspace / "new.txt").write_text("new")
        self.finish()
        self.assertEqual(a.view(self.job, self.state)["execution"]["changed_paths"], ["new.txt", "owned.txt"])
        (self.workspace / "new.txt").unlink()
        a.begin(self.job, self.state)
        (self.workspace / "owned.txt").unlink()
        self.finish()
        self.assertEqual(a.view(self.job, self.state)["execution"]["changed_paths"], ["owned.txt"])

    def test_later_workspace_edits_invalidate_acceptance(self):
        self.finish()
        a.verify(self.job, self.state)
        (self.workspace / "owned.txt").write_text("after verification")
        with self.assertRaisesRegex(ValueError, "stale"):
            a.decide(self.job, self.state)

    def test_verifier_build_artifacts_stay_in_scratch(self):
        self.state["acceptance_contract"]["checks"][0]["command"] = [sys.executable, "-c", "from pathlib import Path; Path('build').mkdir(); Path('build/artifact').write_text('x')"]
        self.finish()
        self.assertEqual(a.verify(self.job, self.state)["status"], "passed")
        self.assertFalse((self.workspace / "build").exists())

    def test_new_turn_starts_pending_and_unverified(self):
        self.finish()
        a.verify(self.job, self.state)
        a.decide(self.job, self.state)
        self.state.update(turn=2, status="running")
        a.begin(self.job, self.state)
        self.assertEqual(a.view(self.job, self.state)["acceptance"]["status"], "pending")
        self.assertEqual(a.view(self.job, self.state)["verification"]["status"], "not_run")

    def test_external_evidence_requires_reason_and_environment(self):
        self.finish()
        with self.assertRaisesRegex(ValueError, "environment"):
            a.verify(self.job, self.state, "required", "passed")

    def test_phase_clears_and_warnings_retain_turn_details(self):
        state = {"steps": 0, "task_failures": 3, "turn": 2}
        cli.event_update(state, {"type": "tool_started", "tool": "bash"})
        cli.event_update(state, {"type": "tool_completed", "native_kind": "tool_end"})
        self.assertEqual(state["phase"], "waiting for model")
        cli.event_update(state, {"type": "task_warning", "tool": "bash", "command": "analyzer", "reason": "missing"})
        self.assertEqual(state["turn_task_failures"], 1)
        self.assertEqual(state["task_failures"], 4)
        self.assertEqual(state["turn_failure_details"][0]["turn"], 2)

    def test_usage_growth_uses_recent_calls(self):
        result = recent_usage([{"input_tokens": 100}, {"input_tokens": 125, "cached_tokens": 100}])
        self.assertEqual(result["input_tokens"], 125)
        self.assertEqual(result["growth_tokens"], 25)
        self.assertEqual(result["cache_percent"], "80.0%")
        self.assertEqual(recent_usage([{}])["trend"], "unknown")

    def test_ignored_build_output_is_still_an_ownership_violation(self):
        (self.workspace / ".gitignore").write_text("build/\n")
        a.begin(self.job, self.state)
        (self.workspace / "build").mkdir()
        (self.workspace / "build/artifact").write_text("generated")
        self.finish()
        self.assertIn("build/artifact", a.view(self.job, self.state)["execution"]["ownership_violations"])

    def test_unavailable_ownership_scan_preserves_completion_but_blocks_acceptance(self):
        import shutil
        shutil.rmtree(self.workspace / ".git")
        self.finish()
        execution = a.view(self.job, self.state)["execution"]
        self.assertEqual(execution["execution"], "completed")
        self.assertEqual(execution["ownership_validation"], "blocked")
        with self.assertRaisesRegex(ValueError, "validation blocked"):
            a.decide(self.job, self.state, exception="ignore")

    def test_optional_failure_stays_visible_without_becoming_mandatory(self):
        self.state["acceptance_contract"]["checks"][0]["required"] = False
        self.finish()
        a.verify(self.job, self.state, "required", "failed", "optional check failed", "test host")
        self.assertEqual(a.view(self.job, self.state)["verification"]["status"], "failed")
        self.assertEqual(a.decide(self.job, self.state)["status"], "accepted")

    def test_concurrent_external_checks_preserve_both_records(self):
        from concurrent.futures import ThreadPoolExecutor
        self.state["acceptance_contract"]["checks"].append(dict(name="second", command=["unused"]))
        self.finish()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(a.verify, self.job, self.state, name, "passed", "independent host check", "test host")
                       for name in ("required", "second")]
            for future in futures:
                future.result(timeout=5)
        self.assertEqual({item["name"] for item in a.view(self.job, self.state)["verification"]["checks"]}, {"required", "second"})
        self.assertEqual(a.decide(self.job, self.state)["status"], "accepted")

    def test_blank_exception_cannot_accept_unrun_check(self):
        self.finish()
        with self.assertRaisesRegex(ValueError, "nonblank"):
            a.decide(self.job, self.state, exception=" ")

    def test_timeout_terminates_verifier_descendants(self):
        import os
        import time
        pidfile = self.root / "child.pid"
        command = ("import subprocess,sys,time; from pathlib import Path; "
                   "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
                   f"Path({str(pidfile)!r}).write_text(str(child.pid)); time.sleep(60)")
        check = self.state["acceptance_contract"]["checks"][0]
        check.update(command=[sys.executable, "-c", command], timeout=2)
        self.finish()
        self.assertEqual(a.verify(self.job, self.state)["status"], "blocked")
        pid = int(pidfile.read_text())
        deadline = time.monotonic() + 2
        while cli.identity(pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        if cli.identity(pid):
            os.kill(pid, 9)
            self.fail("verifier descendant survived timeout")

    def test_ownership_traversal_rejected(self):
        with self.assertRaises(ValueError):
            a.owned_paths(["../escape"])

class AcceptanceCliTests(unittest.TestCase):
    def test_launch_verify_accept_and_turn_warning_reset(self):
        import os
        import re
        import time
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()
            subprocess.run(["git", "init", "-q", str(workspace)], check=True)
            (workspace / "tracked").write_text("unchanged")
            subprocess.run(["git", "-C", str(workspace), "add", "."], check=True)
            subprocess.run(["git", "-C", str(workspace), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "baseline"], check=True)
            prompt = root / "prompt.md"
            prompt.write_text("warn")
            fake = root / "fake-muse"
            fake.write_text('''#!/usr/bin/python3
import json,sys
from pathlib import Path
prompt=Path(sys.argv[sys.argv.index('--prompt-file')+1]).read_text()
def emit(kind, **data):
 print(json.dumps(dict(payload_type=kind,payload=data)),flush=True)
if prompt == 'warn':
 emit('task.lifecycle.failed',event=dict(reason='analyzer unavailable'))
emit('run.terminal.completed',terminal='completed',text='done')
''')
            fake.chmod(0o755)
            checks = root / "checks.json"
            checks.write_text(json.dumps([dict(name="test", command=[sys.executable, "-c", "print('OK')"])]))
            env = dict(os.environ, CLAIVE_DIR=str(root / "state"), MUSE_WORKER_BINARY=str(fake))
            launcher = str(Path(__file__).resolve().parents[1] / "bin" / "claive")
            def run(*args):
                return subprocess.run([launcher, *args], env=env, capture_output=True, text=True, timeout=15)
            started = run("open", "--detach", "--workspace", str(workspace), "--prompt-file", str(prompt), "--checks-file", str(checks), "--owned-path", "tracked")
            self.assertEqual(started.returncode, 0, started.stderr)
            identifier = re.search(r"Worker ([a-f0-9]{12})", started.stdout)[1]
            try:
                self.assertEqual(run("wait", identifier, "--timeout", "5").returncode, 0)
                state = json.loads(run("show", identifier, "--json").stdout)
                self.assertEqual(state["turn_task_failures"], 1)
                self.assertEqual(state["evidence"]["acceptance"]["status"], "pending")
                self.assertNotEqual(run("accept", identifier).returncode, 0)
                verified = run("verify", identifier)
                self.assertEqual(verified.returncode, 0, verified.stderr)
                self.assertEqual(run("accept", identifier).returncode, 0)
                prompt.write_text("clean")
                self.assertEqual(run("followup", identifier, "--prompt-file", str(prompt)).returncode, 0)
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    state = json.loads(run("show", identifier, "--json").stdout)
                    if state["turn"] == 2 and state["status"] == "idle":
                        break
                    time.sleep(0.03)
                self.assertEqual(state["turn"], 2)
                self.assertEqual(state["turn_task_failures"], 0)
                self.assertEqual(state["previous_turn_task_failures"], 1)
                self.assertEqual(state["task_failures"], 1)
                self.assertEqual(state["evidence"]["acceptance"]["status"], "pending")
                self.assertEqual(state["evidence"]["verification"]["status"], "not_run")
            finally:
                run("close", identifier)
                run("wait", identifier, "--timeout", "5")
