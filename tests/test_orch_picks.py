"""Acceptance tests: run categories, stats, and dynamic worker picks for claive-orch."""
import hermetic  # noqa: F401  (must run before claivelib reads the environment)
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]
ORCH = str(REPO_ROOT / "bin/claive-orch")
sys.path.insert(0, str(REPO_ROOT / "bin"))
from claivelib import orchestration as orch

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@localhost",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@localhost"}
MUSE = "muse-spark-1.3-contributor"
MIMO, PICKLE, BUNNY, LONGCAT = "mimo-v2.6-flash-free", "big-pickle", "space-bunny-free", "longcat-2.5-preview-free"


def ev(kind, **data):
    return {"type": kind, "time": 0, "data": data}


def config(run_id="r1", arm="D", category=None, experiment=None, task_id="t"):
    return {"run_id": run_id, "repo": "/nonexistent", "base": "base", "task_file": "/nonexistent/task.md",
            "task_id": task_id, "arm": arm, "rounds": 2, "verify": "true", "worker_verify": None,
            "verify_timeout": 60, "score_regex": None, "acceptance_dir": None, "experiment": experiment,
            "repeat": None, "max_minutes": None, "label": task_id, "post_pass_critic": False,
            "setup": None, "cache_key": None, "cache_paths": [], "verify_memory": None, "category": category}


def started(**kwargs):
    return ev("run.started", config=config(**kwargs))


def lane(name, engine="muse", model=MUSE):
    return ev("lane.added", lane=name, engine=engine, model=model, family=orch.model_family(model, engine),
              strategy=None, branch=f"orch/r/{name}", path=f"/w/{name}", base_commit="base", local_paths=[])


def implementer(name="a", engine="muse", model=MUSE):
    return ev("worker.registered", lane=name, role="implementer", worker_id="aaaaaaaaaaaa",
              engine=engine, model=model, family=orch.model_family(model, engine))


def result(score, passed):
    return {"passed": passed, "score": score, "status": "pass" if passed else "fail", "counts": None}


def accepted(name, score, passed=False, improved=True):
    return [ev("verification.completed", lane=name, result=result(score, passed)),
            ev("checkpoint.accepted", lane=name, round=0, result=result(score, passed),
               commit=f"c{score}", improved=improved, diff_lines=3)]


def reverted(name, score):
    return [ev("verification.completed", lane=name, result=result(score, False)),
            ev("checkpoint.reverted", lane=name, round=1, result=result(score, False), to_commit="c",
               best=result(1.0, False), rejected_ref="refs/x")]


def critique(name, round_number, model, defects=1, error=None, post_pass=False):
    data = {"lane": name, "round": round_number, "model": model, "family": orch.model_family(model, "pi"),
            "worker_id": "cccccccccccc", "no_concrete_defect": defects == 0, "approach_sound": True,
            "post_pass": post_pass,
            "defects": [{"location": "x.py:1", "description": "d", "evidence": "e", "localized": True}] * defects}
    if error:
        data["error"] = error
    return ev("critique.completed", **data)


def finished(outcome="verified", name="a"):
    return ev("run.finished", outcome=outcome, lane=name, reason="r")


def counts(**values):
    base = {"critiques": 0, "improved": 0, "no_change": 0, "reverted": 0, "no_defect": 0,
            "error": 0, "unused": 0, "post_pass": 0}
    base.update(values)
    return base


def outcomes(events):
    return [entry["outcome"] for entry in orch.critic_outcomes(events)]


