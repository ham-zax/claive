"""Logged worktree lifecycle and safe housekeeping."""
import hermetic  # noqa: F401
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock
from types import SimpleNamespace

from test_workers import FIXTURE_CLI, FIXTURE_WORKER, module
from claivelib import worktrees
from claivelib.engine import WorkerEngine

ROOT = Path(__file__).resolve().parents[1]
CLI = str(ROOT / "bin/claive")


class WorktreeHousekeepingChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="claive-trees-")
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.registry = self.base / "state"
        self.env = dict(os.environ, CLAIVE_DIR=str(self.registry))
        self.patched = mock.patch.dict(os.environ, {"CLAIVE_DIR": str(self.registry)})
        self.patched.start()
        self.git("init", "-q")
        (self.repo / "tracked").write_text("base")
        self.git("add", "tracked")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "base")

    def tearDown(self):
        self.patched.stop()
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True,
                              text=True, check=True).stdout.strip()

    def make_tree(self, identifier, status="completed", read_only=False):
        job = self.registry / identifier
        job.mkdir(parents=True)
        with mock.patch.object(worktrees.time, "time", return_value=time.time() - 172800):
            launch = WorkerEngine().prepare_isolation({"read_only": read_only, "isolation": {"mode": "create"}},
                                                      str(self.repo), str(job))
        state = dict(schema_version=module.SCHEMA_VERSION, id=identifier, label="test", status=status, started_at=time.time() - 172800,
                     workspace=str(self.repo), launch=launch)
        if status == "running":
            state.update(supervisor_pid=os.getpid(), supervisor_identity=module.identity(os.getpid()))
        module.save(job / "state.json", state)
        return job, state

    def command(self, *args):
        return subprocess.run([CLI, *args], env=self.env, capture_output=True, text=True, timeout=20)

    def records(self):
        return [json.loads(line) for line in (self.registry / "worktrees.jsonl").read_text().splitlines()]

    def test_opt_in_and_creation_release_kept_and_discard_log(self):
        clean_launch = WorkerEngine().prepare_isolation({"isolation": {"mode": "none"}}, str(self.repo),
                                                        str(self.base / "not-created"))
        self.assertEqual(clean_launch["isolation"]["mode"], "none")
        self.assertFalse((self.registry / "worktrees.jsonl").exists())
        prompt = self.base / "prompt.md"
        prompt.write_text("task")
        launched = subprocess.run([FIXTURE_CLI, "run", "--engine", "fixture", "--workspace", str(self.repo),
                                   "--prompt-file", str(prompt)],
                                  env=dict(self.env, FIXTURE_WORKER_BINARY=FIXTURE_WORKER, FIXTURE_MODE="success"),
                                  capture_output=True, text=True, timeout=15)
        self.assertEqual(launched.returncode, 0, launched.stderr)
        self.assertFalse(list(self.registry.glob("*/worktree")))
        self.assertFalse((self.registry / "worktrees.jsonl").exists())
        job, state = self.make_tree("111111111111")
        tree = job / "worktree"
        self.assertEqual(self.records()[-1]["event"], "created")
        self.assertEqual(self.records()[-1]["branch"], "claive/111111111111")
        module.release_worktree(state)
        self.assertFalse(tree.exists())
        self.assertEqual(self.records()[-1]["event"], "removed")
        job, state = self.make_tree("222222222222")
        (job / "worktree" / "new").write_text("keep")
        module.release_worktree(state)
        self.assertEqual(self.records()[-1]["event"], "kept")
        self.assertIn("changes", self.records()[-1]["reason"])
        job, state = self.make_tree("333333333333", read_only=True)
        self.assertIsNone(self.records()[-1]["branch"])
        module.discard_job(job, state["launch"])
        self.assertFalse(job.exists())
        self.assertEqual(self.records()[-1]["event"], "removed")
        self.assertEqual((self.registry / "worktrees.jsonl").stat().st_mode & 0o777, 0o600)

    def test_list_doctor_clean_and_dry_run(self):
        job, state = self.make_tree("111111111111")
        tree = job / "worktree"
        (tree / "link").symlink_to("tracked")
        listing = self.command("worktree", "list", "--json")
        self.assertEqual(listing.returncode, 0, listing.stderr)
        row = json.loads(listing.stdout)[0]
        self.assertEqual(row["job"], job.name)
        self.assertEqual(row["status"], "completed")
        self.assertGreater(row["age"], 86400)
        self.assertGreater(row["size"], 0)
        self.assertEqual(row["branch"], "claive/111111111111")
        self.assertTrue(row["changed"])
        self.assertEqual(row["source"], str(self.repo))
        (tree / "link").unlink()
        listing = self.command("worktree", "list")
        self.assertIn("111111111111 completed 2d", listing.stdout)
        self.assertIn("changed no", listing.stdout)
        doctor = json.loads(self.command("doctor", "--json").stdout)
        check = next(item for item in doctor["checks"] if item["name"] == "worktrees")
        self.assertTrue(check["ok"])
        self.assertIn("1 worktrees", check["detail"])
        self.assertIn("1 older than 24h", check["detail"])
        before = (self.registry / "worktrees.jsonl").read_text()
        dry = self.command("worktree", "clean", "--dry-run", "--json")
        self.assertEqual(json.loads(dry.stdout)["worktrees"][0]["action"], "would clean")
        self.assertTrue(tree.exists())
        self.assertEqual((self.registry / "worktrees.jsonl").read_text(), before)
        result = self.command("worktree", "clean")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("cleaned 111111111111", result.stdout)
        self.assertFalse(tree.exists())
        self.assertEqual(self.records()[-1]["event"], "cleaned")
        self.assertFalse(self.git("branch", "--list", "claive/111111111111"))
        tree.mkdir()
        (tree / "unlogged").write_text("keep")
        result = self.command("worktree", "clean", "--discard-changed")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((tree / "unlogged").exists())

    def test_changed_and_active_workers_are_kept(self):
        changed, _ = self.make_tree("111111111111")
        (changed / "worktree" / "new").write_text("important")
        active, _ = self.make_tree("222222222222", status="running")
        result = self.command("worktree", "clean", "--discard-changed")
        self.assertIn("kept 222222222222: active", result.stdout)
        self.assertFalse((changed / "worktree").exists())
        self.assertTrue((active / "worktree").exists())
        another, _ = self.make_tree("333333333333")
        (another / "worktree" / "new").write_text("important")
        result = self.command("worktree", "clean")
        self.assertIn("kept 333333333333: changes (use --discard-changed)", result.stdout)
        self.assertTrue((another / "worktree").exists())

    def test_refuses_outside_path_and_unlogged_worktree(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "important").write_text("keep")
        unlogged = self.registry / "222222222222" / "worktree"
        unlogged.mkdir(parents=True)
        with mock.patch.object(worktrees.time, "time", return_value=time.time() - 172800):
            worktrees.record("created", job="111111111111", path=str(outside), branch=None,
                             source=str(self.repo), base_commit=self.git("rev-parse", "HEAD"))
        result = self.command("worktree", "clean", "--discard-changed")
        self.assertIn("kept 111111111111: outside managed layout", result.stdout)
        self.assertTrue((outside / "important").exists())
        self.assertTrue(unlogged.exists())

    def test_missing_source_removal_is_confined_to_state(self):
        job, _ = self.make_tree("111111111111")
        tree = job / "worktree"
        shutil.rmtree(self.repo)
        result = self.command("worktree", "clean", "--discard-changed")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(tree.exists())
        self.assertEqual(self.records()[-1]["event"], "cleaned")

    def test_cleanup_waits_for_acceptance_and_allows_pruning_afterwards(self):
        job, state = self.make_tree("111111111111", status="interrupted")
        state["acceptance_contract"] = {"checks": [{"name": "unit", "command": ["true"]}]}
        with mock.patch.object(worktrees.time, "time", return_value=time.time() - 172800):
            module.release_worktree(state)
        module.save(job / "state.json", state)
        for flags in ((), ("--dry-run",), ("--discard-changed",)):
            result = self.command("worktree", "clean", *flags)
            self.assertIn("kept 111111111111: pending acceptance", result.stdout)
            self.assertTrue((job / "worktree").exists())
        evidence = module.acceptance.directory(job, state)
        module.acceptance.atomic(evidence / "acceptance.json", {"status": "accepted"})
        result = self.command("worktree", "clean")
        self.assertIn("cleaned 111111111111", result.stdout)
        self.assertNotIn("retained_worktree", json.loads((job / "state.json").read_text()))
        result = self.command("prune", "--interrupted")
        self.assertIn("deleted 111111111111", result.stdout)
        self.assertFalse(job.exists())

    def test_read_only_changes_are_retained(self):
        job, state = self.make_tree("111111111111", read_only=True)
        changed = job / "worktree" / "important"
        changed.write_text("keep")
        module.release_worktree(state)
        self.assertTrue(changed.exists())
        self.assertEqual(state["retained_worktree"], str(job / "worktree"))
        self.assertEqual(self.records()[-1]["event"], "kept")

    def test_failed_git_removal_preserves_worktree(self):
        job, state = self.make_tree("111111111111")
        changed = job / "worktree" / "important"
        changed.write_text("keep")
        self.git("worktree", "lock", str(job / "worktree"))
        module.discard_job(job, state["launch"])
        self.assertTrue(changed.exists())
        self.assertEqual(self.records()[-1]["event"], "kept")

    def test_failed_launch_keeps_new_files_without_a_git_lock(self):
        job, state = self.make_tree("111111111111")
        changed = job / "worktree" / "important"
        changed.write_text("keep")
        module.discard_job(job, state["launch"])
        self.assertTrue(changed.exists())
        self.assertEqual(self.records()[-1]["event"], "kept")

    def test_release_does_not_force_away_late_changes(self):
        job, state = self.make_tree("111111111111")
        changed = job / "worktree" / "important"

        def late_change(*_args):
            changed.write_text("keep")
            return None

        with mock.patch.object(module, "_worktree_changes", side_effect=late_change):
            module.release_worktree(state)
        self.assertTrue(changed.exists())
        self.assertEqual(self.records()[-1]["event"], "kept")

    def test_failed_launch_keeps_a_recoverable_job_when_git_refuses(self):
        job, state = self.make_tree("111111111111")
        tree = job / "worktree"
        (job / "state.json").unlink()
        self.git("worktree", "lock", str(tree))
        with mock.patch.object(worktrees.time, "time", return_value=time.time() - 172800):
            module.discard_job(job, state["launch"])
        self.assertEqual(json.loads((job / "state.json").read_text())["status"], "failed")
        self.git("worktree", "unlock", str(tree))
        result = self.command("worktree", "clean", "--discard-changed")
        self.assertIn("cleaned 111111111111", result.stdout)
        self.assertFalse(tree.exists())

    def test_branch_with_new_commits_is_kept_when_head_is_detached(self):
        job, state = self.make_tree("111111111111")
        tree = job / "worktree"
        subprocess.run(["git", "-C", str(tree), "-c", "user.name=Test", "-c",
                        "user.email=test@example.com", "commit", "--allow-empty", "-qm", "new"], check=True)
        subprocess.run(["git", "-C", str(tree), "checkout", "--detach", "-q",
                        state["launch"]["isolation"]["base_commit"]], check=True)
        result = self.command("worktree", "clean")
        self.assertIn("kept 111111111111: changes", result.stdout)
        self.assertTrue(tree.exists())

    def test_cleanup_rechecks_changes_before_removal(self):
        job, _ = self.make_tree("111111111111")
        rows = module.worktree_rows()
        changed = job / "worktree" / "important"
        changed.write_text("keep")
        with mock.patch.object(module, "worktree_rows", return_value=rows), contextlib.redirect_stdout(io.StringIO()) as output:
            module.worktree_command("clean")
        self.assertIn("kept 111111111111: changes", output.getvalue())
        self.assertTrue(changed.exists())

    def test_active_worker_using_another_jobs_worktree_keeps_it(self):
        owner, state = self.make_tree("111111111111")
        tree = owner / "worktree"
        other = self.registry / "222222222222"
        other.mkdir()
        module.save(other / "state.json", dict(schema_version=module.SCHEMA_VERSION,
                    id=other.name, status="idle", started_at=time.time(),
                    supervisor_pid=os.getpid(), supervisor_identity=module.identity(os.getpid()),
                    workspace=str(tree), launch={}))
        with mock.patch.object(worktrees.time, "time", return_value=time.time() - 172800):
            module.release_worktree(state)
        self.assertTrue(tree.exists())
        module.save(owner / "state.json", state)
        result = self.command("worktree", "clean", "--discard-changed")
        self.assertIn("kept 111111111111: in use by worker 222222222222", result.stdout)
        self.assertTrue(tree.exists())

    def test_cleanup_keeps_tree_with_missing_job_state(self):
        job, _ = self.make_tree("111111111111")
        (job / "state.json").unlink()
        result = self.command("worktree", "clean", "--discard-changed")
        self.assertIn("kept 111111111111: missing job state", result.stdout)
        self.assertTrue((job / "worktree").exists())

    def test_reuse_and_removal_share_a_lifecycle_lock(self):
        owner, state = self.make_tree("111111111111")
        tree = owner / "worktree"
        entered, resume, released = threading.Event(), threading.Event(), threading.Event()
        args = SimpleNamespace(workspace=str(tree), worktree=False, worktree_existing=None)

        def create_user(_args):
            entered.set()
            if not resume.wait(5):
                raise RuntimeError("test did not release launch")
            other = self.registry / "222222222222"
            other.mkdir()
            module.save(other / "state.json", dict(schema_version=module.SCHEMA_VERSION, id=other.name,
                        status="starting", started_at=time.time(), workspace=str(tree), launch={}))

        def release():
            module.release_worktree(state)
            released.set()

        with mock.patch.object(module, "_create_job", side_effect=create_user):
            starter = threading.Thread(target=module.create_job, args=(args,))
            starter.start()
            try:
                self.assertTrue(entered.wait(3))
                remover = threading.Thread(target=release)
                remover.start()
                self.assertFalse(released.wait(0.2))
            finally:
                resume.set()
                starter.join(5)
                if 'remover' in locals():
                    remover.join(5)
        self.assertTrue(released.is_set())
        self.assertTrue(tree.exists())

    def test_ignored_dependencies_are_kept_until_explicit_discard(self):
        (self.repo / ".gitignore").write_text("node_modules/\n")
        self.git("add", ".gitignore")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "ignore")
        job, state = self.make_tree("111111111111")
        changed = job / "worktree" / "node_modules" / "custom.js"
        changed.parent.mkdir()
        changed.write_text("user work")
        with mock.patch.object(worktrees.time, "time", return_value=time.time() - 172800):
            module.release_worktree(state)
        module.save(job / "state.json", state)
        self.assertTrue(changed.exists())
        self.assertIn("kept 111111111111: changes", self.command("worktree", "clean").stdout)
        self.assertTrue(changed.exists())
        self.assertIn("cleaned 111111111111", self.command("worktree", "clean", "--discard-changed").stdout)
