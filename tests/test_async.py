"""Acceptance checks for detached workers, the inbox, wait --any, batches and missions.

Spec: docs/superpowers/specs/2026-10-06-async-workers.md
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

from test_workers import FIXTURE_CLI, FIXTURE_WORKER, module


class AsyncChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="claive-async-")
        self.path = Path(self.temp.name)
        self.registry = self.path / "registry"
        self.captured = self.path / "captured.txt"
        self.env_out = self.path / "env.jsonl"
        self.env = dict(os.environ, CLAIVE_DIR=str(self.registry), TERM="dumb",
                        FIXTURE_WORKER_BINARY=FIXTURE_WORKER, FIXTURE_MODE="success",
                        FIXTURE_PROMPT_OUT=str(self.captured), FIXTURE_ENV_OUT=str(self.env_out))
        for name in ("CLAIVE_WORKER_ID", "CLAIVE_ALLOW_NESTED", "CLAIVE_MISSION", "CLAIVE_NOTIFY_CMD", "FIXTURE_TEXT_FILE"):
            self.env.pop(name, None)
        self.prompt = self.prompt_file("task", "PLAIN TASK")

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

    def prompt_file(self, name, text):
        path = self.path / f"{name}.md"
        path.write_text(text)
        return path

    def cli(self, *args, timeout=30, **env):
        return subprocess.run([FIXTURE_CLI, *args], env=dict(self.env, **env), text=True,
                              capture_output=True, timeout=timeout)

    def launch(self, action, *extra, prompt=None, **env):
        result = self.cli(action, "--engine", "fixture", "--workspace", str(self.path),
                          "--prompt-file", str(prompt or self.prompt), *extra, **env)
        found = re.search(r"Worker ([0-9a-f]{12})", result.stdout)
        return result, found.group(1) if found else None

    def state(self, job):
        return json.loads((self.registry / job / "state.json").read_text())

    def eventually(self, predicate, message, seconds=15):
        deadline = time.time() + seconds
        while time.time() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.05)
        self.fail(message)

    def workers(self):
        return json.loads(self.cli("list", "--json").stdout)

    def plan(self, lanes, **extra):
        path = self.path / f"plan-{time.time_ns()}.json"
        path.write_text(json.dumps(dict(workspace=str(self.path), defaults={"engine": "fixture"},
                                        lanes=lanes, **extra)))
        return path

    def batch_state(self, batch):
        return json.loads(self.cli("batch", "status", batch, "--json").stdout)

    def stages(self, batch):
        state = self.batch_state(batch)
        return {f"{lane['key']}.{stage['key']}": stage for lane in state["lanes"] for stage in lane["stages"]}

    def start_batch(self, plan):
        result = self.cli("batch", "start", str(plan))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        found = re.search(r"Batch ([0-9a-f]{12})", result.stdout)
        self.assertIsNotNone(found, result.stdout)
        return found.group(1)

    # A. detach -----------------------------------------------------------------
    def test_open_detach_returns_and_stays_reusable(self):
        began = time.time()
        result, job = self.launch("open", "--detach", FIXTURE_MODE="brief")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertLess(time.time() - began, 5)
        self.assertIsNotNone(job, result.stdout)
        self.assertTrue(self.state(job)["reusable"])
        self.assertEqual(self.cli("wait", job).returncode, 0)
        follow = self.prompt_file("follow", "FOLLOW UP TEXT")
        self.assertEqual(self.cli("followup", job, "--prompt-file", str(follow)).returncode, 0)
        self.assertEqual(self.cli("wait", job).returncode, 0)
        self.assertEqual(self.state(job)["turn"], 2)
        self.assertIn("FOLLOW UP TEXT", self.captured.read_text())
        self.assertEqual(self.cli("close", job).returncode, 0)
        self.eventually(lambda: self.state(job)["status"] not in {"idle", "running"}, "worker did not close")

    # B. recursion guard ------------------------------------------------------------
    def test_workers_get_their_id_and_cannot_launch_workers(self):
        result, job = self.launch("run")
        self.assertEqual(result.returncode, 0, result.stderr)
        recorded = [json.loads(line) for line in self.env_out.read_text().splitlines()]
        self.assertEqual(recorded[-1]["CLAIVE_WORKER_ID"], job)

        before = len(self.workers())
        for action in ("run", "start", "open"):
            refused, _ = self.launch(action, CLAIVE_WORKER_ID="0123456789ab")
            self.assertEqual(refused.returncode, 1, action)
            self.assertIn("claive workers may not launch workers", refused.stderr)
        plan = self.plan([{"key": "a", "stages": [{"key": "s", "prompt_file": str(self.prompt)}]}])
        refused = self.cli("batch", "start", str(plan), CLAIVE_WORKER_ID="0123456789ab")
        self.assertEqual(refused.returncode, 1)
        self.assertIn("claive workers may not launch workers", refused.stderr)
        self.assertEqual(len(self.workers()), before)

        allowed, _ = self.launch("run", CLAIVE_WORKER_ID="0123456789ab", CLAIVE_ALLOW_NESTED="1")
        self.assertEqual(allowed.returncode, 0, allowed.stderr)

    # C. inbox ------------------------------------------------------------------------
    def test_inbox_records_turns_with_per_consumer_cursors(self):
        _, done = self.launch("run")
        ask_prompt = self.prompt_file("ask", "FIXTURE_MODE=ask")
        asked, ask = self.launch("run", "--report", prompt=ask_prompt)
        self.assertEqual(asked.returncode, 3, asked.stdout + asked.stderr)
        failed, fail = self.launch("run", FIXTURE_MODE="terminal-failure")
        self.assertNotEqual(failed.returncode, 0)

        events = json.loads(self.cli("inbox", "--json").stdout)
        self.assertEqual([event["id"] for event in events], [done, ask, fail])
        self.assertEqual([event["code"] for event in events], [0, 3, 1])
        self.assertEqual([event["status"] for event in events], ["completed", "completed", "failed"])
        self.assertTrue(all(event["type"] == "turn" for event in events))
        self.assertEqual(events[1]["needs_parent"]["question"], "Design A or B?")
        self.assertEqual(events[2]["failure_kind"], "worker")
        self.assertIsNone(events[0]["needs_parent"])
        self.assertEqual(events[0]["turn"], 1)

        again = self.cli("inbox")
        self.assertEqual(again.returncode, 0)
        self.assertIn("No new events", again.stdout)

        other = self.cli("inbox", "--consumer", "codex-1", "--peek")
        self.assertRegex(other.stdout, rf"{ask} ASK code=3 .*question: Design A or B\?")
        self.assertRegex(other.stdout, rf"{fail} FAILED code=1 .*kind=worker")
        self.assertRegex(other.stdout, rf"{done} COMPLETED code=0")
        peeked = json.loads(self.cli("inbox", "--consumer", "codex-1", "--json").stdout)
        self.assertEqual(len(peeked), 3, "--peek must not advance the cursor")
        self.assertEqual(json.loads(self.cli("inbox", "--consumer", "codex-1", "--json").stdout), [])

        _, later = self.launch("run")
        self.assertEqual([event["id"] for event in json.loads(self.cli("inbox", "--json").stdout)], [later])
        self.assertEqual(self.cli("inbox", "--consumer", "../bad").returncode, 1)

    def test_notify_command_receives_event_and_failures_are_ignored(self):
        sink = self.path / "notified.json"
        result, job = self.launch("run", CLAIVE_NOTIFY_CMD=f"cat > {sink}; echo \"$CLAIVE_EVENT_CODE\" > {sink}.code")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.eventually(lambda: sink.exists() and sink.read_text().strip(), "notify command did not run")
        self.assertEqual(json.loads(sink.read_text())["id"], job)
        self.eventually(lambda: Path(f"{sink}.code").exists() and Path(f"{sink}.code").read_text().strip() == "0",
                        "notify env missing")
        broken, job = self.launch("run", CLAIVE_NOTIFY_CMD="exit 9")
        self.assertEqual(broken.returncode, 0, broken.stderr)
        self.assertEqual(self.state(job)["status"], "completed")

    # D. wait --any ----------------------------------------------------------------------
    def test_wait_any_and_timeout(self):
        _, slow = self.launch("start", FIXTURE_MODE="slow")
        _, brief = self.launch("start", FIXTURE_MODE="brief")
        misuse = self.cli("wait", slow, brief)
        self.assertEqual(misuse.returncode, 1)
        self.assertIn("use --any", misuse.stderr)

        first = self.cli("wait", "--any", slow, brief, timeout=30)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertIn(brief, first.stdout)

        began = time.time()
        timed = self.cli("wait", slow, "--timeout", "0.5")
        self.assertEqual(timed.returncode, 124, timed.stdout + timed.stderr)
        self.assertLess(time.time() - began, 10)
        self.assertIn(f"still running: {slow}", timed.stdout)
        self.assertEqual(self.cli("wait", "--any", slow, "--timeout", "0").returncode, 1)
        self.assertEqual(self.cli("wait", "--any", slow, "ffffffffffff", "--timeout", "1").returncode, 1)
        self.cli("cancel", slow)
        self.assertEqual(self.cli("wait", slow, timeout=30).returncode, 130)

    # E. batches -----------------------------------------------------------------------------
    def test_batch_lanes_context_and_outcomes(self):
        first = self.prompt_file("a1", "LANE A FIRST")
        second = self.prompt_file("a2", "LANE A SECOND")
        broken = self.prompt_file("b1", "FIXTURE_MODE=terminal-failure")
        never = self.prompt_file("b2", "NEVER RUNS")
        ask = self.prompt_file("c1", "FIXTURE_MODE=ask")
        plan = self.plan([
            {"key": "a", "stages": [{"key": "s1", "prompt_file": str(first)},
                                    {"key": "s2", "prompt_file": str(second), "context": "previous"}]},
            {"key": "b", "stages": [{"key": "s1", "prompt_file": str(broken)},
                                    {"key": "s2", "prompt_file": str(never)}]},
            {"key": "c", "stages": [{"key": "s1", "prompt_file": str(ask), "report": True}]},
        ], label="mixed")
        self.assertIn("Plan ok: 3 lanes, 5 stages", self.cli("batch", "validate", str(plan)).stdout)
        batch = self.start_batch(plan)
        waited = self.cli("batch", "wait", batch, timeout=60)
        self.assertEqual(waited.returncode, 1, waited.stdout + waited.stderr)

        stages = self.stages(batch)
        self.assertEqual({key: stage["status"] for key, stage in stages.items()},
                         {"a.s1": "done", "a.s2": "done", "b.s1": "failed", "b.s2": "skipped",
                          "c.s1": "needs_parent"})
        self.assertIsNone(stages["b.s2"]["worker"])
        self.assertEqual(self.batch_state(batch)["status"], "failed")
        worker = self.state(stages["a.s2"]["worker"])
        self.assertEqual(worker["batch"], batch)
        self.assertEqual(worker["batch_stage"], "a.s2")
        turns = self.captured.read_text()
        self.assertIn("Context from previous stage a.s1:", turns)
        self.assertRegex(turns, r"Context from previous stage a\.s1:\n[\s\S]*fixture result[\s\S]*LANE A SECOND")
        self.assertNotIn("NEVER RUNS", turns)
        self.assertEqual(len(self.workers()), 4, "batch dirs must not be listed as workers")

        status = self.cli("batch", "status", batch).stdout
        self.assertRegex(status, r"b\.s2 SKIPPED -")
        self.assertRegex(status, r"c\.s1 NEEDS_PARENT [0-9a-f]{12} code=3")
        listed = self.cli("batch", "list", "--json")
        self.assertEqual([item["id"] for item in json.loads(listed.stdout)], [batch])
        events = json.loads(self.cli("inbox", "--json").stdout)
        batch_events = [event for event in events if event["type"] == "batch"]
        self.assertEqual(len(batch_events), 1)
        self.assertEqual((batch_events[0]["id"], batch_events[0]["status"], batch_events[0]["code"]),
                         (batch, "failed", 1))
        self.assertEqual(sum(event["type"] == "turn" and event["batch"] == batch for event in events), 4)

    def test_batch_needs_parent_only_exits_3(self):
        ask = self.prompt_file("ask", "FIXTURE_MODE=ask")
        batch = self.start_batch(self.plan([
            {"key": "ok", "stages": [{"key": "s", "prompt_file": str(self.prompt)}]},
            {"key": "q", "stages": [{"key": "s", "prompt_file": str(ask), "role": "reviewer",
                                     "engine": "fixture", "model": "fixture-model"}]}]))
        self.assertEqual(self.cli("batch", "wait", batch, timeout=60).returncode, 3)
        self.assertEqual(self.batch_state(batch)["status"], "needs_parent")
        worker = self.state(self.stages(batch)["q.s"]["worker"])
        self.assertEqual(worker["role"], "reviewer")
        self.assertTrue(worker["launch"]["read_only"])
        self.assertTrue(worker["report_contract"])

    def test_batch_validation_rejects_before_launching(self):
        good = str(self.prompt)
        cases = {
            "duplicate stage": [{"key": "a", "stages": [{"key": "s", "prompt_file": good},
                                                         {"key": "s", "prompt_file": good}]}],
            "duplicate lane": [{"key": "a", "stages": [{"key": "s", "prompt_file": good}]},
                               {"key": "a", "stages": [{"key": "t", "prompt_file": good}]}],
            "relative prompt": [{"key": "a", "stages": [{"key": "s", "prompt_file": "task.md"}]}],
            "first context": [{"key": "a", "stages": [{"key": "s", "prompt_file": good, "context": "previous"}]}],
            "bad role": [{"key": "a", "stages": [{"key": "s", "prompt_file": good, "role": "wizard"}]}],
            "disallowed model": [{"key": "a", "stages": [{"key": "s", "prompt_file": good,
                                                          "model": "nemotron-3-super-free"}]}],
            "unknown option": [{"key": "a", "stages": [{"key": "s", "prompt_file": good, "colour": "red"}]}],
            "bad key": [{"key": "A B", "stages": [{"key": "s", "prompt_file": good}]}],
            "too many lanes": [{"key": f"l{i}", "stages": [{"key": "s", "prompt_file": good}]} for i in range(17)],
            "too many stages": [{"key": "a", "stages": [{"key": f"s{i}", "prompt_file": good} for i in range(9)]}],
            "no lanes": [],
        }
        for name, lanes in cases.items():
            with self.subTest(name):
                plan = self.plan(lanes)
                self.assertEqual(self.cli("batch", "validate", str(plan)).returncode, 1)
                self.assertEqual(self.cli("batch", "start", str(plan)).returncode, 1)
        self.assertEqual(self.workers(), [])
        self.assertEqual(json.loads(self.cli("batch", "list", "--json").stdout), [])

    def test_batch_retry_keeps_done_stages(self):
        flaky = self.prompt_file("flaky", "FIXTURE_MODE=terminal-failure")
        batch = self.start_batch(self.plan([
            {"key": "a", "stages": [{"key": "s1", "prompt_file": str(self.prompt)},
                                    {"key": "s2", "prompt_file": str(flaky), "context": "previous"}]}]))
        self.assertEqual(self.cli("batch", "wait", batch, timeout=60).returncode, 1)
        before = self.stages(batch)
        flaky.write_text("NOW FINE")
        retried = self.cli("batch", "retry", batch)
        self.assertEqual(retried.returncode, 0, retried.stderr)
        self.assertIn("retrying 1 stages", retried.stdout)
        self.assertEqual(self.cli("batch", "wait", batch, timeout=60).returncode, 0)
        after = self.stages(batch)
        self.assertEqual(after["a.s1"]["worker"], before["a.s1"]["worker"])
        self.assertNotEqual(after["a.s2"]["worker"], before["a.s2"]["worker"])
        self.assertEqual(after["a.s2"]["status"], "done")
        self.assertEqual(self.captured.read_text().count("PLAIN TASK"), 1, "done stage must not re-run")
        self.assertRegex(self.captured.read_text(), r"Context from previous stage a\.s1:[\s\S]*NOW FINE")

    def test_batch_cancel(self):
        slow = self.prompt_file("slow", "FIXTURE_MODE=slow")
        batch = self.start_batch(self.plan([
            {"key": "a", "stages": [{"key": "s1", "prompt_file": str(slow)},
                                    {"key": "s2", "prompt_file": str(self.prompt)}]}]))
        self.eventually(lambda: self.stages(batch)["a.s1"]["status"] == "running", "stage did not start")
        self.assertEqual(self.cli("batch", "retry", batch).returncode, 1)
        timed = self.cli("batch", "wait", batch, "--timeout", "0.5")
        self.assertEqual(timed.returncode, 124)
        cancelled = self.cli("batch", "cancel", batch)
        self.assertEqual(cancelled.returncode, 0, cancelled.stderr)
        self.assertEqual(self.cli("batch", "wait", batch, timeout=60).returncode, 130)
        stages = self.stages(batch)
        self.assertEqual(stages["a.s2"]["status"], "skipped")
        self.assertIn(stages["a.s1"]["status"], {"cancelled", "failed"})
        self.assertEqual(self.batch_state(batch)["status"], "cancelled")

    # F. missions ---------------------------------------------------------------------------------
    def test_mission_links_and_next_action(self):
        created = self.cli("mission", "new", "--title", "Ship parser", "--goal", "Parser with tests")
        self.assertEqual(created.returncode, 0, created.stderr)
        mission = re.search(r"Mission ([0-9a-f]{12})", created.stdout).group(1)

        _, done = self.launch("run", "--mission", mission)
        ask_prompt = self.prompt_file("ask", "FIXTURE_MODE=ask")
        _, ask = self.launch("run", "--report", prompt=ask_prompt, CLAIVE_MISSION=mission)
        self.assertEqual(self.state(done)["mission"], mission)
        self.assertEqual(self.cli("mission", "note", mission, "Chose design A for now").returncode, 0)

        shown = json.loads(self.cli("mission", "show", mission, "--json").stdout)
        self.assertEqual(shown["title"], "Ship parser")
        self.assertEqual(shown["goal"], "Parser with tests")
        self.assertEqual([(link["kind"], link["id"]) for link in shown["links"]],
                         [("worker", done), ("worker", ask)])
        self.assertEqual(shown["next"], f"answer {ask}: Design A or B?")
        text = self.cli("mission", "show", mission).stdout
        self.assertIn("Chose design A for now", text)
        self.assertIn(f"Next: answer {ask}: Design A or B?", text)

        second = re.search(r"Mission ([0-9a-f]{12})",
                           self.cli("mission", "new", "--title", "Two", "--goal", "g").stdout).group(1)
        _, failed = self.launch("run", "--mission", second, FIXTURE_MODE="terminal-failure")
        self.assertEqual(json.loads(self.cli("mission", "show", second, "--json").stdout)["next"],
                         f"inspect failed {failed}")
        third = re.search(r"Mission ([0-9a-f]{12})",
                          self.cli("mission", "new", "--title", "Three", "--goal", "g").stdout).group(1)
        _, ok = self.launch("run", "--mission", third)
        self.assertEqual(self.cli("mission", "link", third, "--worker", ok).returncode, 0)
        shown = json.loads(self.cli("mission", "show", third, "--json").stdout)
        self.assertEqual(len(shown["links"]), 1, "duplicate links are ignored")
        self.assertEqual(shown["next"], "review results, then close the mission")

        count = len(self.workers())
        refused, _ = self.launch("run", "--mission", "ffffffffffff")
        self.assertEqual(refused.returncode, 1)
        self.assertEqual(self.cli("mission", "close", third).returncode, 0)
        refused, _ = self.launch("run", "--mission", third)
        self.assertEqual(refused.returncode, 1)
        self.assertEqual(len(self.workers()), count)
        open_ids = [item["id"] for item in json.loads(self.cli("mission", "list", "--json").stdout)]
        self.assertEqual(sorted(open_ids), sorted([mission, second]))
        all_ids = [item["id"] for item in json.loads(self.cli("mission", "list", "--all", "--json").stdout)]
        self.assertIn(third, all_ids)

    def test_batch_links_to_mission_and_running_next(self):
        mission = re.search(r"Mission ([0-9a-f]{12})",
                            self.cli("mission", "new", "--title", "B", "--goal", "g").stdout).group(1)
        slow = self.prompt_file("slow", "FIXTURE_MODE=slow")
        batch = self.start_batch(self.plan([{"key": "a", "stages": [{"key": "s", "prompt_file": str(slow)}]}],
                                           mission=mission))
        self.eventually(lambda: self.stages(batch)["a.s"]["status"] == "running", "stage did not start")
        shown = json.loads(self.cli("mission", "show", mission, "--json").stdout)
        self.assertEqual([(link["kind"], link["id"]) for link in shown["links"]], [("batch", batch)])
        self.assertEqual(shown["next"], f"wait for {batch}")
        self.cli("batch", "cancel", batch)
        self.assertEqual(self.cli("batch", "wait", batch, timeout=60).returncode, 130)


if __name__ == "__main__":
    unittest.main()
