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
ORCH = str(REPO_ROOT / "bin/codex-orch")
sys.path.insert(0, str(REPO_ROOT / "bin"))
from codex_workers import orchestration as orch

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@localhost",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@localhost"}
BROKEN = "def add(a, b):\n    return a - b\n\n\ndef mul(a, b):\n    return a + b\n"
HALF = "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a + b\n"
FIXED = "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"
TESTS = """import unittest
from calc import add, mul


class Calc(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(2, 3), 5)

    def test_mul(self):
        self.assertEqual(mul(2, 3), 6)
"""


class PureChecks(unittest.TestCase):
    def test_model_family(self):
        cases = {"muse-spark-1.3-contributor": "muse-spark", "muse-spark-1.3-free": "muse-spark",
                 "nemotron-3-ultra-free": "nemotron", "nemotron-3.5-lightning-free": "nemotron",
                 "mimo-v2.6-flash-free": "mimo", "big-pickle": "big-pickle", "space-bunny-free": "space-bunny",
                 "ling-3.1-flash-free": "ling", "longcat-2.5-preview-free": "longcat",
                 "opencode2api/mimo-v2.6-flash-free": "mimo"}
        for model, family in cases.items():
            self.assertEqual(orch.model_family(model), family, model)
        self.assertEqual(orch.model_family(None, "muse"), "muse")

    def test_parse_score(self):
        self.assertEqual(orch.parse_score("....\nRan 5 tests in 0.1s\n\nFAILED (failures=1, errors=1)\n"), (3, 5))
        self.assertEqual(orch.parse_score("Ran 4 tests in 0.0s\n\nOK\n"), (4, 4))
        self.assertEqual(orch.parse_score("===== 2 failed, 7 passed, 1 error in 0.52s ====="), (7, 10))
        self.assertEqual(orch.parse_score("===== 3 passed in 0.01s ====="), (3, 3))
        self.assertEqual(orch.parse_score("Tests:       1 failed, 4 passed, 5 total"), (4, 5))
        self.assertEqual(orch.parse_score("# tests 3\n# pass 2\n# fail 1\n"), (2, 3))
        self.assertEqual(orch.parse_score("test result: FAILED. 3 passed; 1 failed; 0 ignored\n"
                                          "test result: ok. 2 passed; 0 failed;"), (5, 6))
        self.assertIsNone(orch.parse_score("error: build failed"))
        self.assertEqual(orch.parse_score("ok=3 bad=1", r"ok=(?P<passed>\d+) bad=(?P<failed>\d+)"), (3, 4))

    def test_critique_parsing_and_validation(self):
        text = 'Reasoning...\n```json\n{"no_concrete_defect": false, "approach_sound": true, "defects": [' \
               '{"location": "calc.py:5", "description": "mul adds", "evidence": "verifier", "localized": true},' \
               '{"location": "", "description": "vague", "evidence": "diff"}]}\n```\n'
        critique = orch.validate_critique(orch.extract_json(text))
        self.assertFalse(critique["no_concrete_defect"])
        self.assertEqual(len(critique["defects"]), 1)
        self.assertEqual(critique["dropped_defects"], 1)
        camel = orch.validate_critique({"noConcreteDefect": True, "approachSound": False, "defects": []})
        self.assertTrue(camel["no_concrete_defect"])
        self.assertFalse(camel["approach_sound"])
        unsupported = orch.validate_critique({"defects": [{"location": "x", "description": "y", "evidence": "vibes"}]})
        self.assertTrue(unsupported["no_concrete_defect"])
        self.assertIn("error", unsupported)
        self.assertEqual(orch.extract_json('trailing {"prefer": "b", "reason": "x"} done')["prefer"], "b")
        with self.assertRaises(ValueError):
            orch.extract_json("no json here")

    def test_decide_transitions(self):
        def run(arm, *items, rounds=2):
            history = [{"type": "run.started", "time": 0, "data": {"config": {"arm": arm, "rounds": rounds}}}]
            history += [{"type": kind, "time": 0, "data": data} for kind, data in items]
            return orch.decide(orch.fold(history))

        def lane(name, engine="muse"):
            return ("lane.added", {"lane": name, "engine": engine, "family": engine, "branch": f"o/{name}",
                                   "path": f"/w/{name}", "base_commit": "base"})

        def verified(name, score, passed=False, improved=True):
            result = {"passed": passed, "score": score, "status": "pass" if passed else "fail"}
            return [("verification.completed", {"lane": name, "result": result}),
                    ("checkpoint.accepted", {"lane": name, "result": result, "commit": f"c{score}",
                                             "improved": improved, "diff_lines": 3})]

        def reverted(name, score):
            result = {"passed": False, "score": score, "status": "fail"}
            return [("verification.completed", {"lane": name, "result": result}),
                    ("checkpoint.reverted", {"lane": name, "result": result})]

        def critique(name, round_number, defects=1, localized=True, sound=True):
            return ("critique.completed", {"lane": name, "round": round_number, "no_concrete_defect": not defects,
                                           "approach_sound": sound,
                                           "defects": [{"localized": localized, "description": "d"}] * defects})

        implementer = ("worker.registered", {"lane": "a", "role": "implementer", "worker_id": "w"})
        self.assertEqual(run("B")["action"], "add_lane")
        self.assertEqual(run("B", lane("a"))["action"], "implement")
        self.assertEqual(run("B", lane("a"), implementer)["action"], "verify")
        first = [lane("a"), implementer, *verified("a", 0.5)]
        self.assertEqual(run("A", *first)["outcome"], "stalled")
        self.assertEqual(run("B", *first)["action"], "critique")
        self.assertEqual(run("B0", *first)["action"], "correct")
        self.assertEqual(run("B", *first, critique("a", 1))["action"], "correct")
        stalled = run("B", *first, critique("a", 1, defects=0))
        self.assertEqual((stalled["action"], stalled["outcome"]), ("finish", "stalled"))
        self.assertEqual(run("D", *first, critique("a", 1, defects=0))["action"], "add_lane")
        self.assertEqual(run("D", *first, critique("a", 1, localized=False))["action"], "add_lane")
        self.assertEqual(run("B", *first, critique("a", 1, localized=False))["action"], "correct")
        passed = run("B", *first, critique("a", 1), *verified("a", 1.0, passed=True))
        self.assertEqual((passed["action"], passed["outcome"], passed["commit"]), ("finish", "verified", "c1.0"))
        # Two consecutive non-improving rounds stall even with rounds left.
        flat = [*first, critique("a", 1), *reverted("a", 0.2), critique("a", 2), *verified("a", 0.5, improved=False)]
        self.assertEqual(run("B", *flat, rounds=3)["outcome"], "stalled")
        # Round limit.
        limited = [*first, critique("a", 1), *verified("a", 0.7)]
        self.assertEqual(run("B", *limited, rounds=1)["action"], "finish")
        self.assertEqual(run("B", *limited, rounds=2)["action"], "critique")
        # Arm R adds a same-configuration lane; arm R' a different one.
        self.assertEqual(run("R", *first)["diversity"], "same")
        self.assertEqual(run("R'", *first)["diversity"], "different")
        # Breadth: verifier picks, ties go to one review, unresolved without a pass.
        breadth = [*first, critique("a", 1, defects=0), lane("b", "pi"),
                   ("worker.registered", {"lane": "b", "role": "implementer", "worker_id": "x"})]
        self.assertEqual(run("D", *breadth, *verified("b", 0.9))["action"], "critique")
        better = run("D", *breadth, *verified("b", 0.9), critique("b", 1, defects=0))
        self.assertEqual((better["lane"], better["outcome"]), ("b", "unresolved"))
        tie = [*breadth, *verified("b", 0.5), critique("b", 1, defects=0)]
        self.assertEqual(run("D", *tie)["action"], "review")
        reviewed = run("D", *tie, ("review.completed", {"prefer": "b"}))
        self.assertEqual((reviewed["action"], reviewed["lane"]), ("finish", "b"))
        self.assertEqual(run("D", *breadth, *verified("b", 1.0, passed=True))["outcome"], "verified")


class RunChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.registry = self.base / "registry"
        self.env = dict(os.environ, CODEX_WORKERS_DIR=str(self.registry), **GIT_ENV)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        (self.repo / "calc.py").write_text(BROKEN)
        (self.repo / "test_calc.py").write_text(TESTS)
        for command in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "base"]):
            subprocess.run(["git", "-C", str(self.repo), *command], check=True, env=self.env)
        self.task = self.base / "task.md"
        self.task.write_text("Fix add and mul in calc.py so the tests pass.\n")

    def tearDown(self):
        self.temp.cleanup()

    def orch(self, *arguments, ok=True):
        result = subprocess.run([ORCH, *arguments], capture_output=True, text=True, env=self.env)
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def init(self, arm, *extra):
        output = self.orch("init", "--repo", str(self.repo), "--task-file", str(self.task), "--arm", arm,
                           "--verify", "python3 -m unittest -q", "--experiment", "exp", *extra).stdout
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

    def test_ladder_with_revert_stall_and_breadth(self):
        run = self.init("D")
        self.assertEqual(self.next(run)["action"], "add_lane")
        self.orch("lane", run, "a", "--engine", "muse", "--model", "muse-spark-1.3-contributor")
        lane_a = self.registry / "runs" / run / "lanes" / "a"
        self.assertEqual(self.next(run)["action"], "implement")
        prompt = self.orch("prompt", run, "implement", "a").stdout.strip()
        self.assertIn("Fix add and mul", Path(prompt).read_text())
        implementer = self.worker(lane_a, "muse", "muse-spark-1.3-contributor")
        self.orch("worker", run, "a", "--role", "implementer", "--worker-id", implementer)
        (lane_a / "calc.py").write_text(HALF)
        self.assertIn("checkpoint", self.orch("verify", run, "a").stdout)
        decision = self.next(run)
        self.assertEqual((decision["action"], decision["round"]), ("critique", 1))
        self.assertIn("mul", Path(self.orch("prompt", run, "critique", "a").stdout.strip()).read_text())
        early = self.orch("prompt", run, "correct", "a", ok=False)
        self.assertIn("next action: critique", early.stderr)
        same = self.worker(lane_a, "pi", "muse-spark-1.3-free")
        refused = self.orch("worker", run, "a", "--role", "critic", "--worker-id", same, ok=False)
        self.assertIn("family", refused.stderr)
        answer = '```json\n{"no_concrete_defect": false, "approach_sound": true, "defects": [{"location": ' \
                 '"calc.py:6", "description": "mul returns a + b", "evidence": "both", "localized": true}]}\n```'
        critic = self.worker(lane_a, "pi", "nemotron-3-ultra-free", answer)
        self.orch("worker", run, "a", "--role", "critic", "--worker-id", critic)
        self.assertIn("1 defect", self.orch("critique", run, "a", "--worker-id", critic).stdout)
        self.assertEqual(self.next(run)["action"], "correct")
        self.assertIn("mul returns a + b", Path(self.orch("prompt", run, "correct", "a").stdout.strip()).read_text())
        (lane_a / "calc.py").write_text("syntax error(\n")
        (lane_a / "junk.py").write_text("x = 1\n")
        self.assertIn("reverted", self.orch("verify", run, "a").stdout)
        self.assertEqual((lane_a / "calc.py").read_text(), HALF)
        self.assertFalse((lane_a / "junk.py").exists())
        decision = self.next(run)
        self.assertEqual((decision["action"], decision["round"]), ("critique", 2))
        nothing = self.worker(lane_a, "pi", "mimo-v2.6-flash-free", '{"no_concrete_defect": true, "defects": []}')
        self.orch("worker", run, "a", "--role", "critic", "--worker-id", nothing)
        self.orch("critique", run, "a", "--worker-id", nothing)
        decision = self.next(run)
        self.assertEqual((decision["action"], decision["lane"]), ("add_lane", "b"))
        refused = self.orch("lane", run, "b", "--engine", "muse", "--model", "muse-spark-1.3-contributor", ok=False)
        self.assertIn("differ", refused.stderr)
        self.orch("lane", run, "b", "--engine", "pi", "--model", "big-pickle", "--strategy", "rewrite calc from scratch")
        lane_b = self.registry / "runs" / run / "lanes" / "b"
        self.assertIn("rewrite calc", Path(self.orch("prompt", run, "implement", "b").stdout.strip()).read_text())
        self.assertEqual((lane_b / "calc.py").read_text(), BROKEN)
        second = self.worker(lane_b, "pi", "big-pickle")
        self.orch("worker", run, "b", "--role", "implementer", "--worker-id", second)
        (lane_b / "calc.py").write_text(FIXED)
        self.orch("verify", run, "b")
        decision = self.next(run)
        self.assertEqual((decision["action"], decision["outcome"], decision["lane"]), ("finish", "verified", "b"))
        self.orch("finish", run)
        self.orch("usage", run)
        report = json.loads(self.orch("report", run, "--json").stdout)
        self.assertEqual(report["lanes"]["a"]["trajectory"], [0.5, 0.0])
        self.assertEqual(report["lanes"]["b"]["trajectory"], [1.0])
        self.assertEqual(report["winner"], "b")
        self.assertEqual(report["lanes"]["a"]["critic_families"], ["mimo", "nemotron"])
        branch = subprocess.run(["git", "-C", str(self.repo), "show", f"orch/{run}/b:calc.py"],
                                capture_output=True, text=True, check=True).stdout
        self.assertEqual(branch, FIXED)
        self.assertIn("| D | 1 | 1 |", self.orch("compare", "--experiment", "exp").stdout)
        self.orch("cleanup", run)
        self.assertFalse(lane_a.exists())
        self.assertEqual((self.repo / "calc.py").read_text(), BROKEN)

    def test_arm_guards(self):
        run = self.init("B0")
        self.orch("lane", run, "a", "--engine", "muse")
        lane_a = self.registry / "runs" / run / "lanes" / "a"
        self.orch("worker", run, "a", "--role", "implementer", "--worker-id", self.worker(lane_a, "muse", None))
        self.orch("verify", run, "a")
        self.assertEqual(self.next(run)["action"], "correct")
        refused = self.orch("critique", run, "a", "--file", str(self.task), ok=False)
        self.assertIn("B0", refused.stderr)
        self.assertNotEqual(self.orch("finish", run, ok=False).returncode, 0)
        self.orch("finish", run, "--abort", "--reason", "test")
        self.assertEqual(json.loads(self.orch("report", run, "--json").stdout)["outcome"], "aborted")

        run = self.init("R")
        self.orch("lane", run, "a", "--engine", "pi", "--model", "big-pickle")
        lane_a = self.registry / "runs" / run / "lanes" / "a"
        wrong = self.worker(self.repo, "pi", "big-pickle")
        refused = self.orch("worker", run, "a", "--role", "implementer", "--worker-id", wrong, ok=False)
        self.assertIn("lane worktree", refused.stderr)
        self.orch("worker", run, "a", "--role", "implementer", "--worker-id", self.worker(lane_a, "pi", "big-pickle"))
        self.orch("verify", run, "a")
        refused = self.orch("lane", run, "b", "--engine", "pi", "--model", "mimo-v2.6-flash-free", ok=False)
        self.assertIn("same configuration", refused.stderr)

    def test_held_out_check_names_do_not_leak(self):
        hidden = self.base / "hidden"
        hidden.mkdir()
        (hidden / "test_secret.py").write_text("import unittest\n")
        run = self.orch("init", "--repo", str(self.repo), "--task-file", str(self.task), "--arm", "B",
                        "--verify", "python3 -m unittest -q test_calc test_secret",
                        "--acceptance-dir", str(hidden)).stdout
        run = re.search(r"Run ([0-9a-f]{12})", run).group(1)
        self.orch("lane", run, "a", "--engine", "muse", "--model", "muse-spark-1.3-contributor")
        text = Path(self.orch("prompt", run, "implement", "a").stdout.strip()).read_text()
        self.assertNotIn("test_secret", text)
        self.assertIn("Held-out checks", text)
        forced = self.orch("prompt", run, "review", "--force").stdout.strip()
        self.assertTrue(Path(forced).is_file())
        self.assertIn("forced review prompt", self.orch("report", run).stdout)

    def test_passing_base_is_rejected(self):
        (self.repo / "calc.py").write_text(FIXED)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qam", "fixed"], check=True, env=self.env)
        result = self.orch("init", "--repo", str(self.repo), "--task-file", str(self.task),
                           "--verify", "python3 -m unittest -q", ok=False)
        self.assertIn("already passes", result.stderr)


if __name__ == "__main__":
    unittest.main()
