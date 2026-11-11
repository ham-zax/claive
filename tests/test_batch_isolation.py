"""Batch lane isolation: one managed worktree per lane, reused across its ordered stages.

The batch owns each lane worktree, so a settled stage must not remove it: the next stage reads
the previous stage's edits and the parent reviews the tree afterwards.
"""
import hermetic  # noqa: F401  (must run before claivelib reads the environment)
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import time
import unittest

from test_workers import FIXTURE_CLI, module

# A worker that records the edits it can see and appends its own, so a following stage proves
# that it reads the previous stage's output from the same worktree.
WORKER = '''#!/usr/bin/python3
import json, os, sys
prompt = open(sys.argv[1]).read().strip() if len(sys.argv) > 1 else ""
path = os.path.join(os.getcwd(), "lane-writes.txt")
seen = open(path).read().strip() if os.path.exists(path) else ""
if not prompt.startswith("READ ONLY"):
    with open(path, "a") as out:
        out.write(prompt + "\\n")
print(json.dumps({"kind": "model"}), flush=True)
print(json.dumps({"kind": "terminal", "status": "completed", "text": "SEEN[" + seen + "]"}), flush=True)
'''


class BatchIsolationChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="claive-batch-isolation-")
        self.path = Path(self.temp.name)
        self.registry = self.path / "registry"
        self.worker = self.path / "writing-worker"
        self.worker.write_text(WORKER)
        self.worker.chmod(0o700)
        self.env = dict(os.environ, CLAIVE_DIR=str(self.registry), TERM="dumb",
                        FIXTURE_WORKER_BINARY=str(self.worker))
        for name in ("CLAIVE_WORKER_ID", "CLAIVE_ALLOW_NESTED", "CLAIVE_MISSION",
                     "CLAIVE_NOTIFY_CMD", "FIXTURE_MODE", "FIXTURE_TEXT_FILE"):
            self.env.pop(name, None)
        self.repo = self.make_repo("repo-one", "seed-one.txt")
        self.repo2 = self.make_repo("repo-two", "seed-two.txt")
        self.plain = self.path / "plain"
        self.plain.mkdir()

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
        self.temp.cleanup()

    def git(self, *args, cwd=None):
        env = dict(self.env, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)
        return subprocess.run(["git", "-C", str(cwd or self.repo), *args], env=env,
                              text=True, capture_output=True, check=True)

    def make_repo(self, name, seed):
        repo = self.path / name
        repo.mkdir()
        self.git("init", "-q", "-b", "main", cwd=repo)
        self.git("config", "user.email", "tests@example.com", cwd=repo)
        self.git("config", "user.name", "Claive Tests", cwd=repo)
        (repo / seed).write_text("seed\n")
        self.git("add", seed, cwd=repo)
        self.git("commit", "-qm", "seed", cwd=repo)
        return repo

    def cli(self, *args, timeout=60, **env):
        return subprocess.run([FIXTURE_CLI, *args], env=dict(self.env, **env), text=True,
                              capture_output=True, timeout=timeout)

    def plan(self, lanes, workspace=None, **extra):
        path = self.path / f"plan-{time.time_ns()}.json"
        extra.setdefault("defaults", {"engine": "fixture"})
        path.write_text(json.dumps(dict(workspace=str(workspace or self.repo), lanes=lanes, **extra)))
        return path

    def stage(self, name, **extra):
        path = self.path / f"{name}.md"
        path.write_text(("READ ONLY " if extra.get("read_only") else "WRITE ") + name.upper())
        return dict(key=name, prompt_file=str(path), **extra)

    def start_batch(self, plan):
        result = self.cli("batch", "start", str(plan))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        found = re.search(r"Batch ([0-9a-f]{12})", result.stdout)
        self.assertIsNotNone(found, result.stdout)
        return found.group(1)

    def batch_state(self, batch):
        return json.loads(self.cli("batch", "status", batch, "--json").stdout)

    def stages(self, batch):
        state = self.batch_state(batch)
        return {f"{lane['key']}.{stage['key']}": stage
                for lane in state["lanes"] for stage in lane["stages"]}

    def workspaces(self, batch, lane_key):
        state = self.batch_state(batch)
        lane = next(lane for lane in state["lanes"] if lane["key"] == lane_key)
        return lane["workspaces"]

    def worker_state(self, batch, stage):
        return json.loads((self.registry / self.stages(batch)[stage]["worker"] / "state.json").read_text())

    def result(self, batch, stage):
        return (self.registry / self.stages(batch)[stage]["worker"] / "result.txt").read_text()

    def run_batch(self, plan, code=0):
        batch = self.start_batch(plan)
        waited = self.cli("batch", "wait", batch, timeout=120)
        self.assertEqual(waited.returncode, code, waited.stdout + waited.stderr)
        return batch

    def runner_log(self, batch):
        return (self.registry / "batches" / batch / "runner.log").read_text()

    # A. default isolation -----------------------------------------------------
    def test_independent_lanes_get_one_worktree_each(self):
        plan = self.plan([
            {"key": "a", "stages": [self.stage("a1"), self.stage("a2")]},
            {"key": "b", "stages": [self.stage("b1")]},
        ])
        batch = self.run_batch(plan)
        trees = {lane: self.workspaces(batch, lane) for lane in ("a", "b")}
        for lane, entries in trees.items():
            self.assertEqual(len(entries), 1, entries)
            entry = next(iter(entries.values()))
            self.assertEqual(entry["mode"], "worktree")
            self.assertEqual(entry["source"], str(self.repo))
            self.assertTrue(Path(entry["path"]).is_dir(), entry)
            self.assertTrue(entry["branch"].startswith("claive/"), entry)
        lane_a = next(iter(trees["a"].values()))
        lane_b = next(iter(trees["b"].values()))
        self.assertNotEqual(lane_a["path"], lane_b["path"], "lanes must not share a worktree")
        self.assertNotEqual(lane_a["branch"], lane_b["branch"])
        # Each lane sees only its own writes, and the plan workspace stays untouched.
        self.assertEqual((Path(lane_a["path"]) / "lane-writes.txt").read_text(), "WRITE A1\nWRITE A2\n")
        self.assertEqual((Path(lane_b["path"]) / "lane-writes.txt").read_text(), "WRITE B1\n")
        self.assertFalse((self.repo / "lane-writes.txt").exists(), "writers must not touch the workspace")
        # The batch owns the worktree: the settled creator stage kept it and the parent can review it.
        creator = self.worker_state(batch, "a.a1")
        self.assertEqual(creator["retained_worktree"], lane_a["path"])
        self.assertEqual(creator["actual_workspace"], lane_a["path"])

    def test_following_stage_reads_the_previous_stage_edits(self):
        plan = self.plan([{"key": "a", "stages": [self.stage("a1"), self.stage("a2")]}])
        batch = self.run_batch(plan)
        first = self.worker_state(batch, "a.a1")
        second = self.worker_state(batch, "a.a2")
        self.assertEqual(second["actual_workspace"], first["actual_workspace"],
                         "the second stage must reuse the lane worktree")
        self.assertEqual(second["launch"]["isolation"]["mode"], "existing")
        self.assertIn("SEEN[WRITE A1]", self.result(batch, "a.a2"))
        self.assertEqual((Path(first["actual_workspace"]) / "lane-writes.txt").read_text(),
                         "WRITE A1\nWRITE A2\n")
        self.assertFalse((self.repo / "lane-writes.txt").exists())

    def test_lane_reuse_respects_source_workspace_allowlist(self):
        config = self.path / "allowlist.json"
        config.write_text(json.dumps({"workspaces": [{"path": str(self.repo), "write": True}]}))
        self.env["CLAIVE_CONFIG"] = str(config)
        plan = self.plan([{"key": "a", "stages": [self.stage("a1"), self.stage("a2")]}])
        batch = self.run_batch(plan)
        self.assertIn("SEEN[WRITE A1]", self.result(batch, "a.a2"))

    def test_worktree_cleanup_protects_lane_between_stages(self):
        batch = self.run_batch(self.plan([{"key": "a", "stages": [self.stage("a1")]}]))
        tree = Path(next(iter(self.workspaces(batch, "a").values()))["path"])
        (tree / "lane-writes.txt").unlink()
        state_path = self.registry / "batches" / batch / "state.json"
        state = json.loads(state_path.read_text())
        state["status"] = "running"
        state_path.write_text(json.dumps(state))
        log = self.registry / "worktrees.jsonl"
        rows = [json.loads(line) for line in log.read_text().splitlines()]
        log.write_text("".join(json.dumps(dict(row, at=0)) + "\n" for row in rows))
        result = self.cli("worktree", "clean", "--older-than", "1s", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(tree.exists())
        self.assertIn("batch " + batch, result.stdout)

    def test_distinct_workspace_in_one_lane_gets_its_own_worktree(self):
        plan = self.plan([{"key": "a", "stages": [self.stage("a1"), self.stage("a2", workspace=str(self.repo2))]}])
        batch = self.run_batch(plan)
        entries = self.workspaces(batch, "a")
        self.assertEqual({entry["source"] for entry in entries.values()}, {str(self.repo), str(self.repo2)})
        self.assertEqual(len({entry["path"] for entry in entries.values()}), 2,
                         "a lane must not reuse one repository's worktree for another repository")
        first = self.worker_state(batch, "a.a1")
        second = self.worker_state(batch, "a.a2")
        self.assertNotEqual(first["actual_workspace"], second["actual_workspace"])
        self.assertTrue((Path(first["actual_workspace"]) / "seed-one.txt").is_file())
        self.assertFalse((Path(first["actual_workspace"]) / "seed-two.txt").exists())
        self.assertTrue((Path(second["actual_workspace"]) / "seed-two.txt").is_file())
        self.assertEqual((Path(second["actual_workspace"]) / "lane-writes.txt").read_text(),
                         "WRITE A2\n", "the second stage must not inherit the first repository's edits")

    # B. read-only stages ------------------------------------------------------
    def test_read_only_stage_reads_the_lane_worktree(self):
        plan = self.plan([
            {"key": "w", "stages": [self.stage("w1"), self.stage("w2", read_only=True)]},
            {"key": "r", "stages": [self.stage("r1", read_only=True)]},
        ])
        batch = self.run_batch(plan)
        lane = next(iter(self.workspaces(batch, "w").values()))
        reader = self.worker_state(batch, "w.w2")
        self.assertTrue(reader["launch"]["read_only"])
        self.assertEqual(reader["actual_workspace"], lane["path"],
                         "a read-only stage must read the preceding lane writer's output")
        self.assertIn("SEEN[WRITE W1]", self.result(batch, "w.w2"))
        self.assertEqual(self.workspaces(batch, "r"), {}, "a read-only-only lane keeps the workspace")
        plain = self.worker_state(batch, "r.r1")
        self.assertTrue(plain["launch"]["read_only"])
        self.assertNotIn("actual_workspace", plain)
        self.assertFalse((self.repo / "lane-writes.txt").exists())

    # C. explicit shared workspace --------------------------------------------
    def test_shared_workspace_override_keeps_the_workspace(self):
        plan = self.plan([{"key": "a", "stages": [self.stage("a1"), self.stage("a2")]}],
                         shared_workspace=True)
        batch = self.run_batch(plan)
        entries = self.workspaces(batch, "a")
        self.assertEqual([entry["mode"] for entry in entries.values()], ["shared"])
        self.assertIn("the plan sets shared_workspace", next(iter(entries.values()))["reason"])
        for stage in ("a.a1", "a.a2"):
            self.assertNotIn("actual_workspace", self.worker_state(batch, stage))
        self.assertEqual((self.repo / "lane-writes.txt").read_text(), "WRITE A1\nWRITE A2\n")
        self.assertNotIn("not a Git repository", self.runner_log(batch),
                         "an explicit override needs no warning")
        self.assertIn("shared workspace", self.cli("batch", "status", batch).stdout)

    # D. non-Git workspaces ----------------------------------------------------
    def test_non_git_workspace_shares_with_an_actionable_warning(self):
        plan = self.plan([{"key": "a", "stages": [self.stage("a1"), self.stage("a2")]}],
                         workspace=self.plain)
        batch = self.run_batch(plan)
        entries = self.workspaces(batch, "a")
        self.assertEqual([entry["mode"] for entry in entries.values()], ["shared"])
        self.assertIn("is not a Git repository", next(iter(entries.values()))["reason"])
        for stage in ("a.a1", "a.a2"):
            self.assertNotIn("actual_workspace", self.worker_state(batch, stage))
        self.assertEqual((self.plain / "lane-writes.txt").read_text(), "WRITE A1\nWRITE A2\n")
        log = self.runner_log(batch)
        self.assertIn("is not a Git repository", log)
        self.assertIn("shared_workspace", log, "the warning must say how to accept or fix it")
        self.assertIn("is not a Git repository", self.cli("batch", "status", batch).stdout)

    # E. invalid option --------------------------------------------------------
    def test_invalid_shared_workspace_option_is_rejected(self):
        lanes = [{"key": "a", "stages": [self.stage("a1")]}]
        for value in ("yes", 1, []):
            with self.subTest(value=value):
                plan = self.plan(lanes, shared_workspace=value)
                for action in ("validate", "start"):
                    result = self.cli("batch", action, str(plan))
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertIn("shared_workspace must be a boolean", result.stdout + result.stderr)
        for plan in (self.plan([{"key": "a", "stages": [self.stage("a1", shared_workspace=True)]}]),
                     self.plan(lanes, defaults={"engine": "fixture", "shared_workspace": True})):
            result = self.cli("batch", "validate", str(plan))
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("unknown option: shared_workspace", result.stdout + result.stderr)
        self.assertEqual(json.loads(self.cli("batch", "list", "--json").stdout), [])


if __name__ == "__main__":
    unittest.main()