class CriticOutcomes(unittest.TestCase):
    def test_improved_when_the_next_checkpoint_of_the_lane_improves(self):
        events = [started(), lane("a"), implementer(), *accepted("a", 0.5), critique("a", 1, MIMO),
                  *accepted("a", 0.8)]
        entries = orch.critic_outcomes(events)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["outcome"], "improved")
        self.assertEqual((entries[0]["lane"], entries[0]["round"], entries[0]["model"], entries[0]["family"]),
                         ("a", 1, MIMO, "mimo"))

    def test_reverted_and_no_change(self):
        events = [started(), lane("a"), *accepted("a", 0.5), critique("a", 1, MIMO), *reverted("a", 0.2),
                  critique("a", 2, PICKLE), *accepted("a", 0.5, improved=False)]
        self.assertEqual(outcomes(events), ["reverted", "no_change"])

    def test_no_defect_error_and_post_pass_ignore_later_checkpoints(self):
        events = [started(), lane("a"), *accepted("a", 0.5), critique("a", 1, MIMO, defects=0),
                  *accepted("a", 0.9), critique("a", 2, PICKLE, error="unparseable critique: x"),
                  *accepted("a", 1.0, passed=True), critique("a", 3, BUNNY, post_pass=True), *accepted("a", 1.0, True)]
        self.assertEqual(outcomes(events), ["no_defect", "error", "post_pass"])

    def test_unused_when_no_later_checkpoint_in_the_same_lane(self):
        events = [started(), lane("a"), *accepted("a", 0.5), critique("a", 1, MIMO), lane("b", "pi", PICKLE),
                  *accepted("b", 0.9)]
        self.assertEqual(outcomes(events), ["unused"])


class CollectStats(unittest.TestCase):
    def histories(self):
        first = [started(run_id="r1", category="bug-fix"), lane("a"), implementer(), *accepted("a", 0.5),
                 critique("a", 1, MIMO), *accepted("a", 1.0, passed=True), finished("verified")]
        second = [started(run_id="r2"), lane("a"), implementer(), *accepted("a", 0.4),
                  critique("a", 1, MIMO), *reverted("a", 0.1), critique("a", 2, PICKLE, defects=0),
                  critique("a", 2, PICKLE, error="unparseable critique: x"), finished("stalled")]
        third = [started(run_id="r3", category="bug-fix"), lane("a"), implementer(),
                 *accepted("a", 1.0, passed=True), finished("verified")]
        return [first, second, third]

    def test_critics(self):
        critics = orch.collect_stats(self.histories())["critics"]
        self.assertEqual({k: critics[MIMO][k] for k in counts()}, counts(critiques=2, improved=1, reverted=1))
        self.assertEqual(critics[MIMO]["family"], "mimo")
        self.assertEqual(critics[MIMO]["helpful_rate"], 0.5)
        self.assertEqual({k: critics[PICKLE][k] for k in counts()}, counts(critiques=2, no_defect=1, error=1))
        self.assertEqual(critics[PICKLE]["helpful_rate"], 0.0)

    def test_helpful_rate_is_none_without_scored_critiques(self):
        history = [started(), lane("a"), *accepted("a", 0.5), critique("a", 1, BUNNY, error="bad")]
        self.assertIsNone(orch.collect_stats([history])["critics"][BUNNY]["helpful_rate"])

    def test_implementers(self):
        implementers = orch.collect_stats(self.histories())["implementers"]
        row = implementers[f"muse/{MUSE}"]
        self.assertEqual((row["lanes"], row["l0_passed"], row["passed"]), (3, 1, 2))

    def test_categories(self):
        categories = orch.collect_stats(self.histories())["categories"]
        self.assertEqual({k: categories["bug-fix"][k] for k in ("runs", "verified", "l0_passed", "recovered")},
                         {"runs": 2, "verified": 2, "l0_passed": 1, "recovered": 1})
        self.assertEqual({k: categories["uncategorized"][k] for k in ("runs", "verified", "l0_passed", "recovered")},
                         {"runs": 1, "verified": 0, "l0_passed": 0, "recovered": 0})

    def test_running_and_invalid_runs_are_left_out_of_categories(self):
        running = [started(run_id="r4", category="refactor"), lane("a"), *accepted("a", 0.5)]
        invalid = [started(run_id="r5", category="refactor"), finished("invalid")]
        self.assertNotIn("refactor", orch.collect_stats([running, invalid])["categories"])


