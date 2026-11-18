"""Regression tests for orchestrator friction fixes (Slice C)."""
import hermetic  # noqa: F401
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

from claivelib import acceptance as a, aliases, cli
from claivelib.engine import TurnRequest, WorkerEngine, link_workspace_deps
from claivelib.engines.agy import AGY_MODEL, AgyEngine, resolve_agy_model


def _git(workspace, *args):
    subprocess.run(
        ["git", "-C", str(workspace), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo(workspace):
    workspace.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    (workspace / "owned.txt").write_text("baseline\n", encoding="utf-8")
    _git(workspace, "add", ".")
    _git(workspace, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "baseline")


class ManifestNormalizationTests(unittest.TestCase):
    def test_parse_inline_check_command(self):
        self.assertEqual(a.parse_inline_check_command("pytest -q tests/test_a.py"), ["pytest", "-q", "tests/test_a.py"])
        self.assertEqual(a.parse_inline_check_command("cd web && vitest run"), ["bash", "-lc", "cd web && vitest run"])
        self.assertEqual(a.parse_inline_check_command("cd web"), ["bash", "-lc", "cd web"])
        self.assertEqual(a.parse_inline_check_command("echo ok | cat"), ["bash", "-lc", "echo ok | cat"])
        for bad in ("", "   ", "''"):
            with self.assertRaises(ValueError):
                a.parse_inline_check_command(bad)

    def test_validate_manifest_list_and_object_forms(self):
        list_form = a.validate_manifest([
            {"name": "unit", "command": ["pytest", "-q"]},
            {"name": "web", "shell": "cd web && npm test"},
            {"name": "lint", "command": "ruff check ."},
            {"name": "chain", "command": "cd api && pytest -q"},
        ])
        self.assertEqual([c["command"] for c in list_form], [
            ["pytest", "-q"],
            ["bash", "-lc", "cd web && npm test"],
            ["ruff", "check", "."],
            ["bash", "-lc", "cd api && pytest -q"],
        ])
        self.assertNotIn("shell", list_form[1])

        obj_form = a.validate_manifest({
            "checks": [{"name": "web", "shell": "cd web && vitest run"}]
        })
        self.assertEqual(obj_form, [{"name": "web", "command": ["bash", "-lc", "cd web && vitest run"]}])

    def test_validate_manifest_rejections_include_example(self):
        for bad in (
            "not-a-list",
            {"checks": [], "extra": 1},
            {},
            {"checks": "not-a-list"},
            [{"name": "both", "command": ["pytest"], "shell": "pytest"}],
            [{"name": "empty_cmd", "command": ""}],
            [{"name": "empty_shell", "shell": "   "}],
            [{"name": "missing_cmd"}],
        ):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "Example:"):
                a.validate_manifest(bad)


class VerifyInWorktreeGitignoredTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "job" / "worktree"
        _init_repo(self.workspace)
        (self.workspace / ".gitignore").write_text("web/dist/\n.vite/\n", encoding="utf-8")
        _git(self.workspace, "add", ".gitignore")
        _git(self.workspace, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "gitignore")

    def test_verify_in_worktree_ignores_new_gitignored_build_artifacts(self):
        job = self.root / "job"
        build_code = (
            "from pathlib import Path; "
            "Path('web/dist').mkdir(parents=True, exist_ok=True); "
            "Path('web/dist/app.js').write_text('console.log(1);'); "
            "Path('.vite/deps').mkdir(parents=True, exist_ok=True); "
            "Path('.vite/deps/_metadata.json').write_text('{}')"
        )
        state = {
            "status": "running",
            "workspace": str(self.workspace),
            "turn": 1,
            "acceptance_contract": {
                "owned_paths": ["owned.txt"],
                "verify_in_worktree": True,
                "checks": [
                    {"name": "build", "command": [sys.executable, "-c", build_code]},
                    {"name": "follow", "command": [sys.executable, "-c", "from pathlib import Path; assert Path('web/dist/app.js').is_file()"]},
                ],
            },
        }
        a.begin(job, state)
        (self.workspace / "owned.txt").write_text("updated\n", encoding="utf-8")
        state["status"] = "completed"
        a.finish(job, state)

        ev1 = a.verify(job, state)
        self.assertEqual(ev1["status"], "passed", ev1)
        self.assertEqual(a.check_freshness(job, state)["status"], "fresh")
        # A second verify call and decide() must both succeed without stale errors.
        ev2 = a.verify(job, state)
        self.assertEqual(ev2["status"], "passed", ev2)
        self.assertEqual(a.decide(job, state)["status"], "accepted")

    def test_verify_in_worktree_still_rejects_non_ignored_mutations(self):
        job = self.root / "job_bad"
        mut_code = "from pathlib import Path; Path('untracked.txt').write_text('oops')"
        state = {
            "status": "running",
            "workspace": str(self.workspace),
            "turn": 1,
            "acceptance_contract": {
                "owned_paths": ["owned.txt"],
                "checks": [{"name": "mutator", "command": [sys.executable, "-c", mut_code]}],
            },
        }
        a.begin(job, state)
        state["status"] = "completed"
        a.finish(job, state)

        ev = a.verify(job, state, in_worktree=True)
        self.assertEqual(ev["status"], "failed")
        self.assertIn("check mutated workspace files", ev["checks"][0]["reason"])
        self.assertIn("untracked.txt", ev["checks"][0]["mutated_files"])

    def test_gitignored_paths_includes_linked_deps_manifest(self):
        (self.workspace.parent / "linked-deps.json").write_text(
            json.dumps({"linked": ["node_modules", "packages/web/node_modules"]}),
            encoding="utf-8",
        )
        ignored = a.gitignored_paths(self.workspace)
        self.assertIn("node_modules", ignored)
        self.assertIn("packages/web/node_modules", ignored)


class LinkWorkspaceDepsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "repo"
        _init_repo(self.workspace)
        # Tracked directory that must NOT be overwritten by link_workspace_deps
        (self.workspace / "tracked" / "node_modules").mkdir(parents=True)
        (self.workspace / "tracked" / "node_modules" / "keep.txt").write_text("tracked\n", encoding="utf-8")
        _git(self.workspace, "add", "tracked/node_modules/keep.txt")
        _git(self.workspace, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "tracked")

        # Untracked dependency directories at root, child, and monorepo grandchild
        (self.workspace / ".venv" / "bin").mkdir(parents=True)
        (self.workspace / "node_modules" / "pkg" / "node_modules").mkdir(parents=True)
        (self.workspace / "web" / "node_modules").mkdir(parents=True)
        (self.workspace / "packages" / "ui" / "node_modules").mkdir(parents=True)
        (self.workspace / "apps" / "api" / ".venv").mkdir(parents=True)

    def test_prepare_isolation_links_deps_and_records_manifest(self):
        engine = WorkerEngine()
        job_dir = self.root / "jobs" / "abcdef123456"
        job_dir.mkdir(parents=True)
        launch = {"isolation": {"mode": "create", "link_deps": True}, "read_only": False}
        with mock.patch("claivelib.worktrees.record_isolation"):
            result = engine.prepare_isolation(launch, str(self.workspace), str(job_dir))

        wt = Path(result["isolation"]["existing_path"])
        linked = result["isolation"]["linked_deps"]
        self.assertEqual(
            set(linked),
            {".venv", "node_modules", "web/node_modules", "packages/ui/node_modules", "apps/api/.venv"},
        )
        # Must not descend into node_modules or overwrite tracked/node_modules
        self.assertNotIn("node_modules/pkg/node_modules", linked)
        self.assertNotIn("tracked/node_modules", linked)
        self.assertFalse((wt / "tracked" / "node_modules").is_symlink())
        for rel in linked:
            self.assertTrue((wt / rel).is_symlink())
            self.assertEqual((wt / rel).resolve(), (self.workspace / rel).resolve())

        manifest_data = json.loads((job_dir / "linked-deps.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest_data, {"linked": linked})

    def test_link_workspace_deps_does_not_overwrite_existing_target(self):
        target = self.root / "wt"
        (target / "node_modules").mkdir(parents=True)
        job_dir = self.root / "job_no_overwrite"
        job_dir.mkdir()
        linked = link_workspace_deps(self.workspace, target, job_dir)
        self.assertNotIn("node_modules", linked)
        self.assertFalse((target / "node_modules").is_symlink())


class AgyModelFamilyResolutionTests(unittest.TestCase):
    def setUp(self):
        self.engine = AgyEngine()

    def test_resolve_agy_model_family_stems_and_efforts(self):
        self.assertEqual(resolve_agy_model("gemini-4-hydrogen", "low"), "gemini-4-hydrogen-low")
        self.assertEqual(resolve_agy_model("gemini-4-hydrogen", "xhigh"), "gemini-4-hydrogen-xhigh")
        self.assertEqual(resolve_agy_model("gemini-4-hydrogen", None), "gemini-4-hydrogen-high")
        self.assertEqual(resolve_agy_model(None, "low"), "gemini-3.8-flash-low")
        self.assertEqual(resolve_agy_model(None, None), AGY_MODEL)
        self.assertEqual(resolve_agy_model("gemini-4-hydrogen-high", "low"), "gemini-4-hydrogen-high")
        with self.assertRaisesRegex(ValueError, "supports efforts high, medium, low, not xhigh"):
            resolve_agy_model("gemini-3.8-flash", "xhigh")

    def test_resolve_launch_and_validate_turn_behavior(self):
        launch_low = self.engine.resolve_launch(model="gemini-4-hydrogen", reasoning_effort="low")
        self.assertEqual(launch_low["model"], "gemini-4-hydrogen-low")
        self.assertEqual(self.engine.default_effort_for(launch_low["model"]), "low")

        launch_default = self.engine.resolve_launch(model="gemini-4-hydrogen")
        self.assertEqual(launch_default["model"], "gemini-4-hydrogen-high")
        self.assertEqual(self.engine.default_effort_for("gemini-4-hydrogen"), "high")

        with tempfile.TemporaryDirectory() as tmp:
            prompt = Path(tmp) / "prompt.md"
            prompt.write_text("hi", encoding="utf-8")
            req_mismatch = TurnRequest(
                binary="/bin/true",
                workspace=tmp,
                prompt_file=str(prompt),
                session_id=None,
                provider="antigravity",
                model="gemini-4-hydrogen-high",
                reasoning_effort="low",
                max_model_steps=None,
                read_only=False,
                web=False,
                output_schema=None,
                session_logging=True,
                isolation={},
            )
            with self.assertRaisesRegex(ValueError, "AGY model gemini-4-hydrogen-high requires effort high, not low"):
                self.engine.validate_turn(req_mismatch)

            req_stem = TurnRequest(
                binary="/bin/true",
                workspace=tmp,
                prompt_file=str(prompt),
                session_id=None,
                provider="antigravity",
                model="gemini-3.8-flash",
                reasoning_effort="high",
                max_model_steps=None,
                read_only=False,
                web=False,
                output_schema=None,
                session_logging=True,
                isolation={},
            )
            with self.assertRaisesRegex(ValueError, "unsupported AGY model"):
                self.engine.validate_turn(req_stem)


class AliasReuseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state_dir = Path(self.temp.name) / "state"
        self.state_dir.mkdir()
        self.env_patch = mock.patch.dict(os.environ, {"CLAIVE_DIR": str(self.state_dir)})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def _make_worker(self, worker_id, state):
        job_dir = self.state_dir / worker_id
        job_dir.mkdir(parents=True, exist_ok=True)
        cli.save(job_dir / "state.json", dict(id=worker_id, label="w", workspace=str(self.state_dir), **state))
        return job_dir

    def test_reuse_completed_policy(self):
        w_done = "111111111111"
        w_new = "222222222222"
        w_active = "333333333333"
        w_ask = "444444444444"
        w_wt = "555555555555"
        w_missing = "666666666666"

        self._make_worker(w_done, {"status": "completed"})
        self._make_worker(w_new, {"status": "completed"})
        self._make_worker(w_active, {
            "status": "running",
            "supervisor_pid": os.getpid(),
            "supervisor_identity": cli.identity(os.getpid()),
        })
        self._make_worker(w_ask, {"status": "completed", "needs_parent": {"kind": "needs_decision", "question": "which?"}})
        wt_path = Path(self.temp.name) / "retained-wt"
        wt_path.mkdir()
        self._make_worker(w_wt, {
            "status": "completed",
            "retained_worktree": str(wt_path),
            "evidence": {"acceptance": {"status": "pending"}},
        })
        (self.state_dir / w_missing).mkdir()

        out = io.StringIO()
        # Bind @w to completed worker, default update without replace/reuse_completed still refuses
        aliases.update("w", w_done, output=out)
        with self.assertRaisesRegex(ValueError, "use --replace to rebind it"):
            aliases.update("w", w_new, output=out)

        # With reuse_completed=True, completed worker is rebound cleanly
        aliases.update("w", w_new, reuse_completed=True, output=out)
        self.assertEqual(aliases.resolve("@w"), w_new)

        # Missing state.json (deleted after alias was bound) is reusable
        self._make_worker(w_missing, {"status": "completed"})
        aliases.update("stale", w_missing, output=out)
        (self.state_dir / w_missing / "state.json").unlink()
        self.assertEqual(aliases.can_reuse_worker(w_missing), (True, None))
        aliases.update("stale", w_new, reuse_completed=True, output=out)
        self.assertEqual(aliases.resolve("@stale"), w_new)

        # Active worker cannot be auto-rebound
        aliases.update("active", w_active, output=out)
        ok, reason = aliases.can_reuse_worker(w_active)
        self.assertFalse(ok)
        self.assertIn("still running", reason)
        with self.assertRaisesRegex(ValueError, "cannot be auto-rebound"):
            aliases.update("active", w_new, reuse_completed=True, output=out)

        # Worker with needs_parent cannot be auto-rebound
        aliases.update("ask", w_ask, output=out)
        ok, reason = aliases.can_reuse_worker(w_ask)
        self.assertFalse(ok)
        self.assertIn("needs_parent", reason)
        with self.assertRaisesRegex(ValueError, "cannot be auto-rebound"):
            aliases.update("ask", w_new, reuse_completed=True, output=out)

        # Worker with unaccepted retained_worktree cannot be auto-rebound
        aliases.update("wt", w_wt, output=out)
        ok, reason = aliases.can_reuse_worker(w_wt)
        self.assertFalse(ok)
        self.assertIn("unaccepted worktree", reason)
        with self.assertRaisesRegex(ValueError, "cannot be auto-rebound"):
            aliases.update("wt", w_new, reuse_completed=True, output=out)

        # Once accepted, retained worktree worker can be auto-rebound
        wt_job = self.state_dir / w_wt
        a.atomic(wt_job / "evidence" / "turn-0001" / "acceptance.json", {"status": "accepted"})
        self.assertEqual(aliases.can_reuse_worker(w_wt), (True, None))
        aliases.update("wt", w_new, reuse_completed=True, output=out)
        self.assertEqual(aliases.resolve("@wt"), w_new)


if __name__ == "__main__":
    unittest.main()
