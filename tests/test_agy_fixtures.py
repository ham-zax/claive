import hermetic  # noqa: F401 (isolate provider settings before other imports)
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / "fixtures" / "agy"


def events(name):
    return [json.loads(line) for line in (FIXTURES / name).read_text().splitlines()]


def result(name):
    return next(row["result"] for row in events(name) if row["event"] == "result")


class AgyFixtureChecks(unittest.TestCase):
    """Check observed protocol facts, without claiming adapter acceptance."""

    def replay(self, scenario="success", *args):
        return subprocess.run(
            [str(ROOT / "fake_agy.py"), *args],
            env=dict(os.environ, FAKE_AGY_SCENARIO=scenario),
            capture_output=True, text=True, timeout=5,
        )

    def test_provenance_and_sanitization(self):
        manifest = json.loads((FIXTURES / "manifest.json").read_text())
        self.assertEqual(manifest["version"], "1.4.12")
        for name, metadata in manifest["files"].items():
            with self.subTest(name=name):
                content = (FIXTURES / name).read_bytes()
                self.assertEqual(hashlib.sha256(content).hexdigest(), metadata["sha256"])
                self.assertNotIn(b"/home/ubuntu", content)
                self.assertNotIn(b"/tmp/claive-agy-phase0", content)
                if metadata["provenance"] == "captured":
                    self.assertEqual(len(metadata["source_sha256"]), 64)
                else:
                    self.assertEqual(metadata["provenance"], "synthetic")
        self.assertEqual(manifest["files"]["timeout.ndjson"]["provenance"], "captured")
        for name in ("error.ndjson", "banner.ndjson"):
            self.assertEqual(manifest["files"][name]["provenance"], "synthetic")

    def test_init_has_top_level_conversation_id(self):
        rows = events("success.ndjson")
        init = rows[0]
        self.assertEqual(init["event"], "init")
        self.assertNotIn("conversation_id", init["init"])
        self.assertEqual(init["conversation_id"], rows[-1]["result"]["conversation_id"])
        for row in rows[1:-1]:
            self.assertEqual(row["step_update"]["conversation_id"], init["conversation_id"])

    def test_three_turn_resume_and_usage(self):
        names = ("success.ndjson", "resume.ndjson", "third.ndjson")
        totals = [result(name) for name in names]
        self.assertEqual(len({row["conversation_id"] for row in totals}), 1)
        self.assertEqual([row["num_turns"] for row in totals], [1, 2, 3])
        self.assertEqual([row["response"].strip() for row in totals], ["READY"] * 3)
        self.assertEqual([row["usage"]["input_tokens"] for row in totals], [6779, 13828, 21130])
        for index, row in enumerate(totals):
            usage = row["usage"]
            self.assertEqual(usage["total_tokens"], usage["input_tokens"] + usage["output_tokens"])
            self.assertLessEqual(usage["thinking_tokens"], usage["output_tokens"])
            self.assertEqual(usage["cache_read_tokens"], 0)
            if index:
                previous = totals[index - 1]["usage"]
                self.assertTrue(all(usage[key] >= previous[key] for key in usage))
        indices = [{row["step_update"]["step_index"] for row in events(name)
                    if row["event"] == "step_update"} for name in names]
        self.assertLess(max(indices[0]), min(indices[1]))
        self.assertLess(max(indices[1]), min(indices[2]))

    def test_edit_contains_multiple_model_steps_and_tool_pairs(self):
        rows = events("edit.ndjson")
        steps = [row["step_update"] for row in rows if row["event"] == "step_update"]
        model_steps = [step for step in steps if step.get("usage")]
        self.assertGreater(len(model_steps), 1)
        tools = [step for step in steps if step["step_type"] == "tool"]
        self.assertIn("write_file", {step["tool_name"] for step in tools})
        for index in {step["step_index"] for step in tools}:
            pair = [step for step in tools if step["step_index"] == index]
            self.assertEqual([step["state"] for step in pair], ["ACTIVE", "DONE"])
        self.assertEqual(rows[0]["init"]["cwd"], "/fixture/edit-repo")
        self.assertIn("after", rows[-1]["result"]["response"])

    def test_timeout_replays_zero_exit_and_success_status(self):
        process = self.replay("timeout")
        self.assertEqual(process.returncode, 0)
        terminal = json.loads(process.stdout.splitlines()[-1])["result"]
        self.assertEqual(terminal["status"], "SUCCESS")
        self.assertEqual(terminal["finished_reason"], "print_timeout")
        self.assertEqual(terminal["response"], "")
        self.assertIn("timeout", process.stderr)

    def test_nested_steps_and_split_text_paths(self):
        steps = [row["step_update"] for row in events("nested.ndjson")
                 if row["event"] == "step_update"]
        self.assertTrue({"subagent", "system_message"} <= {step["step_type"] for step in steps})
        text = "".join(step.get("text_delta", "") for step in steps)
        self.assertNotIn("/tmp/claive-agy-phase0", text)
        self.assertIn("/fixture/edit-repo", text)

    def test_unknown_conversation_returns_a_different_id(self):
        unknown = "00000000-0000-4000-8000-000000000000"
        process = self.replay("unknown", "--conversation", unknown)
        self.assertEqual(process.returncode, 0)
        self.assertIn(unknown, process.stderr)
        terminal = json.loads(process.stdout.splitlines()[-1])["result"]
        self.assertEqual(terminal["status"], "SUCCESS")
        self.assertNotEqual(terminal["conversation_id"], unknown)

    def test_concurrent_captures_retain_separate_conversations(self):
        identifiers = []
        for label in ("alpha", "bravo"):
            first = result(f"concurrent-{label}.ndjson")
            resumed = result(f"concurrent-{label}-resume.ndjson")
            self.assertEqual(first["conversation_id"], resumed["conversation_id"])
            self.assertEqual(first["response"], resumed["response"])
            identifiers.append(first["conversation_id"])
        self.assertNotEqual(*identifiers)

    def test_replay_formats_and_synthetic_failures(self):
        for name in ("success", "resume", "third", "edit", "nested", "flags-after"):
            with self.subTest(scenario=name):
                process = self.replay(name, "--output-format", "stream-json", "-p", "fixture prompt")
                self.assertEqual(process.returncode, 0, process.stderr)
                self.assertEqual(process.stdout, (FIXTURES / (name + ".ndjson")).read_text())
        process = self.replay("success", "--output-format", "json")
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout)["status"], "SUCCESS")
        process = self.replay("error")
        self.assertEqual(process.returncode, 1)
        self.assertEqual(json.loads(process.stdout)["result"]["status"], "ERROR")
        process = self.replay("banner")
        self.assertEqual(process.returncode, 0)
        self.assertEqual(process.stdout.splitlines()[0], "Synthetic startup banner")
        self.assertEqual(json.loads(process.stdout.splitlines()[1])["event"], "init")

    def test_replay_rejects_unknown_scenarios(self):
        process = self.replay("../../success")
        self.assertEqual(process.returncode, 2)
        self.assertIn("unknown fixture", process.stderr)
        self.assertEqual(process.stdout, "")

    def test_replay_identifies_itself(self):
        process = self.replay("success", "--version")
        self.assertEqual(process.returncode, 0)
        self.assertEqual(process.stdout.strip(), "1.4.12 (fixture replay)")
        process = self.replay("success", "models")
        self.assertEqual(process.returncode, 0)
        self.assertEqual(len(process.stdout.splitlines()), 6)

    def test_hermetic_environment_blocks_host_agy(self):
        process = subprocess.run(
            [sys.executable, "-c", "import hermetic, os, json; print(json.dumps(dict(os.environ)))"],
            cwd=ROOT, env=dict(os.environ, AGY_WORKER_BINARY="/host/agy", FAKE_AGY_SCENARIO="error"),
            capture_output=True, text=True, timeout=5,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        environment = json.loads(process.stdout)
        self.assertNotEqual(environment["AGY_WORKER_BINARY"], "/host/agy")
        self.assertFalse(Path(environment["AGY_WORKER_BINARY"]).exists())
        self.assertNotIn("FAKE_AGY_SCENARIO", environment)


if __name__ == "__main__":
    unittest.main(verbosity=2)
