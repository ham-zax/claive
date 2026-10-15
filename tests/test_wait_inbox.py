"""Acceptance checks for `wait --inbox CONSUMER` and `wait --json`.

Spec: docs/design-messaging-and-roles.md, feature 2 (event-based wait).
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


class WaitInboxChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="claive-wait-inbox-")
        self.path = Path(self.temp.name)
        self.registry = self.path / "registry"
        self.env = dict(os.environ, CLAIVE_DIR=str(self.registry), TERM="dumb",
                        FIXTURE_WORKER_BINARY=FIXTURE_WORKER, FIXTURE_MODE="success",
                        FIXTURE_PROMPT_OUT=str(self.path / "captured.txt"),
                        FIXTURE_ENV_OUT=str(self.path / "env.jsonl"))
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

    def unread(self, consumer):
        return json.loads(self.cli("inbox", "--consumer", consumer, "--json").stdout)

    def cursor(self, consumer):
        return (self.registry / "inbox-cursors" / consumer).read_text()

    def test_inbox_only_wait_returns_on_appended_event_and_advances_cursor(self):
        # The wait starts first, so the event is appended while it polls.
        waiting = subprocess.Popen([FIXTURE_CLI, "wait", "--inbox", "ops", "--json", "--timeout", "30"],
                                   env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        _, job = self.launch("run")
        out, err = waiting.communicate(timeout=30)
        self.assertEqual(waiting.returncode, 0, out + err)
        result = json.loads(out)
        self.assertEqual(set(result), {"event", "worker", "code", "inbox_events"})
        self.assertEqual(result["event"], "inbox")
        self.assertIsNone(result["worker"])
        self.assertEqual(result["code"], 0)
        self.assertEqual([event["id"] for event in result["inbox_events"]], [job])
        self.assertEqual(self.unread("ops"), [], "the wait must advance the consumer cursor")
        self.assertEqual([event["id"] for event in self.unread("other")], [job], "other consumers keep their cursor")

    def test_worker_that_settles_first_still_wins_with_its_own_code(self):
        failing = self.prompt_file("failing", "FIXTURE_MODE=terminal-failure")
        result, job = self.launch("start", prompt=failing)
        self.assertEqual(result.returncode, 0, result.stderr)
        waited = self.cli("wait", job, "--inbox", "ops", "--json", timeout=30)
        self.assertEqual(waited.returncode, 1, waited.stdout + waited.stderr)
        self.assertEqual(json.loads(waited.stdout), {"event": "worker", "worker": job, "code": 1, "inbox_events": []})
        self.assertIn(job, waited.stderr, "the human report belongs on stderr under --json")
        # The worker's own turn event is not consumed by the wait that reported the worker.
        self.assertEqual([event["id"] for event in self.unread("ops")], [job])

    def test_human_report_and_stderr_logs_strip_controls_without_changing_stored_text(self):
        summary = "summary before\x1b[2J after café"
        answer = ("start\x1b[2Jend\x07 bell \x9b2J c1\n"
                  f"```claive-report\n{{\"status\":\"done\",\"summary\":{json.dumps(summary)}}}\n```\n")
        answer_file = self.prompt_file("answer", answer)
        started, job = self.launch("run", "--report", FIXTURE_TEXT_FILE=str(answer_file))
        self.assertEqual(started.returncode, 0, started.stdout + started.stderr)

        waited = self.cli("wait", job)
        self.assertEqual(waited.returncode, 0, waited.stdout + waited.stderr)
        self.assertNotIn("\x1b", waited.stdout)
        self.assertNotIn("\x07", waited.stdout)
        self.assertNotIn("\x9b", waited.stdout)
        self.assertIn("Report: summary before after café", waited.stdout)
        self.assertIn("startend bell  c1", waited.stdout)

        shown = self.cli("show", job)
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertNotIn("\x1b", shown.stdout)
        state = json.loads(self.cli("show", job, "--json").stdout)
        self.assertEqual(state["report"]["summary"], summary)
        result_file = self.registry / job / "result.txt"
        self.assertEqual(result_file.read_text(), answer)

        stderr_file = self.registry / job / "stderr.log"
        stderr = "stderr before\x1b[2J after\x07\n"
        stderr_file.write_text(stderr)
        logged = self.cli("logs", job, "--stderr")
        self.assertEqual(logged.returncode, 0, logged.stderr)
        self.assertEqual(logged.stdout, "stderr before after\n")
        self.assertEqual(stderr_file.read_text(), stderr)

    def test_human_inbox_outputs_strip_controls_but_json_and_file_keep_them(self):
        label = "worker before\x1b[2J after\x07 café"
        started, job = self.launch("run", "--label", label)
        self.assertEqual(started.returncode, 0, started.stdout + started.stderr)

        human = self.cli("inbox", "--consumer", "ansi-human", "--peek")
        self.assertEqual(human.returncode, 0, human.stderr)
        self.assertNotIn("\x1b", human.stdout)
        self.assertNotIn("\x07", human.stdout)
        self.assertIn("worker before after café", human.stdout)

        structured = self.cli("inbox", "--consumer", "ansi-json", "--peek", "--json")
        self.assertEqual(json.loads(structured.stdout)[0]["label"], label)
        event = next(json.loads(line) for line in (self.registry / "inbox.jsonl").read_text().splitlines()
                     if json.loads(line).get("id") == job)
        self.assertEqual(event["label"], label)

        waited = self.cli("wait", "--inbox", "ansi-wait", "--timeout", "2")
        self.assertEqual(waited.returncode, 0, waited.stdout + waited.stderr)
        self.assertNotIn("\x1b", waited.stdout)
        self.assertNotIn("\x07", waited.stdout)
        self.assertIn("worker before after café", waited.stdout)

    def test_ask_event_from_the_inbox_exits_3(self):
        ask = self.prompt_file("ask", "FIXTURE_MODE=ask")
        asked, job = self.launch("run", "--report", prompt=ask)
        self.assertEqual(asked.returncode, 3, asked.stdout + asked.stderr)
        waited = self.cli("wait", "--inbox", "ops", "--json", timeout=30)
        self.assertEqual(waited.returncode, 3, waited.stdout + waited.stderr)
        result = json.loads(waited.stdout)
        self.assertEqual(result["event"], "inbox")
        self.assertEqual(result["code"], 3)
        self.assertEqual([event["id"] for event in result["inbox_events"]], [job])
        self.assertEqual(result["inbox_events"][0]["needs_parent"]["question"], "Design A or B?")
        self.assertEqual(self.unread("ops"), [])

        # Text output matches `claive inbox`, and the exit code is still 3.
        again, second = self.launch("run", "--report", prompt=self.prompt_file("ask-2", "FIXTURE_MODE=ask"))
        self.assertEqual(again.returncode, 3, again.stdout + again.stderr)
        text = self.cli("wait", "--inbox", "ops", timeout=30)
        self.assertEqual(text.returncode, 3, text.stdout + text.stderr)
        self.assertRegex(text.stdout, rf"{second} ASK code=3 .*question: Design A or B\?")

    def test_timeout_returns_124_and_does_not_move_the_cursor(self):
        _, job = self.launch("run")
        # Catch the consumer up first: a timeout only happens when nothing is unread.
        self.assertEqual([event["id"] for event in self.unread("ops")], [job])
        before = self.cursor("ops")

        began = time.time()
        timed = self.cli("wait", "--inbox", "ops", "--timeout", "0.5", "--json")
        self.assertEqual(timed.returncode, 124, timed.stdout + timed.stderr)
        self.assertLess(time.time() - began, 10)
        self.assertEqual(json.loads(timed.stdout), {"event": "timeout", "worker": None, "code": 124, "inbox_events": []})
        self.assertEqual(self.cursor("ops"), before)

        _, slow = self.launch("start", FIXTURE_MODE="slow")
        timed = self.cli("wait", slow, "--inbox", "ops", "--timeout", "0.5")
        self.assertEqual(timed.returncode, 124, timed.stdout + timed.stderr)
        self.assertIn(f"still running: {slow}", timed.stdout)
        self.assertEqual(self.cursor("ops"), before)
        self.assertEqual(self.cli("cancel", slow).returncode, 0)
        self.assertEqual(self.cli("wait", slow, timeout=30).returncode, 130)

    def test_misuse_keeps_the_existing_errors(self):
        bare = self.cli("wait")
        self.assertEqual(bare.returncode, 2)
        self.assertIn("the following arguments are required: id", bare.stderr)
        self.assertEqual(self.cli("wait", "--inbox", "../bad").returncode, 1)
        _, first = self.launch("run")
        _, second = self.launch("run")
        misuse = self.cli("wait", first, second, "--inbox", "ops")
        self.assertEqual(misuse.returncode, 1)
        self.assertIn("use --any", misuse.stderr)
        self.assertEqual(self.cli("wait", "--inbox", "ops", "--timeout", "0").returncode, 1)


if __name__ == "__main__":
    unittest.main()
