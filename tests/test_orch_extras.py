"""Acceptance tests: post-pass critic, integrate, branch cleanup and lane setup for claive-orch."""
import hermetic  # noqa: F401  (must run before claivelib reads the environment)
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

REPO_ROOT = Path(__file__).resolve().parents[1]
ORCH = str(REPO_ROOT / "bin/claive-orch")
sys.path.insert(0, str(REPO_ROOT / "bin"))
from claivelib import orchestration as orch

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@localhost",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@localhost"}
BROKEN = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"
TESTS = "import unittest\nfrom calc import add\n\n\nclass Calc(unittest.TestCase):\n" \
        "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n"
DEFECT = '```json\n{"no_concrete_defect": false, "approach_sound": true, "defects": [{"location": "calc.py:2", ' \
         '"description": "changes rounding beyond the task", "evidence": "diff", "localized": true}]}\n```'
CLEAN = '{"no_concrete_defect": true, "approach_sound": true, "defects": []}'


def history(arm, *items, rounds=2, post_pass=True):
    config = {"arm": arm, "rounds": rounds, "post_pass_critic": post_pass}
    events = [{"type": "run.started", "time": 0, "data": {"config": config}}]
    return orch.fold(events + [{"type": kind, "time": 0, "data": data} for kind, data in items])


def lane(name):
    return ("lane.added", {"lane": name, "engine": "muse", "family": "muse", "branch": f"o/{name}",
                           "path": f"/w/{name}", "base_commit": "base"})


def verified(name, score, passed, improved=True):
    result = {"passed": passed, "score": score, "status": "pass" if passed else "fail"}
    return [("verification.completed", {"lane": name, "result": result}),
            ("checkpoint.accepted", {"lane": name, "result": result, "commit": f"c{score}",
                                     "improved": improved, "diff_lines": 3})]


def critique(name, round_number, defects, post_pass=False):
    return ("critique.completed", {"lane": name, "round": round_number, "no_concrete_defect": not defects,
                                   "approach_sound": True, "post_pass": post_pass,
                                   "defects": [{"localized": True, "description": "d"}] * defects})


IMPLEMENTER = ("worker.registered", {"lane": "a", "role": "implementer", "worker_id": "w"})


class PostPassDecisions(unittest.TestCase):
    def test_disabled_finishes_on_first_pass(self):
        decision = orch.decide(history("D", lane("a"), IMPLEMENTER, *verified("a", 1.0, True), post_pass=False))
        self.assertEqual((decision["action"], decision["outcome"]), ("finish", "verified"))

    def test_first_pass_asks_for_post_pass_critique(self):
        decision = orch.decide(history("D", lane("a"), IMPLEMENTER, *verified("a", 1.0, True)))
        self.assertEqual((decision["action"], decision["lane"], decision["round"]), ("critique", "a", 1))
        self.assertTrue(decision["post_pass"])

    def test_clean_post_pass_critique_finishes(self):
        decision = orch.decide(history("B", lane("a"), IMPLEMENTER, *verified("a", 1.0, True),
                                       critique("a", 1, 0, post_pass=True)))
        self.assertEqual((decision["action"], decision["outcome"]), ("finish", "verified"))

    def test_defects_get_one_correction_then_finish(self):
        base = [lane("a"), IMPLEMENTER, *verified("a", 1.0, True), critique("a", 1, 1, post_pass=True)]
        decision = orch.decide(history("D", *base))
        self.assertEqual((decision["action"], decision["round"]), ("correct", 1))
        self.assertTrue(decision["post_pass"])
        after = orch.decide(history("D", *base, *verified("a", 1.0, True, improved=False)))
        self.assertEqual((after["action"], after["outcome"]), ("finish", "verified"))

    def test_no_correction_when_round_limit_is_used(self):
        items = [lane("a"), IMPLEMENTER, *verified("a", 0.5, False), critique("a", 1, 1),
                 *verified("a", 1.0, True), critique("a", 2, 1, post_pass=True)]
        decision = orch.decide(history("D", *items, rounds=1))
        self.assertEqual((decision["action"], decision["outcome"]), ("finish", "verified"))

    def test_only_one_post_pass_critique_after_late_pass(self):
        items = [lane("a"), IMPLEMENTER, *verified("a", 0.5, False), critique("a", 1, 1),
                 *verified("a", 1.0, True)]
        decision = orch.decide(history("D", *items))
        self.assertEqual((decision["action"], decision["round"], decision["post_pass"]), ("critique", 2, True))


