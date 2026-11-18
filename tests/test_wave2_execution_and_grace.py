"""Regression tests for Wave 2 Slice D: byproduct/linked-deps filtering and stream-finish grace."""
import hermetic  # noqa: F401
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

from claivelib import acceptance, runtime_limits, status_file


def _init_repo(workspace: Path) -> None:
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    (workspace / "owned.txt").write_text("baseline\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(workspace), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(workspace),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "baseline",
        ],
        check=True,
    )


class NoiseFilteringTests(unittest.TestCase):
    def test_is_noise_path_matches_byproducts_dirs_locks_and_linked_deps(self):
        linked = {"vendor/shared", "apps/web/custom_deps"}
        noise_cases = [
            "",
            "   ",
            ".",
            "__pycache__/mod.cpython-314.pyc",
            "pkg/sub/__pycache__/mod.py",
            "pkg/mod.pyc",
            "pkg/mod.pyo",
            ".pytest_cache/v/cache/lastfailed",
            ".mypy_cache/3.14/mod.meta.json",
            ".ruff_cache/0.1.0/state",
            ".vitest/results.json",
            ".turbo/cache.json",
            "node_modules/pkg/index.js",
            "packages/ui/node_modules/react/index.js",
            ".venv/bin/python",
            "services/api/venv/lib/site.py",
            ".git/index",
            "launch.lock",
            "nested/launch.lock",
            "vendor/shared",
            "vendor/shared/lib.so",
            "apps/web/custom_deps/pkg/index.js",
        ]
        for rel in noise_cases:
            with self.subTest(rel=rel):
                self.assertTrue(acceptance.is_noise_path(rel, linked))

        kept_cases = [
            "owned.txt",
            "src/app.py",
            "other.txt",
            "build/artifact",
            "vendor/shared_other/lib.so",
            "apps/web/custom_deps_2/index.js",
        ]
        for rel in kept_cases:
            with self.subTest(rel=rel):
                self.assertFalse(acceptance.is_noise_path(rel, linked))

    def test_linked_dep_prefixes_checks_job_dir_and_worktree_parent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            job_dir = root / "job"
            job_dir.mkdir()
            (job_dir / "linked-deps.json").write_text(
                json.dumps({"linked": ["node_modules", "/packages/ui/deps/"]}),
                encoding="utf-8",
            )

            wt = root / "iso" / "worktree"
            wt.mkdir(parents=True)
            (wt.parent / "linked-deps.json").write_text(
                json.dumps({"linked": ["custom_linked"]}),
                encoding="utf-8",
            )

            non_wt = root / "regular_repo"
            non_wt.mkdir()
            # Parent of non-worktree workspace must NOT be consulted for linked-deps.json
            (root / "linked-deps.json").write_text(
                json.dumps({"linked": ["should_not_apply"]}),
                encoding="utf-8",
            )

            filtered_wt = acceptance.filter_noise_paths(
                ["packages/ui/deps/a.js", "custom_linked/x.py", "should_not_apply/a.py", "src/ok.py"],
                workspace=wt,
                job_dir=job_dir,
            )
            self.assertEqual(filtered_wt, ["should_not_apply/a.py", "src/ok.py"])

            filtered_non_wt = acceptance.filter_noise_paths(
                ["should_not_apply/a.py", "src/ok.py"],
                workspace=non_wt,
            )
            self.assertEqual(filtered_non_wt, ["should_not_apply/a.py", "src/ok.py"])


class ExecutionAndStatusFilteringTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "iso" / "worktree"
        self.workspace.mkdir(parents=True)
        _init_repo(self.workspace)
        self.job = self.root / "job"
        self.job.mkdir()

    def test_finish_filters_noise_from_changed_paths_and_ownership_violations(self):
        linked_target = self.root / "external_shared"
        linked_target.mkdir()
        (linked_target / "dep.txt").write_text("shared", encoding="utf-8")
        (self.job / "linked-deps.json").write_text(
            json.dumps({"linked": ["shared_deps"]}),
            encoding="utf-8",
        )

        state = {
            "id": "w1",
            "status": "running",
            "workspace": str(self.workspace),
            "turn": 1,
            "acceptance_contract": {
                "owned_paths": ["owned.txt"],
                "checks": [{"name": "ok", "command": [sys.executable, "-c", "print('ok')"]}],
            },
        }
        acceptance.begin(self.job, state)

        # Mutate owned file + create verifier/runtime byproducts + create linked-deps symlink.
        (self.workspace / "owned.txt").write_text("updated\n", encoding="utf-8")
        (self.workspace / "__pycache__").mkdir()
        (self.workspace / "__pycache__" / "mod.cpython-314.pyc").write_bytes(b"pyc")
        (self.workspace / ".pytest_cache").mkdir()
        (self.workspace / ".pytest_cache" / "README.md").write_text("cache", encoding="utf-8")
        (self.workspace / ".ruff_cache").mkdir()
        (self.workspace / ".ruff_cache" / "state.json").write_text("{}", encoding="utf-8")
        (self.workspace / ".venv" / "bin").mkdir(parents=True)
        (self.workspace / ".venv" / "bin" / "python").write_text("", encoding="utf-8")
        (self.workspace / "node_modules" / "pkg").mkdir(parents=True)
        (self.workspace / "node_modules" / "pkg" / "index.js").write_text("", encoding="utf-8")
        (self.workspace / "launch.lock").write_text("lock", encoding="utf-8")
        (self.workspace / "shared_deps").symlink_to(linked_target)

        state["status"] = "completed"
        acceptance.finish(self.job, state)

        execution = acceptance.view(self.job, state)["execution"]
        self.assertEqual(execution["changed_paths"], ["owned.txt"])
        self.assertEqual(execution["ownership_violations"], [])

        # Real unauthorized edits still trigger ownership_violations while noise stays filtered.
        (self.workspace / "other.txt").write_text("unowned", encoding="utf-8")
        (self.workspace / "src").mkdir()
        (self.workspace / "src" / "unowned.py").write_text("x = 1\n", encoding="utf-8")
        acceptance.finish(self.job, state)

        execution2 = acceptance.view(self.job, state)["execution"]
        self.assertEqual(execution2["changed_paths"], ["other.txt", "owned.txt", "src/unowned.py"])
        self.assertEqual(execution2["ownership_violations"], ["other.txt", "src/unowned.py"])

    def test_status_file_filters_noise_from_report_and_touched_files(self):
        (self.workspace.parent / "linked-deps.json").write_text(
            json.dumps({"linked": ["linked_vendor"]}),
            encoding="utf-8",
        )
        state_from_report = {
            "id": "w2",
            "status": "completed",
            "workspace": str(self.workspace),
            "turn": 1,
            "report": {
                "status": "done",
                "changed_files": [
                    "__pycache__/a.pyc",
                    ".pytest_cache/x",
                    ".venv/lib/x.py",
                    "node_modules/foo/bar.js",
                    "launch.lock",
                    "linked_vendor/pkg.js",
                    "src/real.py",
                ],
            },
        }
        payload_report = status_file.build(self.job, state_from_report, now=1000.0)
        self.assertEqual(payload_report["changed_files"], ["src/real.py"])

        state_from_touched = {
            "id": "w3",
            "status": "running",
            "workspace": str(self.workspace),
            "turn": 1,
            "touched_files": [
                ".ruff_cache/x",
                "sub/launch.lock",
                "linked_vendor",
                "lib/real.py",
            ],
        }
        payload_touched = status_file.build(self.job, state_from_touched, now=1000.0)
        self.assertEqual(payload_touched["changed_files"], ["lib/real.py"])


class StreamFinishGraceTests(unittest.TestCase):
    def test_constants_and_default_compatibility(self):
        self.assertEqual(runtime_limits.STREAM_FINISH_GRACE_SECONDS, 20.0)
        self.assertEqual(runtime_limits.STREAM_ACTIVE_WINDOW_SECONDS, 15.0)
        # Default streaming_final=False keeps exact prior timeout behavior.
        self.assertEqual(runtime_limits.turn_action(100, 1, {}, 100, 30, wrapped=True), "timeout")

    def test_stream_finish_grace_extends_budget_only_when_actively_streaming_final(self):
        # Active final stream (no active tools, recent output < 15s) gets 20s grace.
        self.assertIsNone(
            runtime_limits.turn_action(100, 2, {}, 100, 30, grace=0, wrapped=True, streaming_final=True)
        )
        self.assertIsNone(
            runtime_limits.turn_action(119.9, 14.9, {}, 100, 30, grace=0, wrapped=True, streaming_final=True)
        )
        # Once elapsed reaches budget + STREAM_FINISH_GRACE_SECONDS (120s), timeout fires.
        self.assertEqual(
            runtime_limits.turn_action(120.0, 2, {}, 100, 30, grace=0, wrapped=True, streaming_final=True),
            "timeout",
        )
        # Once stream goes inactive >= STREAM_ACTIVE_WINDOW_SECONDS (15s), grace does not apply.
        self.assertEqual(
            runtime_limits.turn_action(105, 15.0, {}, 100, 30, grace=0, wrapped=True, streaming_final=True),
            "timeout",
        )
        # If tools are still active, stream-finish grace does not apply.
        self.assertEqual(
            runtime_limits.turn_action(105, 1, {"t1": "bash"}, 100, 30, grace=0, wrapped=True, streaming_final=True),
            "timeout",
        )
        # Larger explicit grace is preserved via max(grace, STREAM_FINISH_GRACE_SECONDS).
        self.assertIsNone(
            runtime_limits.turn_action(125, 1, {}, 100, 30, grace=30, wrapped=True, streaming_final=True)
        )
        self.assertEqual(
            runtime_limits.turn_action(130, 1, {}, 100, 30, grace=30, wrapped=True, streaming_final=True),
            "timeout",
        )


if __name__ == "__main__":
    unittest.main()
