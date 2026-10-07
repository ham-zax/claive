"""Acceptance tests: setup-local paths, rescore, reject, integrate paths, report tokens and verify cache."""
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
PARTIAL = "def add(a, b):\n    return 0 if a == 1 else a + b\n"
WRONG = "def add(a, b):\n    return a * b\n"
TESTS = "import unittest\nfrom calc import add\n\n\nclass Calc(unittest.TestCase):\n" \
        "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n"
HIDDEN = "import unittest\nfrom calc import add\n\n\nclass Hidden(unittest.TestCase):\n" \
         "    def test_ones(self):\n        self.assertEqual(add(1, 1), {})\n"
TWO_DEFECTS = '```json\n{"no_concrete_defect": false, "approach_sound": true, "defects": [' \
              '{"location": "test_calc.py:7", "description": "relax the assertion in the test", ' \
              '"evidence": "diff", "localized": true}, ' \
              '{"location": "calc.py:2", "description": "special case for a == 1 is wrong", ' \
              '"evidence": "diff", "localized": true}]}\n```'
MUSE = "muse-spark-1.3-contributor"


class OrchMore(unittest.TestCase):
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
        self.task.write_text("Fix add in calc.py so the tests pass. Never weaken a test.\n")
        self.hidden = self.base / "hidden"
        self.hidden.mkdir()
        (self.hidden / "test_hidden.py").write_text(HIDDEN.format(2))

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *arguments, cwd=None):
        return subprocess.run(["git", "-C", str(cwd or self.repo), *arguments], check=True, env=self.env,
                              capture_output=True, text=True).stdout

    def orch(self, *arguments, ok=True):
        result = subprocess.run([ORCH, *arguments], capture_output=True, text=True, env=self.env)
        if ok:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def init(self, arm, *extra, verify="python3 -m unittest -q"):
        output = self.orch("init", "--repo", str(self.repo), "--task-file", str(self.task), "--arm", arm,
                           "--verify", verify, *extra).stdout
        return re.search(r"Run ([0-9a-f]{12})", output).group(1)

    def hidden_init(self, *extra):
        return self.init("D", "--acceptance-dir", str(self.hidden),
                         "--worker-verify", "python3 -m unittest -q test_calc", *extra)

    def next(self, run):
        return json.loads(self.orch("next", run, "--json").stdout)

    def worker(self, workspace, engine, model, answer="done", **extra):
        worker_id = uuid.uuid4().hex[:12]
        path = self.registry / worker_id
        (path / "requests").mkdir(parents=True)
        state = {"id": worker_id, "engine": engine, "model": model, "workspace": str(workspace),
                 "status": "completed", "started_at": time.time(), "turn": 1, "launch": {"model": model, "read_only": True},
                 **extra}
        (path / "state.json").write_text(json.dumps(state))
        (path / "result.txt").write_text(answer)
        return worker_id

    def lane(self, run):
        self.orch("lane", run, "a", "--engine", "muse", "--model", MUSE)
        lane_a = self.registry / "runs" / run / "lanes" / "a"
        implementer = self.worker(lane_a, "muse", MUSE)
        self.orch("worker", run, "a", "--role", "implementer", "--worker-id", implementer)
        return lane_a

    def solve(self, run, content=FIXED):
        lane_a = self.lane(run)
        (lane_a / "calc.py").write_text(content)
        self.orch("verify", run, "a")
        return lane_a

    def critique(self, run, lane_a, answer=TWO_DEFECTS, model="mimo-v2.6-flash-free", **extra):
        critic = self.worker(lane_a, "pi", model, answer, **extra)
        self.orch("worker", run, "a", "--role", "critic", "--worker-id", critic)
        self.orch("critique", run, "a", "--worker-id", critic)
        return critic

    def events(self, run):
        lines = (self.registry / "runs" / run / "events.jsonl").read_text().splitlines()
        return [json.loads(line) for line in lines]

    def committed(self, run):
        return self.git("ls-tree", "-r", "--name-only", f"orch/{run}/a").split()

    def report(self, run):
        return json.loads(self.orch("report", run, "--json").stdout)

    # ---------------------------------------------------------- setup-local paths

    def test_setup_files_stay_out_of_checkpoints(self):
        setup = 'ln -s "$CLAIVE_ORCH_REPO/notes.txt" linked.txt && mkdir -p build && echo x > build/out'
        run = self.init("D", "--setup", setup)
        lane_a = self.solve(run)
        files = self.committed(run)
        self.assertIn("calc.py", files)
        self.assertNotIn("linked.txt", files)
        self.assertNotIn("build/out", files)
        added = next(e["data"] for e in self.events(run) if e["type"] == "lane.added")
        self.assertEqual(sorted(p.rstrip("/") for p in added["local_paths"]), ["build", "linked.txt"])
        self.assertTrue((lane_a / "linked.txt").is_symlink())

    def test_verifier_byproducts_stay_out_of_checkpoints(self):
        # unittest imports calc.py and test_calc.py, leaving __pycache__ behind in the lane.
        run = self.init("D")
        lane_a = self.solve(run)
        self.assertTrue(list(lane_a.glob("__pycache__/*.pyc")))
        self.assertFalse([f for f in self.committed(run) if "__pycache__" in f or f.endswith(".pyc")])

    def test_revert_keeps_setup_files(self):
        setup = 'ln -s "$CLAIVE_ORCH_REPO/notes.txt" linked.txt && mkdir -p build && echo x > build/out'
        run = self.hidden_init("--setup", setup)
        lane_a = self.solve(run, PARTIAL)
        self.assertEqual(self.next(run)["action"], "critique")
        self.critique(run, lane_a)
        self.assertEqual(self.next(run)["action"], "correct")
        (lane_a / "calc.py").write_text(WRONG)
        (lane_a / "stray.txt").write_text("worker junk\n")
        self.orch("verify", run, "a")
        self.assertEqual((lane_a / "calc.py").read_text(), PARTIAL)
        self.assertFalse((lane_a / "stray.txt").exists())
        self.assertTrue((lane_a / "linked.txt").is_symlink())
        self.assertEqual((lane_a / "build" / "out").read_text(), "x\n")
        self.assertNotIn("linked.txt", self.committed(run))

    def test_cleanup_ignores_setup_files(self):
        run = self.init("D", "--setup", "mkdir -p build && echo x > build/out")
        lane_a = self.solve(run)
        self.orch("finish", run)
        output = self.orch("cleanup", run).stdout
        self.assertNotIn("uncommitted", output.lower())
        self.assertFalse(lane_a.exists())

    # --------------------------------------------------------------------- rescore

    def test_rescore_after_fixing_hidden_test(self):
        (self.hidden / "test_hidden.py").write_text(HIDDEN.format(3))
        run = self.hidden_init()
        self.solve(run)
        self.assertEqual(self.next(run)["action"], "critique")
        (self.hidden / "test_hidden.py").write_text(HIDDEN.format(2))
        output = self.orch("rescore", run, "--reason", "hidden test expected 3").stdout
        self.assertIn("Lane a rescored", output)
        decision = self.next(run)
        self.assertEqual((decision["action"], decision["outcome"]), ("finish", "verified"))
        self.orch("finish", run)
        lane = self.report(run)["lanes"]["a"]
        self.assertEqual(lane["rounds"], 0)
        self.assertEqual(lane["trajectory"], [1.0])
        self.assertEqual(lane["best"], 1.0)
        rescored = [e["data"] for e in self.events(run) if e["type"] == "verification.rescored"]
        self.assertEqual(rescored[0]["reason"], "hidden test expected 3")

    def test_rescore_requires_reason(self):
        run = self.hidden_init()
        self.solve(run, PARTIAL)
        self.assertNotEqual(self.orch("rescore", run, ok=False).returncode, 0)
        self.assertNotEqual(self.orch("rescore", run, "--reason", "", ok=False).returncode, 0)

    def test_rescore_refuses_dirty_lane(self):
        run = self.hidden_init()
        lane_a = self.solve(run, PARTIAL)
        (lane_a / "calc.py").write_text(FIXED)
        result = self.orch("rescore", run, "--reason", "x", ok=False)
        self.assertIn("uncommitted", result.stderr)
        self.assertEqual(self.report(run)["lanes"]["a"]["trajectory"], [0.5])

    def test_rescore_drops_pending_critique(self):
        run = self.hidden_init()
        lane_a = self.solve(run, PARTIAL)
        self.critique(run, lane_a)
        self.assertEqual(self.next(run)["action"], "correct")
        self.orch("rescore", run, "--reason", "verifier changed")
        decision = self.next(run)
        self.assertEqual((decision["action"], decision["round"]), ("critique", 1))
        self.assertEqual(self.report(run)["lanes"]["a"]["rounds"], 0)

    def test_rescore_refuses_finished_run(self):
        run = self.init("D")
        self.solve(run)
        self.orch("finish", run)
        self.assertIn("finished", self.orch("rescore", run, "--reason", "x", ok=False).stderr)

    # ---------------------------------------------------------------------- reject

    def test_critique_prompt_treats_constraints_as_binding(self):
        run = self.hidden_init()
        self.solve(run, PARTIAL)
        text = Path(self.orch("prompt", run, "critique", "a").stdout.strip()).read_text()
        self.assertIn("constraints as binding", text)

    def test_reject_one_defect(self):
        run = self.hidden_init()
        lane_a = self.solve(run, PARTIAL)
        self.critique(run, lane_a)
        self.orch("reject", run, "a", "--defect", "1", "--reason", "tests must not be weakened")
        self.assertEqual(self.next(run)["action"], "correct")
        text = Path(self.orch("prompt", run, "correct", "a").stdout.strip()).read_text()
        kept, _, rejected = text.partition("## Rejected suggestions")
        self.assertIn("special case for a == 1 is wrong", kept)
        self.assertNotIn("relax the assertion", kept)
        self.assertIn("relax the assertion", rejected)
        self.assertIn("tests must not be weakened", rejected)
        self.assertIn("Do not apply this.", rejected)

    def test_reject_all_defects_stalls_lane(self):
        run = self.hidden_init()
        lane_a = self.solve(run, PARTIAL)
        self.critique(run, lane_a)
        self.orch("reject", run, "a", "--defect", "1", "--defect", "2", "--reason", "both wrong")
        decision = self.next(run)
        self.assertEqual((decision["action"], decision["lane"]), ("add_lane", "b"))

    def test_reject_validation(self):
        run = self.hidden_init()
        lane_a = self.solve(run, PARTIAL)
        result = self.orch("reject", run, "a", "--defect", "1", "--reason", "x", ok=False)
        self.assertIn("no pending critique", result.stderr)
        self.critique(run, lane_a)
        self.assertNotEqual(self.orch("reject", run, "a", "--defect", "3", "--reason", "x", ok=False)
                            .returncode, 0)
        self.assertNotEqual(self.orch("reject", run, "a", "--defect", "0", "--reason", "x", ok=False)
                            .returncode, 0)
        self.assertNotEqual(self.orch("reject", run, "a", "--defect", "1", "--reason", "", ok=False)
                            .returncode, 0)
        self.assertEqual(self.next(run)["action"], "correct")

    # ------------------------------------------------------------ integrate paths

    def finished_with_junk(self):
        run = self.init("D")
        lane_a = self.lane(run)
        (lane_a / "calc.py").write_text(FIXED)
        (lane_a / "junk.txt").write_text("scratch\n")
        self.orch("verify", run, "a")
        self.orch("finish", run)
        return run

    def test_integrate_exclude(self):
        run = self.finished_with_junk()
        output = self.orch("integrate", run, "--exclude", "junk.txt").stdout
        self.assertIn("Applied: calc.py", output)
        self.assertNotIn("Applied: junk.txt", output)
        self.assertEqual((self.repo / "calc.py").read_text(), FIXED)
        self.assertFalse((self.repo / "junk.txt").exists())
        event = next(e["data"] for e in self.events(run) if e["type"] == "run.integrated")
        self.assertEqual(event["exclude"], ["junk.txt"])

    def test_integrate_paths(self):
        run = self.finished_with_junk()
        self.orch("integrate", run, "--paths", "calc.py")
        self.assertEqual((self.repo / "calc.py").read_text(), FIXED)
        self.assertFalse((self.repo / "junk.txt").exists())

    def test_integrate_empty_selection(self):
        run = self.finished_with_junk()
        result = self.orch("integrate", run, "--paths", "missing.py", ok=False)
        self.assertIn("nothing to integrate", result.stderr)
        self.assertEqual((self.repo / "calc.py").read_text(), BROKEN)

    # --------------------------------------------------------------- report tokens

    def test_report_collects_usage_when_finished(self):
        run = self.init("D")
        self.solve(run)
        self.orch("finish", run)
        output = self.orch("report", run).stdout
        self.assertIn("tokens: unknown (1 worker(s) without usage)", output)
        self.assertEqual(sum(e["type"] == "usage.collected" for e in self.events(run)), 1)
        self.orch("report", run)
        self.assertEqual(sum(e["type"] == "usage.collected" for e in self.events(run)), 1)

    def test_report_sums_known_workers(self):
        run = self.init("D")
        self.solve(run)
        self.orch("finish", run)
        path = self.registry / "runs" / run
        orch.append(path, "usage.collected", workers={
            "w1": {"role": "implementer", "lane": "a", "model": MUSE,
                   "totals": {"input_tokens": 100, "output_tokens": 10, "cached_tokens": 5}},
            "w2": {"role": "critic", "lane": "a", "model": "big-pickle", "error": "no session"}})
        summary = self.report(run)
        self.assertEqual(summary["tokens"], {"input_tokens": 100, "output_tokens": 10, "cached_tokens": 5})
        self.assertEqual(summary["tokens_unknown_workers"], 1)
        self.assertIn("(1 worker(s) unknown)", self.orch("report", run).stdout)

    def test_report_running_does_not_collect(self):
        run = self.init("D")
        self.solve(run, WRONG)
        self.assertIn("tokens: not collected", self.orch("report", run).stdout)
        self.assertFalse(any(e["type"] == "usage.collected" for e in self.events(run)))

    # ---------------------------------------------------------------- verify cache

    def test_verify_cache_restores_build_outputs(self):
        log = self.base / "build.log"
        seen = self.base / "cache-env"
        verify = (f'echo "$CLAIVE_ORCH_CACHE" > {seen}; '
                  f'test -e build/out || {{ echo built >> {log}; mkdir -p build; echo out > build/out; }}; '
                  'python3 -m unittest -q')
        run = self.init("D", "--cache-key", "echo v1", "--cache-path", "build", verify=verify)
        self.assertEqual(log.read_text().splitlines(), ["built"])
        self.assertTrue(seen.read_text().strip().startswith(str(self.registry / "orch-cache")))
        self.orch("lane", run, "a", "--engine", "muse", "--model", MUSE)
        lane_a = self.registry / "runs" / run / "lanes" / "a"
        implementer = self.worker(lane_a, "muse", MUSE)
        self.orch("worker", run, "a", "--role", "implementer", "--worker-id", implementer)
        (lane_a / "calc.py").write_text(FIXED)
        output = self.orch("verify", run, "a").stdout
        self.assertIn("Cache hit", output)
        self.assertEqual(log.read_text().splitlines(), ["built"])
        self.assertNotIn("build/out", self.committed(run))
        self.assertIn("calc.py", self.committed(run))

    def test_cache_key_change_rebuilds(self):
        log = self.base / "build.log"
        key = self.base / "key"
        key.write_text("v1\n")
        verify = (f'test -e build/out || {{ echo built >> {log}; mkdir -p build; echo out > build/out; }}; '
                  'python3 -m unittest -q')
        run = self.init("D", "--cache-key", f"cat {key}", "--cache-path", "build", verify=verify)
        key.write_text("v2\n")
        self.solve(run)
        self.assertEqual(log.read_text().splitlines(), ["built", "built"])

    def test_cache_options_go_together(self):
        for extra in (["--cache-key", "echo v1"], ["--cache-path", "build"],
                      ["--cache-key", "echo v1", "--cache-path", "../out"],
                      # "." would make a cache restore delete the whole lane worktree.
                      ["--cache-key", "echo v1", "--cache-path", "."],
                      ["--cache-key", "echo v1", "--cache-path", "./"],
                      ["--cache-key", "echo v1", "--cache-path", "src/.."],
                      ["--cache-key", "echo v1", "--cache-path", ".git/hooks"]):
            result = self.orch("init", "--repo", str(self.repo), "--task-file", str(self.task), "--arm", "D",
                               "--verify", "python3 -m unittest -q", *extra, ok=False)
            self.assertNotEqual(result.returncode, 0, extra)

    # ------------------------------------------------------------ critic fallbacks

    def test_fallback_model_family_is_checked(self):
        run = self.init("D")
        lane_a = self.solve(run, WRONG)
        critic = self.worker(lane_a, "pi", "big-pickle", fallback_models=["muse-spark-1.3-contributor-free"])
        result = self.orch("worker", run, "a", "--role", "critic", "--worker-id", critic, ok=False)
        self.assertIn("family", result.stderr)
        self.orch("worker", run, "a", "--role", "critic", "--worker-id", critic, "--allow-same-family")

    def test_critique_records_fallbacks(self):
        run = self.init("D")
        lane_a = self.solve(run, WRONG)
        fallbacks = [{"model": "mimo-v2.6-flash-free", "failure_kind": "timeout", "error": "turn timed out"}]
        self.critique(run, lane_a, model="big-pickle", fallback_models=["big-pickle"], fallbacks=fallbacks)
        event = next(e["data"] for e in self.events(run) if e["type"] == "critique.completed")
        self.assertEqual(event["model"], "big-pickle")
        self.assertEqual(event["fallbacks"], fallbacks)


    def test_lane_engine_defaults_to_config_default_engine(self):
        run = self.init("D")
        config = self.base / "config.json"
        config.write_text(json.dumps({"default_engine": "pi"}))
        self.env["CLAIVE_CONFIG"] = str(config)
        self.orch("lane", run, "a", "--model", "big-pickle")
        explicit = self.init("D")
        self.orch("lane", explicit, "a", "--engine", "muse", "--model", MUSE)
        engines = []
        for run_id in (run, explicit):
            events = (self.registry / "runs" / run_id / "events.jsonl").read_text().splitlines()
            engines += [event["data"]["engine"] for event in map(json.loads, events) if event["type"] == "lane.added"]
        self.assertEqual(engines, ["pi", "muse"])


if __name__ == "__main__":
    unittest.main()