def scored(improved, total):
    return dict(counts(critiques=total, improved=improved, reverted=total - improved), helpful_rate=improved / total)


class PickCritic(unittest.TestCase):
    def test_cold_start_takes_the_first_roster_model(self):
        choice = orch.pick_critic({}, "muse-spark", seed="r:a:1")
        self.assertEqual((choice["model"], choice["mode"]), (MIMO, "cold-start"))
        self.assertEqual(choice["fallbacks"], [PICKLE, BUNNY])

    def test_never_the_lane_family(self):
        for seed in ("s1", "s2", "s3", "s4"):
            choice = orch.pick_critic({}, "mimo", seed=seed, explore=1.0)
            self.assertNotEqual(choice["model"], MIMO)
            self.assertNotIn(MIMO, choice["fallbacks"])

    def test_cold_start_continues_until_every_model_has_min_uses(self):
        stats = {MIMO: scored(3, 3), PICKLE: scored(0, 1)}
        choice = orch.pick_critic(stats, "muse-spark", seed="x")
        self.assertEqual((choice["model"], choice["mode"]), (PICKLE, "cold-start"))

    def test_exploit_takes_the_best_smoothed_rate(self):
        stats = {MIMO: scored(1, 4), PICKLE: scored(3, 3), BUNNY: scored(2, 4)}
        choice = orch.pick_critic(stats, "muse-spark", seed="x", explore=0.0)
        self.assertEqual((choice["model"], choice["mode"]), (PICKLE, "exploit"))
        self.assertEqual(choice["fallbacks"], [BUNNY, MIMO])
        self.assertAlmostEqual(choice["scores"][PICKLE], 4 / 5)

    def test_explore_is_seeded_and_never_the_best(self):
        stats = {MIMO: scored(1, 4), PICKLE: scored(3, 3), BUNNY: scored(2, 4)}
        first = orch.pick_critic(stats, "muse-spark", seed="run1:a:1", explore=1.0)
        again = orch.pick_critic(stats, "muse-spark", seed="run1:a:1", explore=1.0)
        self.assertEqual(first["mode"], "explore")
        self.assertNotEqual(first["model"], PICKLE)
        self.assertEqual(first, again)

    def test_reserve_only_when_the_roster_is_exhausted(self):
        choice = orch.pick_critic({}, "mimo", seed="x", roster=(MIMO,))
        self.assertEqual(choice["model"], LONGCAT)

    def test_disallowed_models_are_skipped(self):
        choice = orch.pick_critic({}, "muse-spark", seed="x", roster=("nemotron-3-ultra-free", PICKLE))
        self.assertEqual(choice["model"], PICKLE)

    def test_no_candidate_raises(self):
        with self.assertRaises(ValueError):
            orch.pick_critic({}, "mimo", seed="x", roster=(MIMO,), reserve=())


class ImplementerChoice(unittest.TestCase):
    def test_muse_when_available(self):
        choice = orch.implementer_choice(muse_available=True, pi_available=True)
        self.assertEqual((choice["engine"], choice["model"], choice["reasoning_effort"], choice["fallback"]),
                         ("muse", MUSE, "xhigh", False))

    def test_pi_muse_free_when_muse_quota_is_exhausted(self):
        choice = orch.implementer_choice(muse_available=True, pi_available=True,
                                         muse_quota_reset="2099-01-01T00:00:00Z")
        self.assertEqual((choice["engine"], choice["model"], choice["reasoning_effort"], choice["fallback"]),
                         ("pi", "muse-spark-1.3-contributor-free", "max", True))
        self.assertIn("2099-01-01T00:00:00Z", choice["reason"])

    def test_pi_muse_free_when_muse_is_missing(self):
        choice = orch.implementer_choice(muse_available=False, pi_available=True)
        self.assertEqual((choice["engine"], choice["model"], choice["fallback"]),
                         ("pi", "muse-spark-1.3-contributor-free", True))
        self.assertIn("muse", choice["reason"].lower())

    def test_pi_host_default_is_not_a_fallback(self):
        choice = orch.implementer_choice(muse_available=True, pi_available=True, configured_engine="pi")
        self.assertEqual((choice["engine"], choice["model"], choice["fallback"]),
                         ("pi", "muse-spark-1.3-contributor-free", False))

    def test_nothing_available_raises(self):
        with self.assertRaises(ValueError):
            orch.implementer_choice(muse_available=False, pi_available=False)


