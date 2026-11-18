"""Managed worktrees and shared heavy locks."""
import hermetic  # noqa: F401
import contextlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from test_workers import module
from claivelib import config, memcap
from claivelib.engines import get_engine

ROOT = Path(__file__).resolve().parents[1]
CLI = str(ROOT / "bin/claive")
MEMCAP = str(ROOT / "bin/claive-memcap")


class IsolationChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="claive-isolation-")
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.registry = self.base / "state"
        self.prompt = self.base / "task.md"
        self.prompt.write_text("fixture")
        self.git("init", "-q")
        (self.repo / "tracked").write_text("base")
        self.git("add", "tracked")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "base")
        self.fixture = self.base / "worker"
        self.fixture.write_text("#!/usr/bin/python3\n" + 'import json, os, sys\nfrom pathlib import Path\nPath(os.environ["TEST_CWD"]).write_text(os.getcwd())\nif os.environ.get("TEST_DIRTY"):\n    Path("worker-change").write_text("changed")\nengine = os.environ["TEST_ENGINE"]\nif engine == "pi":\n    os.execv(os.environ["TEST_PI"], [os.environ["TEST_PI"], *sys.argv[1:]])\nif engine == "codex":\n    print(json.dumps({"type": "turn.completed"}), flush=True)\nelif engine == "agy":\n    identifier = "isolation-conversation"\n    print(json.dumps({"event": "init", "conversation_id": identifier, "init": {"cwd": os.getcwd()}}), flush=True)\n    print(json.dumps({"event": "result", "result": {"conversation_id": identifier, "status": "SUCCESS", "response": "ok"}}), flush=True)\nelse:\n    print(json.dumps({"type": "result", "subtype": "success", "result": "ok", "is_error": False}), flush=True)\n')
        self.fixture.chmod(0o755)
        self.env = dict(os.environ, CLAIVE_DIR=str(self.registry), TEST_CWD=str(self.base / "cwd"),
                        TEST_PI=str(ROOT / "tests/fake_pi.py"), CODEX_WORKER_BINARY=str(self.fixture),
                        CLAUDE_WORKER_BINARY=str(self.fixture), PI_WORKER_BINARY=str(self.fixture),
                        AGY_WORKER_BINARY=str(self.fixture))

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True, text=True,
                              capture_output=True).stdout.strip()

    def launch(self, engine, *args, **env):
        result = subprocess.run([CLI, "run", "--engine", engine, "--workspace", str(self.repo),
                                 "--prompt-file", str(self.prompt), *args],
                                env=dict(self.env, TEST_ENGINE=engine, **env),
                                capture_output=True, text=True, timeout=30)
        found = re.search(r"Worker ([0-9a-f]{12})", result.stdout)
        state = json.loads((self.registry / found.group(1) / "state.json").read_text()) if found else None
        return result, state

    def test_all_engines_use_managed_worktree_and_release_or_keep(self):
        for engine in ("codex", "claude", "pi", "agy"):
            for dirty in (False, True):
                with self.subTest(engine=engine, dirty=dirty):
                    result, state = self.launch(engine, "--worktree", **({"TEST_DIRTY": "1"} if dirty else {}))
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    tree = self.registry / state["id"] / "worktree"
                    self.assertEqual((self.base / "cwd").read_text(), str(tree))
                    self.assertEqual(state["actual_workspace"], str(tree))
                    self.assertEqual(tree.exists(), dirty)
                    branches = self.git("branch", "--list", f"claive/{state['id']}")
                    self.assertEqual(bool(branches), dirty)
                    if dirty:
                        self.assertEqual(state["retained_worktree"], str(tree))

    def test_isolation_validation_and_setup_requires_worktree(self):
        with mock.patch.dict(os.environ, {"CODEX_WORKER_BINARY": str(self.fixture),
                                         "CLAUDE_WORKER_BINARY": str(self.fixture),
                                         "PI_WORKER_BINARY": str(self.fixture)}):
            for name in ("codex", "claude", "pi"):
                engine = get_engine(name)
                launch = engine.resolve_launch()
                for isolation in ({"mode": "wrong"}, {"mode": "existing", "existing_path": "relative"},
                                  {"mode": "none", "base": "HEAD"}):
                    with self.subTest(engine=name, isolation=isolation):
                        with self.assertRaises(ValueError):
                            engine.validate_launch(dict(launch, isolation=isolation))
        result, _ = self.launch("codex", "--worktree-setup", "true")
        self.assertIn("--worktree-setup requires --worktree", result.stderr)
        cfg = self.base / "config.json"
        with mock.patch.dict(os.environ, {"CLAIVE_CONFIG": str(cfg)}):
            for value in (1, 3600):
                cfg.write_text(json.dumps({"launch": {"worktree_setup_timeout": value}}))
                self.assertEqual(config.launch_setting("worktree_setup_timeout", 900), value)
            for value in (0, 3601, True):
                cfg.write_text(json.dumps({"launch": {"worktree_setup_timeout": value}}))
                with self.assertRaises(ValueError):
                    config.load()

    def test_setup_runs_in_tree_and_failure_does_not_run_model(self):
        marker = self.base / "setup-cwd"
        result, state = self.launch("codex", "--worktree", "--worktree-setup", f"pwd > {marker}")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(marker.read_text().strip(), state["actual_workspace"])
        self.assertTrue((self.registry / state["id"] / "worktree-setup.log").exists())
        (self.base / "cwd").unlink()
        result, state = self.launch("codex", "--worktree", "--worktree-setup", "echo failed; exit 7")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(state["failure_kind"], "worktree_setup")
        self.assertIn("worktree setup failed (exit 7)", state["error"])
        self.assertFalse((self.base / "cwd").exists())
        config_path = self.base / "timeout.json"
        config_path.write_text(json.dumps({"launch": {"worktree_setup_timeout": 1}}))
        pid_file = self.base / "setup-child-pid"
        result, state = self.launch("codex", "--worktree", "--worktree-setup",
                                    f"sleep 20 & echo $! > {pid_file}; wait",
                                    CLAIVE_CONFIG=str(config_path))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(state["failure_kind"], "worktree_setup")
        self.assertIn("worktree setup failed (exit 124)", state["error"])
        self.assertFalse((self.base / "cwd").exists())
        self.assertIsNone(module.identity(int(pid_file.read_text())))

    def test_followup_uses_worktree_and_setup_runs_once(self):
        for engine in ("codex", "claude", "pi", "agy"):
            with self.subTest(engine=engine):
                marker = self.base / f"setup-{engine}"
                env = dict(self.env, TEST_ENGINE=engine)
                start = subprocess.run([CLI, "open", "--detach", "--engine", engine, "--workspace", str(self.repo),
                                        "--prompt-file", str(self.prompt), "--worktree", "--worktree-setup",
                                        f"echo ran >> {marker}"], env=env, capture_output=True, text=True, timeout=20)
                self.assertEqual(start.returncode, 0, start.stderr)
                job = re.search(r"Worker ([0-9a-f]{12})", start.stdout).group(1)
                state_path = self.registry / job / "state.json"

                def idle(turn):
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        state = json.loads(state_path.read_text())
                        if state["status"] == "idle" and state["turn"] == turn:
                            return state
                        time.sleep(0.05)
                    self.fail("worker did not become idle")

                try:
                    first = idle(1)
                    tree = first["actual_workspace"]
                    self.assertEqual((self.base / "cwd").read_text(), tree)
                    (self.base / "cwd").unlink()
                    follow = subprocess.run([CLI, "followup", job, "--prompt-file", str(self.prompt)],
                                            env=env, capture_output=True, text=True, timeout=10)
                    self.assertEqual(follow.returncode, 0, follow.stderr)
                    idle(2)
                    self.assertEqual((self.base / "cwd").read_text(), tree)
                    self.assertEqual(marker.read_text().splitlines(), ["ran"])
                finally:
                    subprocess.run([CLI, "close", job], env=env, capture_output=True, text=True, timeout=10)
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        if json.loads(state_path.read_text())["status"] not in {"starting", "running", "idle"}:
                            break
                        time.sleep(0.05)

    def test_dirty_warning_counts_paths(self):
        (self.repo / "staged").write_text("staged")
        self.git("add", "staged")
        (self.repo / "tracked").write_text("changed")
        (self.repo / "new").write_text("new")
        result, _ = self.launch("codex", "--worktree")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("lacks 3 uncommitted paths", result.stderr)
        self.assertIn("(staged 1, unstaged 1, untracked 1)", result.stderr)
        self.assertIn("--worktree --carry-dirty", result.stderr)
        self.assertNotIn("git -C", result.stderr)

    def test_dirty_warning_quotes_renames_and_special_filenames(self):
        self.git("mv", "tracked", "renamed name")
        (self.repo / 'quote"name').write_text("new")
        result, _ = self.launch("codex", "--worktree")
        self.assertEqual(result.returncode, 0, result.stderr)
        first_line = result.stderr.splitlines()[0]
        for expected in ("tracked", "renamed name", 'quote"name'):
            self.assertIn(expected, first_line)
        self.assertIn("--worktree --carry-dirty", result.stderr)

    def test_cancel_during_setup_or_lock_wait(self):
        for waiting in (False, True):
            with self.subTest(waiting=waiting):
                marker = self.base / "setup-pid"
                marker.unlink(missing_ok=True)
                if (self.base / "cwd").exists():
                    (self.base / "cwd").unlink()
                with mock.patch.dict(os.environ, {"CLAIVE_DIR": str(self.registry)}):
                    lock = memcap.hold_lock("heavy") if waiting else contextlib.nullcontext()
                    with lock:
                        result = subprocess.run([CLI, "start", "--engine", "codex", "--workspace", str(self.repo),
                                                 "--prompt-file", str(self.prompt), "--worktree", "--worktree-setup",
                                                 f"sleep 30 & echo $! > {marker}; wait"],
                                                env=dict(self.env, TEST_ENGINE="codex"), capture_output=True,
                                                text=True, timeout=15)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        job = re.search(r"Worker ([0-9a-f]{12})", result.stdout).group(1)
                        state_path = self.registry / job / "state.json"
                        child_pid = None
                        try:
                            if not waiting:
                                deadline = time.monotonic() + 5
                                while not marker.exists() and time.monotonic() < deadline:
                                    time.sleep(0.05)
                                self.assertTrue(marker.exists())
                                child_pid = int(marker.read_text())
                            cancelled = subprocess.run([CLI, "cancel", job], env=self.env,
                                                       capture_output=True, text=True, timeout=5)
                            self.assertEqual(cancelled.returncode, 0, cancelled.stderr)
                            deadline = time.monotonic() + 3
                            while time.monotonic() < deadline:
                                state = json.loads(state_path.read_text())
                                if state["status"] == "cancelled":
                                    break
                                time.sleep(0.05)
                            self.assertEqual(state["status"], "cancelled")
                            self.assertFalse((self.base / "cwd").exists())
                            if child_pid:
                                self.assertIsNone(module.identity(child_pid))
                            else:
                                self.assertFalse(marker.exists())
                        finally:
                            state = json.loads(state_path.read_text())
                            pid = state.get("supervisor_pid")
                            if pid and module.identity(pid) == state.get("supervisor_identity"):
                                os.kill(pid, signal.SIGKILL)
                            if child_pid and module.identity(child_pid):
                                os.killpg(os.getpgid(child_pid), signal.SIGKILL)

    def test_memcap_lock_wait_timeout_and_name(self):
        marker = self.base / "acquired"
        holder = subprocess.Popen([MEMCAP, "1G", "--lock", "heavy", "--", "sh", "-c",
                                   f"touch {marker}; sleep 1.5"], env=self.env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 10
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(marker.exists())
            blocked = subprocess.run([MEMCAP, "1G", "--lock", "heavy", "--lock-timeout", "0.2", "--", "true"],
                                     env=self.env, capture_output=True, text=True, timeout=10)
            self.assertEqual(blocked.returncode, 75)
            self.assertIn(f"waiting for lock heavy (held by pid {holder.pid})", blocked.stderr)
            waited = subprocess.run([MEMCAP, "1G", "--lock", "heavy", "--", "true"],
                                    env=self.env, capture_output=True, text=True, timeout=10)
            self.assertEqual(waited.returncode, 0, waited.stderr)
            self.assertIn("waiting for lock heavy", waited.stderr)
        finally:
            holder.communicate(timeout=10)
        bad = subprocess.run([MEMCAP, "1G", "--lock", "../bad", "--", "true"],
                             env=self.env, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("invalid lock name", bad.stderr)

    def test_carry_dirty_untouched_cleanup_and_worktree_apply(self):
        (self.repo / "other_dirty").write_text("parent-v1")
        (self.repo / "tracked").write_text("dirty-tracked")
        (self.repo / "staged_file").write_text("staged-content")
        self.git("add", "staged_file")

        # 1. Untouched --carry-dirty worktree sees dirty files and auto-cleans on exit.
        seen_file = self.base / "seen.json"
        probe_worker = self.base / "probe_worker"
        probe_worker.write_text(
            "#!/usr/bin/python3\n"
            "import json, os\n"
            "from pathlib import Path\n"
            f"Path({str(seen_file)!r}).write_text(json.dumps({{\n"
            "    'tracked': Path('tracked').read_text(),\n"
            "    'other': Path('other_dirty').read_text(),\n"
            "    'staged': Path('staged_file').read_text(),\n"
            "}))\n"
            "if os.environ.get('EDIT_WORKTREE') == '1':\n"
            "    Path('tracked').write_text('worker-v2')\n"
            "    Path('worker_new.txt').write_text('created-by-worker')\n"
            "print(json.dumps({'type': 'turn.completed'}), flush=True)\n",
            encoding="utf-8",
        )
        probe_worker.chmod(0o755)
        env = dict(self.env, CODEX_WORKER_BINARY=str(probe_worker))

        clean_run = subprocess.run(
            [CLI, "run", "--engine", "codex", "--workspace", str(self.repo),
             "--prompt-file", str(self.prompt), "--worktree", "--carry-dirty"],
            env=env, capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(clean_run.returncode, 0, clean_run.stderr)
        self.assertNotIn("lacks", clean_run.stderr)
        seen = json.loads(seen_file.read_text(encoding="utf-8"))
        self.assertEqual(seen, {"tracked": "dirty-tracked", "other": "parent-v1", "staged": "staged-content"})
        clean_id = re.search(r"Worker ([0-9a-f]{12})", clean_run.stdout).group(1)
        clean_state = json.loads(
            subprocess.run([CLI, "show", clean_id, "--json"], env=env, capture_output=True, text=True).stdout
        )
        self.assertIsNone(clean_state.get("retained_worktree"))

        # 2. Writer --carry-dirty edits disjoint paths while parent edits other_dirty -> clean apply.
        edit_run = subprocess.run(
            [CLI, "run", "--engine", "codex", "--workspace", str(self.repo),
             "--prompt-file", str(self.prompt), "--worktree", "--carry-dirty"],
            env=dict(env, EDIT_WORKTREE="1"), capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(edit_run.returncode, 0, edit_run.stderr)
        edit_id = re.search(r"Worker ([0-9a-f]{12})", edit_run.stdout).group(1)

        # Parent modifies an unrelated dirty file while/after worker ran.
        (self.repo / "other_dirty").write_text("parent-v2-concurrent")

        applied = subprocess.run(
            [CLI, "worktree", "apply", edit_id, "--json"],
            env=env, capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(applied.returncode, 0, applied.stderr)
        apply_data = json.loads(applied.stdout)
        self.assertEqual(apply_data["applied"], ["tracked", "worker_new.txt"])
        self.assertEqual(apply_data["conflicts"], [])
        self.assertEqual((self.repo / "tracked").read_text(), "worker-v2")
        self.assertEqual((self.repo / "worker_new.txt").read_text(), "created-by-worker")
        self.assertEqual((self.repo / "other_dirty").read_text(), "parent-v2-concurrent")

        # 3. Divergent parent edit on worker-touched path refuses without --force, succeeds with --force.
        (self.repo / "tracked").write_text("parent-diverged")
        conflict_res = subprocess.run(
            [CLI, "worktree", "apply", edit_id, "--json"],
            env=env, capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(conflict_res.returncode, 1)
        conflict_data = json.loads(conflict_res.stdout)
        self.assertEqual(conflict_data["conflicts"], ["tracked"])
        self.assertEqual((self.repo / "tracked").read_text(), "parent-diverged")

        forced_res = subprocess.run(
            [CLI, "worktree", "apply", edit_id, "--force", "--json"],
            env=env, capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(forced_res.returncode, 0, forced_res.stderr)
        self.assertEqual((self.repo / "tracked").read_text(), "worker-v2")
