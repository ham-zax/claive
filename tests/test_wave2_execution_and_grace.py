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

    def test_status_file_caps_changed_files_and_preserves_total(self):
        many_paths = [f"src/module_{i:03d}/file_{i:03d}.py" for i in range(120)]
        original_report = {
            "status": "done",
            "summary": "Large refactor",
            "changed_files": list(many_paths),
        }
        state = {
            "id": "abcdef123456",
            "status": "completed",
            "workspace": str(self.workspace),
            "turn": 1,
            "report": original_report,
        }
        payload = status_file.build(self.job, state, now=1000.0)
        self.assertEqual(payload["changed_files_total"], 120)
        self.assertTrue(payload["changed_files_truncated"])
        self.assertEqual(len(payload["changed_files"]), status_file.MAX_CHANGED_FILES)
        self.assertEqual(len(payload["report"]["changed_files"]), status_file.MAX_CHANGED_FILES)
        self.assertEqual(payload["report"]["changed_files_total"], 120)
        self.assertTrue(payload["report"]["changed_files_truncated"])
        # Must not mutate caller's state["report"] in place.
        self.assertEqual(len(original_report["changed_files"]), 120)

        preview = status_file.format_preview(payload)
        self.assertIn("files=120", preview)
        self.assertIn("+116 more", preview)

    def test_status_file_caps_changed_files_by_byte_budget(self):
        long_paths = [f"deep/{'segment_' * 60}/{i:03d}.py" for i in range(60)]
        state = {
            "id": "123456abcdef",
            "status": "running",
            "workspace": str(self.workspace),
            "turn": 1,
            "touched_files": long_paths,
        }
        payload = status_file.build(self.job, state, now=1000.0)
        encoded_size = len(json.dumps(payload["changed_files"], ensure_ascii=False).encode("utf-8"))
        self.assertLessEqual(encoded_size, status_file.MAX_CHANGED_FILES_BYTES)
        self.assertEqual(payload["changed_files_total"], 60)
        self.assertTrue(payload["changed_files_truncated"])
        self.assertLess(len(payload["changed_files"]), 60)

    def test_dash_and_watch_tui_dispatch(self):
        import os
        import stat
        from unittest import mock
        from claivelib import cli

        fake_bin = self.root / "fake-claive-tui"
        out_file = self.root / "argv.json"
        fake_bin.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            f"open({str(out_file)!r}, 'w').write(json.dumps({{'argv': sys.argv[1:], 'dir': os.environ.get('CLAIVE_DIR')}}))\n",
            encoding="utf-8",
        )
        fake_bin.chmod(fake_bin.stat().st_mode | stat.S_IXUSR)

        with mock.patch.dict(os.environ, {"CLAIVE_TUI_BIN": str(fake_bin), "CLAIVE_DIR": str(self.root)}):
            args = cli.parser().parse_args(["dash", "--compact", "--triage", "--session", "claude-1", "--all", "--actions"])
            self.assertEqual(cli.run_dash(args), 0)
            recorded = json.loads(out_file.read_text(encoding="utf-8"))
            self.assertEqual(recorded["dir"], str(self.root))
            self.assertEqual(recorded["argv"], ["--compact", "--triage", "--all", "--actions", "--session", "claude-1"])

            args_ro = cli.parser().parse_args(["dash", "--actions", "--read-only"])
            self.assertEqual(cli.run_dash(args_ro), 0)
            recorded_ro = json.loads(out_file.read_text(encoding="utf-8"))
            self.assertEqual(recorded_ro["argv"], ["--read-only"])

            args_tui = cli.parser().parse_args(["watch", "--tui", "--focus", "abcdef123456"])
            self.assertEqual(cli.run_dash(args_tui), 0)
            recorded2 = json.loads(out_file.read_text(encoding="utf-8"))
            self.assertEqual(recorded2["argv"], ["--focus", "abcdef123456"])

        with mock.patch.dict(os.environ, {"CLAIVE_TUI_BIN": str(self.root / "missing-bin")}):
            with self.assertRaises(ValueError):
                cli.find_tui_binary()

    def test_answer_expected_turn_question_hash_and_duplicate_guard(self):
        import hashlib
        import io
        import os
        from unittest import mock
        from claivelib import cli

        wid = "abcd12345678"
        jdir = self.root / wid
        (jdir / "requests").mkdir(parents=True)
        question = "Should fallback skip oversized docs?"
        qhash = hashlib.sha256(question.encode("utf-8")).hexdigest()[:16]
        state = {
            "schema_version": 2,
            "id": wid,
            "reusable": True,
            "status": "idle",
            "engine": "pi",
            "model": "deepseek-v4.1-flash-free",
            "reasoning_effort": "high",
            "session_id": "sess-1",
            "workspace": str(self.workspace),
            "turn": 2,
            "supervisor_pid": os.getpid(),
            "supervisor_identity": cli.identity(os.getpid()),
            "needs_parent": {"kind": "needs_decision", "question": question},
            "launch": {
                "engine": "pi",
                "binary": "/bin/true",
                "provider": None,
                "model": "deepseek-v4.1-flash-free",
                "workspace": str(self.workspace),
                "read_only": True,
                "web": False,
            },
        }
        (jdir / "state.json").write_text(json.dumps(state), encoding="utf-8")
        (jdir / "policy.json").write_text(
            json.dumps({"reasoning_effort": None, "max_model_steps": None}),
            encoding="utf-8",
        )

        with mock.patch.dict(os.environ, {"CLAIVE_DIR": str(self.root)}), \
             mock.patch.object(cli, "validate_turn"):
            # Wrong turn rejected.
            with mock.patch("sys.argv", ["claive", "answer", wid, "--message", "yes", "--expected-turn", "1"]):
                with self.assertRaisesRegex(ValueError, "turn changed"):
                    cli.main()
            # Wrong question hash rejected.
            with mock.patch("sys.argv", ["claive", "answer", wid, "--message", "yes", "--expected-question-hash", "00000000deadbeef"]):
                with self.assertRaisesRegex(ValueError, "question changed"):
                    cli.main()
            # Matching turn + hash succeeds once.
            buf = io.StringIO()
            with mock.patch("sys.argv", [
                "claive", "answer", wid, "--message", "yes",
                "--expected-turn", "2", "--expected-question-hash", qhash,
            ]), mock.patch("sys.stdout", buf):
                cli.main()
            self.assertIn("Answer queued", buf.getvalue())
            # Second answer for the same turn is rejected by duplicate guard.
            with mock.patch("sys.argv", ["claive", "answer", wid, "--message", "again"]):
                with self.assertRaisesRegex(ValueError, "already queued for turn 2"):
                    cli.main()

    def test_apply_dry_run_reports_gate_blockers(self):
        import io
        import os
        import subprocess
        from unittest import mock
        from claivelib import cli

        repo = self.root / "gitrepo"
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
        (repo / "a.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "a.txt"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True)
        base_commit = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()

        wt = self.root / "wt"
        subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "-b", "claive/wt1", str(wt)], check=True)
        (wt / "a.txt").write_text("edited\n", encoding="utf-8")

        wid = "fedcba987654"
        jdir = self.root / wid
        (jdir / "requests").mkdir(parents=True)
        state = {
            "schema_version": 2,
            "id": wid,
            "status": "completed",
            "workspace": str(repo),
            "retained_worktree": str(wt),
            "turn": 1,
            "acceptance_contract": {"checks": [{"name": "unit", "command": ["true"]}]},
            "launch": {"isolation": {"owner": "claive", "source": str(repo), "base_commit": base_commit}},
        }
        (jdir / "state.json").write_text(json.dumps(state), encoding="utf-8")

        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"CLAIVE_DIR": str(self.root)}), mock.patch("sys.stdout", buf):
            rc = cli.worktree_apply_command(wid, dry_run=True, json_output=True, enforce_gates=True)
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["applied"], ["a.txt"])
        self.assertTrue(payload["gate_blockers"], "dry-run must report verification gate blocker")
        self.assertIn("verification status", payload["gate_blockers"][0])


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
