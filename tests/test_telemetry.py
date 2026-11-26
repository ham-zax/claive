"""Hermetic tests for lightweight fail-silent Claive telemetry."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin" / "claive"
sys.path.insert(0, str(ROOT / "bin"))

from claivelib import telemetry


class TelemetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _run_cli(self, *args, extra_env=None):
        env = os.environ.copy()
        env["CLAIVE_DIR"] = str(self.base)
        env.pop("CLAIVE_TELEMETRY", None)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [sys.executable, str(BIN), *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=20,
        )

    def test_record_turn_extracts_stderr_root_cause_and_redacts_secrets(self):
        job_dir = self.base / "aeb38c4d8652"
        job_dir.mkdir(parents=True)
        (job_dir / "stderr.log").write_text(
            "error: Bearer sk-secret-token-1234567890abcdef\n"
            'AGY_ERROR: {"short_error":"RESOURCE_EXHAUSTED (code 429): quota reached; token=sk-abcdefghijklmnop1234"}\n',
            encoding="utf-8",
        )
        state = {
            "id": "aeb38c4d8652",
            "engine": "agy",
            "model": "gemini-4-hydrogen-high",
            "reasoning_effort": "high",
            "workspace": "/home/hamza/repo/tessera",
            "launch": {"provider": "antigravity"},
            "status": "failed",
            "exit_code": 3,
            "failure_kind": "worker",
            "error": "exit=3; terminal=failed; malformed events=0; error",
            "terminal_reason": "token=sk-abcdefghijklmnop1234 " + "x" * 300,
            "started_at": 1000.0,
            "ended_at": 1044.25,
            "event_steps": 4,
            "steps": 2,
            "malformed_events": 0,
            "turn_task_failures": 1,
            "turn_usage": {
                "input_tokens": 1200,
                "output_tokens": 300,
                "native_cache_read_tokens": 800,
            },
            "context_tokens": 5000,
            "workspace_delta": {"files": 2},
            "report_state": "missing",
        }
        evidence = {"verification": {"status": "not_run"}, "acceptance": {"status": "pending"}}

        ok = telemetry.record_turn(state, job_dir=job_dir, evidence=evidence, base=self.base)
        self.assertTrue(ok)

        events = telemetry.read_events(base=self.base)
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertEqual(ev["event"], "turn")
        self.assertEqual(ev["repo"], "tessera")
        self.assertEqual(ev["duration_s"], 44.25)
        self.assertEqual(ev["usage"], {"in": 1200, "out": 300, "cache_read": 800, "ctx": 5000})
        self.assertEqual(ev["changed_files"], 2)
        self.assertIn("RESOURCE_EXHAUSTED (code 429)", ev["error"])
        self.assertNotIn("sk-abcdefghijklmnop1234", ev["error"])
        self.assertIn("[redacted]", ev["error"])
        self.assertIn("[redacted]", ev["terminal_reason"])
        self.assertNotIn("sk-abcdefghijklmnop1234", ev["terminal_reason"])
        self.assertLessEqual(len(ev["terminal_reason"]), 240)

    def test_rotation_and_corrupt_line_resilience(self):
        for i in range(6):
            ok = telemetry.emit({"event": "turn", "id": f"job{i:09d}", "engine": "pi", "model": "m1", "status": "completed", "duration_s": float(i + 1)},
                                base=self.base, max_bytes=220)
            self.assertTrue(ok)

        self.assertTrue(telemetry.archive_path(base=self.base).is_file())
        self.assertTrue(telemetry.telemetry_path(base=self.base).is_file())

        # Append a torn line to verify reader skips malformed JSONL cleanly.
        with open(telemetry.telemetry_path(base=self.base), "a", encoding="utf-8") as stream:
            stream.write("{torn-json\n")

        events = telemetry.read_events(base=self.base)
        self.assertGreaterEqual(len(events), 2)
        self.assertTrue(all(e.get("event") == "turn" for e in events))

    def test_fail_silent_and_opt_out(self):
        # Unwritable target path (a regular file used as parent dir) must never raise.
        blocker = self.base / "not-a-dir"
        blocker.write_text("x", encoding="utf-8")
        self.assertFalse(telemetry.emit({"event": "turn", "id": "abc"}, base=blocker))

        # CLAIVE_TELEMETRY=0 disables emission.
        old = os.environ.get("CLAIVE_TELEMETRY")
        try:
            os.environ["CLAIVE_TELEMETRY"] = "0"
            self.assertFalse(telemetry.enabled())
            self.assertFalse(telemetry.emit({"event": "turn", "id": "abc"}, base=self.base))
            self.assertFalse(telemetry.telemetry_path(base=self.base, create=False).exists())
        finally:
            if old is None:
                os.environ.pop("CLAIVE_TELEMETRY", None)
            else:
                os.environ["CLAIVE_TELEMETRY"] = old

    def test_cli_error_omits_subprocess_arguments_and_output(self):
        telemetry.emit({"event": "turn"}, base=self.base)
        command = ["worker", "--prompt", "private task details"]
        errors = (
            subprocess.CalledProcessError(2, command, stderr="private output"),
            subprocess.TimeoutExpired(command, 5, output="private output"),
        )
        for error in errors:
            self.assertTrue(telemetry.record_cli_error("run", error, base=self.base))

        events = telemetry.read_events(base=self.base, event_type="cli_error")
        self.assertEqual([event["error"] for event in events], [
            "Subprocess exited with status 2", "Subprocess timed out after 5 seconds",
        ])

    def test_cli_summary_tail_path_clear_and_cli_error_capture(self):
        telemetry.emit({"event": "turn", "id": "a1", "engine": "agy", "model": "m1", "status": "completed", "duration_s": 10.0}, base=self.base)
        telemetry.emit({"event": "turn", "id": "a2", "engine": "agy", "model": "m1", "status": "failed", "failure_kind": "quota", "error": "429 quota", "duration_s": 20.0}, base=self.base)
        telemetry.emit({"event": "fallback", "id": "a2", "engine": "agy", "from_model": "m1", "to_model": "m2", "failure_kind": "quota"}, base=self.base)

        # Trigger a real CLI ValueError (invalid worker ID) to verify cli_error recording.
        bad = self._run_cli("show", "does-not-exist-xyz")
        self.assertEqual(bad.returncode, 1)

        res = self._run_cli("telemetry", "summary", "--json")
        self.assertEqual(res.returncode, 0, res.stderr)
        summary = json.loads(res.stdout)
        self.assertEqual(summary["turns"], 2)
        self.assertEqual(summary["ok"], 1)
        self.assertEqual(summary["fail"], 1)
        self.assertEqual(summary["fallback_events"], 1)
        self.assertEqual(summary["cli_errors"], 1)
        self.assertEqual(summary["top_failures"][0]["count"], 1)

        tail = self._run_cli("telemetry", "tail", "--failures", "--json")
        self.assertEqual(tail.returncode, 0, tail.stderr)
        tail_items = json.loads(tail.stdout)
        self.assertEqual(len(tail_items), 3)  # failed turn + fallback + cli_error

        path_res = self._run_cli("telemetry", "path")
        self.assertEqual(path_res.returncode, 0, path_res.stderr)
        self.assertIn("telemetry.jsonl", path_res.stdout)

        clr = self._run_cli("telemetry", "clear")
        self.assertEqual(clr.returncode, 0, clr.stderr)
        self.assertFalse(telemetry.telemetry_path(base=self.base, create=False).exists())


if __name__ == "__main__":
    unittest.main()