class OrchRun(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.registry = self.base / "registry"
        self.env = dict(os.environ, CLAIVE_DIR=str(self.registry), **GIT_ENV)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        (self.repo / "calc.py").write_text(BROKEN)
        (self.repo / "test_calc.py").write_text(TESTS)
        (self.repo / "notes.txt").write_text("user notes\n")
        self.git("init", "-q")
        self.git("add", "-A")
        self.git("commit", "-qm", "base")
        self.task = self.base / "task.md"
        self.task.write_text("Fix add in calc.py so the tests pass.\n")

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *arguments):
        return subprocess.run(["git", "-C", str(self.repo), *arguments], check=True, env=self.env,
                              capture_output=True, text=True).stdout

    def orch(self, *arguments, ok=True):
        result = subprocess.run([ORCH, *arguments], capture_output=True, text=True, env=self.env)
        if ok:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def init(self, arm, *extra):
        output = self.orch("init", "--repo", str(self.repo), "--task-file", str(self.task), "--arm", arm,
                           "--verify", "python3 -m unittest -q", *extra).stdout
        return re.search(r"Run ([0-9a-f]{12})", output).group(1)

    def next(self, run):
        return json.loads(self.orch("next", run, "--json").stdout)

    def worker(self, workspace, engine, model, answer="done"):
        worker_id = uuid.uuid4().hex[:12]
        path = self.registry / worker_id
        (path / "requests").mkdir(parents=True)
        state = {"id": worker_id, "engine": engine, "model": model, "workspace": str(workspace),
                 "status": "completed", "started_at": time.time(), "turn": 1, "launch": {"model": model}}
        (path / "state.json").write_text(json.dumps(state))
        (path / "result.txt").write_text(answer)
        return worker_id

    def solve(self, run, content=FIXED):
        self.orch("lane", run, "a", "--engine", "muse", "--model", "muse-spark-1.3-contributor")
        lane_a = self.registry / "runs" / run / "lanes" / "a"
        implementer = self.worker(lane_a, "muse", "muse-spark-1.3-contributor")
        self.orch("worker", run, "a", "--role", "implementer", "--worker-id", implementer)
        (lane_a / "calc.py").write_text(content)
        self.orch("verify", run, "a")
        return lane_a

    def finished(self, arm="D"):
        run = self.init(arm)
        self.solve(run)
        self.orch("finish", run)
        return run

    def branches(self):
        return self.git("branch", "--list", "orch/*", "--format=%(refname:short)").split()

    # ------------------------------------------------------------ post-pass critic

    def test_post_pass_critic_flow(self):
        run = self.init("D", "--post-pass-critic")
        lane_a = self.solve(run)
        decision = self.next(run)
        self.assertEqual((decision["action"], decision["round"], decision["post_pass"]), ("critique", 1, True))
        text = Path(self.orch("prompt", run, "critique", "a").stdout.strip()).read_text()
        self.assertIn("Post-pass review", text)
        self.assertIn("verifier passes", text)
        self.assertIn("beyond the task", text)
        self.assertIn("add", text)
        critic = self.worker(lane_a, "pi", "mimo-v2.6-flash-free", DEFECT)
        self.orch("worker", run, "a", "--role", "critic", "--worker-id", critic)
        self.assertIn("1 defect", self.orch("critique", run, "a", "--worker-id", critic).stdout)
        decision = self.next(run)
        self.assertEqual((decision["action"], decision["post_pass"]), ("correct", True))
        text = Path(self.orch("prompt", run, "correct", "a").stdout.strip()).read_text()
        self.assertIn("changes rounding beyond the task", text)
        (lane_a / "calc.py").write_text(FIXED + "\n")
        self.orch("verify", run, "a")
        decision = self.next(run)
        self.assertEqual((decision["action"], decision["outcome"]), ("finish", "verified"))

    def test_post_pass_clean_critique_finishes(self):
        run = self.init("B", "--post-pass-critic")
        lane_a = self.solve(run)
        critic = self.worker(lane_a, "pi", "big-pickle", CLEAN)
        self.orch("worker", run, "a", "--role", "critic", "--worker-id", critic)
        self.orch("critique", run, "a", "--worker-id", critic)
        self.assertEqual(self.next(run)["outcome"], "verified")

    def test_post_pass_critic_rejected_for_other_arms(self):
        for arm in ("A", "B0", "R"):
            result = self.orch("init", "--repo", str(self.repo), "--task-file", str(self.task), "--arm", arm,
                               "--verify", "python3 -m unittest -q", "--post-pass-critic", ok=False)
            self.assertNotEqual(result.returncode, 0, arm)
            self.assertIn("--post-pass-critic", result.stderr)

    # ------------------------------------------------------------------ integrate

    def test_integrate_applies_unstaged_and_verifies(self):
        run = self.finished()
        output = self.orch("integrate", run).stdout
        self.assertEqual((self.repo / "calc.py").read_text(), FIXED)
        self.assertEqual(self.git("diff", "--cached", "--name-only").strip(), "")
        self.assertEqual(self.git("diff", "--name-only").strip(), "calc.py")
        self.assertIn("Verifier in", output)
        self.assertIn("pass", output)
        report = json.loads(self.orch("report", run, "--json").stdout)
        self.assertTrue(report["integrated"])
        again = self.orch("integrate", run, ok=False)
        self.assertIn("already integrated", again.stderr)

    def test_integrate_keeps_unrelated_user_changes(self):
        run = self.finished()
        (self.repo / "notes.txt").write_text("edited by user\n")
        self.git("add", "notes.txt")
        self.orch("integrate", run)
        self.assertEqual(self.git("diff", "--cached", "--name-only").strip(), "notes.txt")
        self.assertEqual((self.repo / "calc.py").read_text(), FIXED)

    def test_integrate_fails_when_checkout_verifier_fails(self):
        run = self.finished()
        (self.repo / "test_calc.py").write_text(TESTS + "\n    def test_bad(self):\n        self.fail('x')\n")
        result = self.orch("integrate", run, ok=False)
        self.assertEqual(result.returncode, 1)
        self.assertEqual((self.repo / "calc.py").read_text(), FIXED)
        self.assertIn("fail", result.stdout)

    def test_integrate_conflict_reports_and_exits_1(self):
        run = self.finished()
        (self.repo / "calc.py").write_text("def add(a, b):\n    return b - a\n")
        self.git("commit", "-qam", "user change")
        result = self.orch("integrate", run, ok=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("calc.py", result.stdout + result.stderr)
        self.assertIn("conflict", (result.stdout + result.stderr).lower())

    def test_integrate_refuses_unfinished_or_unverified(self):
        run = self.init("D")
        self.assertIn("not finished", self.orch("integrate", run, ok=False).stderr)
        run = self.init("A")
        self.solve(run, content=BROKEN.replace("a - b", "a * b"))
        self.orch("finish", run)
        self.assertIn("not verified", self.orch("integrate", run, ok=False).stderr)
        self.assertEqual((self.repo / "calc.py").read_text(), BROKEN)

    def test_integrate_no_verify(self):
        run = self.finished()
        output = self.orch("integrate", run, "--no-verify").stdout
        self.assertNotIn("Verifier in", output)
        self.assertEqual((self.repo / "calc.py").read_text(), FIXED)

    # ------------------------------------------------------------ branch cleanup

    def test_cleanup_branches_deletes_run_branches(self):
        run = self.finished()
        self.orch("integrate", run, "--no-verify")
        self.assertEqual(self.branches(), [f"orch/{run}/a"])
        output = self.orch("cleanup", run, "--branches").stdout
        self.assertIn(f"Deleted branch orch/{run}/a", output)
        self.assertEqual(self.branches(), [])

    def test_cleanup_branches_keeps_unintegrated_winner(self):
        run = self.finished()
        output = self.orch("cleanup", run, "--branches").stdout
        self.assertIn(f"Kept orch/{run}/a", output)
        self.assertIn("not integrated", output)
        self.assertEqual(self.branches(), [f"orch/{run}/a"])
        self.orch("cleanup", run, "--branches", "--force")
        self.assertEqual(self.branches(), [])

    def test_prune_is_dry_run_by_default(self):
        done = self.finished()
        self.orch("integrate", done, "--no-verify")
        self.git("checkout", "-q", "--", "calc.py")
        self.orch("cleanup", done)
        running = self.init("D")
        self.orch("lane", running, "a", "--engine", "muse", "--model", "muse-spark-1.3-contributor")
        self.git("branch", "orch/ffffffffffff/a")
        output = self.orch("prune", "--repo", str(self.repo)).stdout
        self.assertIn(f"Would delete orch/{done}/a", output)
        self.assertIn(f"Kept orch/{running}/a", output)
        self.assertIn("Kept orch/ffffffffffff/a", output)
        self.assertEqual(len(self.branches()), 3)
        output = self.orch("prune", "--repo", str(self.repo), "--apply").stdout
        self.assertIn(f"Deleted branch orch/{done}/a", output)
        self.assertEqual(sorted(self.branches()), sorted([f"orch/{running}/a", "orch/ffffffffffff/a"]))

    # ---------------------------------------------------------------- lane setup

    def test_setup_runs_at_base_check_and_in_each_lane(self):
        marker = self.base / "setup.log"
        setup = f'echo "$PWD $CLAIVE_ORCH_REPO" >> {marker} && touch .setup-done'
        run = self.init("D", "--setup", setup)
        lines = marker.read_text().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].endswith(f" {self.repo}"))
        lane_a = self.solve(run)
        lines = marker.read_text().splitlines()
        self.assertEqual(lines[1], f"{lane_a} {self.repo}")
        self.assertTrue((lane_a / ".setup-done").exists())

    def test_failing_setup_removes_the_lane(self):
        flag = self.base / "fail-now"
        run = self.init("D", "--setup", f'test ! -e {flag} || {{ echo setup-broke; exit 7; }}')
        flag.touch()
        result = self.orch("lane", run, "a", "--engine", "muse", "--model", "muse-spark-1.3-contributor",
                           ok=False)
        self.assertIn("setup", result.stderr)
        self.assertIn("setup-broke", result.stderr)
        self.assertFalse((self.registry / "runs" / run / "lanes" / "a").exists())
        self.assertEqual(self.branches(), [])
        self.assertEqual(self.next(run)["action"], "add_lane")
        flag.unlink()
        self.orch("lane", run, "a", "--engine", "muse", "--model", "muse-spark-1.3-contributor")

    def test_failing_setup_at_base_aborts_init(self):
        result = self.orch("init", "--repo", str(self.repo), "--task-file", str(self.task), "--arm", "D",
                           "--verify", "python3 -m unittest -q", "--setup", "exit 3", ok=False)
        self.assertIn("setup", result.stderr)


if __name__ == "__main__":
    unittest.main()