class BreadthChoice(unittest.TestCase):
    def test_skips_lane_a_and_critic_families(self):
        choice = orch.breadth_choice({}, "muse-spark", {"mimo"})
        self.assertEqual((choice["engine"], choice["model"]), ("pi", PICKLE))
        self.assertEqual(choice["fallbacks"], [BUNNY])

    def test_relaxes_critic_families_before_giving_up(self):
        choice = orch.breadth_choice({}, "muse-spark", {"mimo", "big-pickle", "space-bunny"})
        self.assertEqual(choice["model"], MIMO)

    def test_ranks_by_lane_pass_rate(self):
        stats = {f"pi/{BUNNY}": {"lanes": 3, "l0_passed": 2, "passed": 3},
                 f"pi/{PICKLE}": {"lanes": 3, "l0_passed": 0, "passed": 0}}
        choice = orch.breadth_choice(stats, "muse-spark", {"mimo"})
        self.assertEqual(choice["model"], BUNNY)


class Cli(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.env = dict(os.environ, CLAIVE_DIR=str(self.state), PYTHONDONTWRITEBYTECODE="1")
        self.env.update(GIT_ENV)

    def tearDown(self):
        self.temporary.cleanup()

    def orch(self, *arguments, check=True):
        completed = subprocess.run([sys.executable, ORCH, *arguments], env=self.env, text=True,
                                   capture_output=True, timeout=120)
        if check and completed.returncode != 0:
            self.fail(f"claive-orch {' '.join(arguments)} failed: {completed.stdout}{completed.stderr}")
        return completed

    def write_run(self, run_id, events):
        path = self.state / "runs" / run_id
        path.mkdir(parents=True)
        lines = []
        for seq, event in enumerate(events, 1):
            lines.append(json.dumps(dict(event, seq=seq, time=time.time())))
        (path / "events.jsonl").write_text("\n".join(lines) + "\n")
        return path

    def events(self, path):
        return [json.loads(line) for line in (path / "events.jsonl").read_text().splitlines() if line.strip()]

    def executable(self, name):
        binary = self.root / name
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(0o755)
        return str(binary)

    def repo(self):
        repo = self.root / "repo"
        repo.mkdir()
        (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n")
        (repo / "test_calc.py").write_text("import unittest\nfrom calc import add\n\n\nclass C(unittest.TestCase):\n"
                                           "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n")
        for command in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "base"]):
            subprocess.run(["git", "-C", str(repo), *command], check=True, env=self.env)
        task = self.root / "task.md"
        task.write_text("Fix add.\n")
        return repo, task

    def test_init_records_category_and_list_shows_it(self):
        repo, task = self.repo()
        out = self.orch("init", "--repo", str(repo), "--task-file", str(task), "--verify",
                        "python3 -m unittest -q", "--arm", "D", "--category", "bug-fix").stdout
        run_id = out.split()[1]
        config = self.events(self.state / "runs" / run_id)[0]["data"]["config"]
        self.assertEqual(config["category"], "bug-fix")
        self.assertIn("bug-fix", self.orch("list").stdout)

    def test_init_rejects_an_unknown_category(self):
        repo, task = self.repo()
        completed = self.orch("init", "--repo", str(repo), "--task-file", str(task), "--verify", "false",
                              "--category", "whatever", check=False)
        self.assertNotEqual(completed.returncode, 0)

    def test_init_refuses_post_pass_critic_in_experiment_runs(self):
        repo, task = self.repo()
        completed = self.orch("init", "--repo", str(repo), "--task-file", str(task), "--verify", "false",
                              "--arm", "D", "--experiment", "calib", "--post-pass-critic", check=False)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("post-pass", completed.stderr)
        self.assertFalse((self.state / "runs").exists() and any((self.state / "runs").iterdir()))

    def test_stats_json(self):
        self.write_run("aaaaaaaaaaa1", [started(run_id="aaaaaaaaaaa1", category="bug-fix"), lane("a"), implementer(),
                                        *accepted("a", 0.5), critique("a", 1, MIMO), *accepted("a", 1.0, True),
                                        finished("verified")])
        self.write_run("aaaaaaaaaaa2", [started(run_id="aaaaaaaaaaa2", experiment="calib"), lane("a"),
                                        implementer(), *accepted("a", 1.0, True), finished("verified")])
        data = json.loads(self.orch("stats", "--json").stdout)
        self.assertEqual(set(data), {"critics", "implementers", "categories"})
        self.assertEqual(data["critics"][MIMO]["improved"], 1)
        self.assertEqual(data["implementers"][f"muse/{MUSE}"]["lanes"], 2)
        only = json.loads(self.orch("stats", "--json", "--experiment", "calib").stdout)
        self.assertEqual(only["implementers"][f"muse/{MUSE}"]["lanes"], 1)
        self.assertEqual(only["critics"], {})
        text = self.orch("stats").stdout
        self.assertIn(MIMO, text)
        self.assertIn("bug-fix", text)

    def test_pick_implementer_falls_back_to_pi_without_muse(self):
        self.env.update(MUSE_WORKER_BINARY=str(self.root / "no-muse"), PI_WORKER_BINARY=self.executable("pi"))
        path = self.write_run("bbbbbbbbbbb1", [started(run_id="bbbbbbbbbbb1")])
        choice = json.loads(self.orch("pick", "bbbbbbbbbbb1", "implementer", "--json").stdout)
        self.assertEqual((choice["engine"], choice["model"], choice["fallback"]),
                         ("pi", "muse-spark-1.3-contributor-free", True))
        picks = [e["data"] for e in self.events(path) if e["type"] == "pick.made"]
        self.assertEqual(len(picks), 1)
        self.assertEqual(picks[0]["role"], "implementer")
        self.assertEqual(picks[0]["choice"]["engine"], "pi")
        self.assertEqual(self.orch("next", "bbbbbbbbbbb1").returncode, 0)

    def test_pick_implementer_prefers_muse_and_shows_the_lane_command(self):
        self.env.update(MUSE_WORKER_BINARY=self.executable("muse"), PI_WORKER_BINARY=self.executable("pi"))
        self.write_run("bbbbbbbbbbb2", [started(run_id="bbbbbbbbbbb2")])
        out = self.orch("pick", "bbbbbbbbbbb2", "implementer").stdout
        self.assertIn("claive-orch lane bbbbbbbbbbb2 a --engine muse --model muse-spark-1.3-contributor", out)

    def test_pick_implementer_sees_an_active_muse_quota(self):
        self.env.update(MUSE_WORKER_BINARY=self.executable("muse"), PI_WORKER_BINARY=self.executable("pi"))
        worker = self.state / "dddddddddddd"
        worker.mkdir()
        (worker / "state.json").write_text(json.dumps({
            "schema_version": 2, "id": "dddddddddddd", "engine": "muse", "status": "failed",
            "started_at": time.time() - 60, "ended_at": time.time() - 30, "quota_exhausted": True,
            "quota_reset_at": "2099-01-01T00:00:00Z", "failure_kind": "quota", "workspace": str(self.root),
            "launch": {"model": MUSE}}))
        self.write_run("bbbbbbbbbbb3", [started(run_id="bbbbbbbbbbb3")])
        choice = json.loads(self.orch("pick", "bbbbbbbbbbb3", "implementer", "--json").stdout)
        self.assertEqual((choice["engine"], choice["fallback"]), ("pi", True))
        self.assertIn("2099-01-01T00:00:00Z", choice["reason"])

    def test_pick_implementer_counts_legacy_records_without_an_engine_as_muse(self):
        self.env.update(MUSE_WORKER_BINARY=self.executable("muse"), PI_WORKER_BINARY=self.executable("pi"))
        worker = self.state / "ffffffffffff"
        worker.mkdir()
        (worker / "state.json").write_text(json.dumps({
            "id": "ffffffffffff", "engine": None, "status": "failed", "ended_at": time.time() - 30,
            "quota_exhausted": True, "quota_reset_at": None, "workspace": str(self.root)}))
        self.write_run("bbbbbbbbbbb5", [started(run_id="bbbbbbbbbbb5")])
        choice = json.loads(self.orch("pick", "bbbbbbbbbbb5", "implementer", "--json").stdout)
        self.assertEqual((choice["engine"], choice["fallback"]), ("pi", True))
        self.assertIn("quota hit at", choice["reason"])

    def test_pick_implementer_ignores_an_expired_muse_quota(self):
        self.env.update(MUSE_WORKER_BINARY=self.executable("muse"), PI_WORKER_BINARY=self.executable("pi"))
        worker = self.state / "eeeeeeeeeeee"
        worker.mkdir()
        (worker / "state.json").write_text(json.dumps({
            "schema_version": 2, "id": "eeeeeeeeeeee", "engine": "muse", "status": "failed",
            "started_at": time.time() - 60, "ended_at": time.time() - 30, "quota_exhausted": True,
            "quota_reset_at": "2001-01-01T00:00:00Z", "failure_kind": "quota", "workspace": str(self.root),
            "launch": {"model": MUSE}}))
        self.write_run("bbbbbbbbbbb4", [started(run_id="bbbbbbbbbbb4")])
        choice = json.loads(self.orch("pick", "bbbbbbbbbbb4", "implementer", "--json").stdout)
        self.assertEqual(choice["engine"], "muse")

    def test_pick_critic_records_and_prints_the_launch(self):
        path = self.write_run("ccccccccccc1", [started(run_id="ccccccccccc1"), lane("a"), implementer(),
                                               *accepted("a", 0.5)])
        choice = json.loads(self.orch("pick", "ccccccccccc1", "critic", "--lane", "a", "--json").stdout)
        self.assertEqual((choice["model"], choice["mode"]), (MIMO, "cold-start"))
        picks = [e["data"] for e in self.events(path) if e["type"] == "pick.made"]
        self.assertEqual((picks[-1]["role"], picks[-1]["lane"]), ("critic", "a"))
        out = self.orch("pick", "ccccccccccc1", "critic", "--lane", "a").stdout
        self.assertIn(f"--model {MIMO}", out)
        self.assertIn("--fallback-models big-pickle,space-bunny-free", out)
        self.assertIn("--read-only", out)

    def test_pick_breadth_avoids_lane_a_and_its_critics(self):
        self.write_run("ccccccccccc2", [started(run_id="ccccccccccc2"), lane("a"), implementer(),
                                        *accepted("a", 0.5), critique("a", 1, MIMO, defects=0)])
        choice = json.loads(self.orch("pick", "ccccccccccc2", "breadth", "--json").stdout)
        self.assertEqual((choice["engine"], choice["model"]), ("pi", PICKLE))

    def test_pick_is_refused_in_experiment_runs(self):
        self.write_run("ccccccccccc3", [started(run_id="ccccccccccc3", experiment="calib"), lane("a"),
                                        implementer(), *accepted("a", 0.5)])
        completed = self.orch("pick", "ccccccccccc3", "critic", "--lane", "a", check=False)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("experiment", completed.stderr)


if __name__ == "__main__":
    unittest.main()
