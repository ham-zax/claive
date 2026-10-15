"""One-time reusable-turn incident notes and provider overflow classification."""
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
from claivelib import cli
from claivelib.engines.claude import ClaudeEngine


class ReusableIncidentNoteChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="claive-incidents-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.registry = self.root / "registry"
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.captured = self.root / "captured-prompts.txt"
        self.env = dict(os.environ, CLAIVE_DIR=str(self.registry), TERM="dumb",
                        FIXTURE_WORKER_BINARY=FIXTURE_WORKER,
                        FIXTURE_PROMPT_OUT=str(self.captured))
        self.processes = []

    def tearDown(self):
        if self.registry.exists():
            for folder in self.registry.iterdir():
                if not (folder / "state.json").exists():
                    continue
                state = json.loads((folder / "state.json").read_text())
                for field in ("worker", "supervisor"):
                    pid = state.get(field + "_pid")
                    if pid and module.identity(pid) == state.get(field + "_identity"):
                        try:
                            os.killpg(pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=5)

    def fixture(self, *args, mode="success"):
        return subprocess.run([FIXTURE_CLI, *args], env=dict(self.env, FIXTURE_MODE=mode),
                              text=True, capture_output=True, timeout=15)

    def state(self, job):
        return json.loads((self.registry / job / "state.json").read_text())

    def until(self, job, predicate, what):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            state = self.state(job)
            if predicate(state):
                return state
            time.sleep(0.025)
        self.fail(f"timed out waiting for {what}: {self.state(job)}")

    def open_reusable(self, prompt):
        before = set(self.registry.glob("*/state.json"))
        process = subprocess.Popen(
            [FIXTURE_CLI, "open", "--engine", "fixture", "--workspace", str(self.workspace),
             "--prompt-file", str(prompt)],
            env=dict(self.env, FIXTURE_MODE="success"), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
        )
        self.processes.append(process)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            created = set(self.registry.glob("*/state.json")) - before
            if created:
                return process, json.loads(next(iter(created)).read_text())["id"]
            time.sleep(0.025)
        self.fail("reusable worker did not start")

    def test_note_formats_failed_cancelled_timed_out_and_is_bounded(self):
        cases = (
            (dict(status="idle", last_turn_status="failed", failure_kind="worker"), "failed (worker)"),
            (dict(status="idle", last_turn_status="failed", failure_kind="timeout"), "failed (timeout)"),
            (dict(status="idle", last_turn_status="cancelled"), "cancelled (cancelled)"),
            (dict(status="cancelled"), "cancelled (cancelled)"),
            (dict(status="idle", last_turn_status="timed_out", failure_kind="timeout"),
             "timed_out (timeout)"),
        )
        for fields, outcome in cases:
            with self.subTest(outcome=outcome):
                state = dict(turn=4, error="provider failure", **fields)
                note = cli.previous_turn_incident(state)
                self.assertEqual(note, f"[claive] The previous turn (4) ended {outcome}: "
                                 "provider failure. This message did not cause it.")

        long = cli.previous_turn_incident(dict(
            turn=4, status="idle", last_turn_status="failed", failure_kind="worker", error="x" * 205,
        ))
        self.assertEqual(len(long.split(": ", 1)[1].split(". This message", 1)[0]), 200)
        self.assertNotIn("\n", long)

    def test_success_and_already_noted_turn_have_no_incident(self):
        for state in (
            dict(turn=4, status="idle", last_turn_status="completed", error="ok"),
            dict(turn=4, status="idle", last_turn_status="failed", failure_kind="worker",
                 error="failed", incident_noted_turn=4),
        ):
            with self.subTest(state=state):
                self.assertIsNone(cli.previous_turn_incident(state))

    def test_failure_note_starts_the_next_prompt_once(self):
        first = self.root / "first.md"
        first.write_text("first assignment\nFIXTURE_MODE=terminal-failure\n")
        process, job = self.open_reusable(first)
        failed = self.until(job, lambda state: state.get("status") == "idle"
                            and state.get("last_turn_status") == "failed", "failed first turn")
        self.assertEqual(failed["failure_kind"], "worker")

        followup = self.root / "followup.md"
        followup.write_text("second assignment\nFIXTURE_MODE=success\n")
        queued = self.fixture("followup", job, "--prompt-file", str(followup))
        self.assertEqual(queued.returncode, 0, queued.stderr)
        second = self.until(job, lambda state: state.get("status") == "idle" and state.get("turn") == 2,
                            "second turn")
        self.assertEqual(second["incident_noted_turn"], 1)
        prompts = self.captured.read_text().split("\n----\n")
        note = prompts[1].splitlines()[0]
        self.assertRegex(note, r"^\[claive\] The previous turn \(1\) ended failed \(worker\): .+$")
        self.assertIn("fixture terminal failure", note)
        self.assertIn("This message did not cause it.", note)

        third = self.root / "third.md"
        third.write_text("third assignment\n")
        queued = self.fixture("followup", job, "--prompt-file", str(third))
        self.assertEqual(queued.returncode, 0, queued.stderr)
        self.until(job, lambda state: state.get("status") == "idle" and state.get("turn") == 3,
                   "third turn")
        prompts = self.captured.read_text().split("\n----\n")
        self.assertFalse(prompts[2].startswith("[claive] The previous turn"))
        self.assertEqual(self.fixture("close", job).returncode, 0)
        process.communicate(timeout=10)

    def test_context_overflow_failure_kind_uses_provider_reason(self):
        prompt = self.root / "context.md"
        prompt.write_text("context test\nFIXTURE_MODE=context-failure\n")
        result = self.fixture("run", "--engine", "fixture", "--workspace", str(self.workspace),
                              "--prompt-file", str(prompt))
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        job = re.search(r"Worker ([0-9a-f]{12})", result.stdout).group(1)
        state = self.state(job)
        self.assertEqual(state["terminal_reason"],
                         "Autocompact is thrashing: the context refilled to the limit within 3 turns "
                         "of the previous compact, 3 times in a row.")
        self.assertEqual(state["failure_kind"], "context_overflow")

    def test_context_overflow_reason_matches_only_known_provider_wording(self):
        for reason in (
            "Autocompact is thrashing: the context refilled to the limit within 3 turns of the previous "
            "compact, 3 times in a row.",
            "context window exceeded", "context length exceeded", "context_length_exceeded",
            "maximum context reached", "prompt is too long",
        ):
            with self.subTest(reason=reason):
                self.assertTrue(cli.context_overflow_reason(reason))
        self.assertFalse(cli.context_overflow_reason("authentication failed"))

    def test_context_file_error_stays_a_worker_failure(self):
        prompt = self.root / "context-file.md"
        prompt.write_text("context file test\nFIXTURE_MODE=context-file-error\n")
        result = self.fixture("run", "--engine", "fixture", "--workspace", str(self.workspace),
                              "--prompt-file", str(prompt))
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        job = re.search(r"Worker ([0-9a-f]{12})", result.stdout).group(1)
        state = self.state(job)
        self.assertEqual(state["terminal_reason"], "cannot read context file")
        self.assertEqual(state["failure_kind"], "worker")

    def test_claude_success_result_with_error_flag_keeps_error_text_as_terminal_reason(self):
        message = ("Autocompact is thrashing: the context refilled to the limit within 3 turns of the "
                   "previous compact, 3 times in a row.")
        event = {"type": "result", "subtype": "success", "is_error": True, "result": message}
        normalized = ClaudeEngine().normalize_event(event)
        state = {}
        cli.event_update(state, normalized[0])
        self.assertEqual(state["terminal_reason"], message)
        self.assertTrue(cli.context_overflow_reason(state["terminal_reason"]))


if __name__ == "__main__":
    unittest.main()
