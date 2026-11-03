"""Opt-in real AGY acceptance. Artifacts remain under /tmp for inspection."""
import hermetic
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import time
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
BINARY = hermetic.AGY_HOST_BINARY


@unittest.skipUnless(hermetic.AGY_LIVE and BINARY and os.access(BINARY, os.X_OK),
                     "set CLAIVE_AGY_LIVE=1 with an executable AGY binary")
class AgyLiveChecks(unittest.TestCase):
    def test_edit_resume_usage_and_soft_read(self):
        base = Path(tempfile.mkdtemp(prefix="claive-agy-live-"))
        repo = base / "repo"
        repo.mkdir()
        registry = base / "state"
        env = dict(os.environ, AGY_WORKER_BINARY=BINARY, CLAIVE_DIR=str(registry),
                   CLAIVE_CONFIG=str(base / "no-config.json"))
        def run(*args, timeout=30):
            return subprocess.run([str(ROOT / "bin/claive"), *args], env=env,
                                  capture_output=True, text=True, timeout=timeout)
        def git(*args):
            return subprocess.run(["git", "-C", str(repo), *args],
                                  capture_output=True, text=True, check=True).stdout
        def idle(job, turn):
            deadline = time.monotonic() + 210
            while time.monotonic() < deadline:
                state = json.loads((registry / job / "state.json").read_text())
                if state["status"] == "idle" and state["turn"] == turn:
                    self.assertEqual(state["last_turn_status"], "completed", state)
                    return state
                if state["status"] not in {"starting", "running", "idle"}:
                    self.fail(str(state))
                time.sleep(0.1)
            self.fail("live AGY turn exceeded its deadline")
        git("init", "-q")
        (repo / "tracked.txt").write_text("parent stays unchanged\n")
        git("add", "tracked.txt")
        git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "base")
        nonce = uuid.uuid4().hex
        first_prompt = base / "first.md"
        first_prompt.write_text(f"Write exactly {nonce} followed by a newline to nonce.txt in the current directory. "
                                "Read the file to check the content. Reply READY followed by its content.")
        check = "nonce=" + shlex.join(["python3", "-c",
            f"from pathlib import Path; assert Path('nonce.txt').read_text() == {repr(nonce + chr(10))}"])
        start = run("open", "--detach", "--engine", "agy", "--workspace", str(repo),
                    "--prompt-file", str(first_prompt), "--worktree", "--check", check, "--turn-timeout", "180")
        self.assertEqual(start.returncode, 0, start.stderr)
        job = re.search(r"Worker ([0-9a-f]{12})", start.stdout).group(1)
        try:
            first = idle(job, 1)
            tree = Path(first["actual_workspace"])
            self.assertEqual((tree / "nonce.txt").read_text(), nonce + "\n")
            self.assertFalse((repo / "nonce.txt").exists())
            self.assertEqual(git("status", "--porcelain"), "")
            checked = run("verify", job)
            self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
            # Remove the only file containing the random nonce. The second prompt
            # supplies neither the value nor the first prompt's path.
            (tree / "nonce.txt").unlink()
            first_prompt.unlink()
            second_prompt = base / "second.md"
            second_prompt.write_text("Reply with the exact random nonce from our previous turn. "
                                     "Use conversation memory. Do not use tools or read any files.")
            followed = run("followup", job, "--prompt-file", str(second_prompt))
            self.assertEqual(followed.returncode, 0, followed.stderr)
            second = idle(job, 2)
            self.assertEqual(first["session_id"], second["session_id"])
            self.assertIn(nonce, (registry / job / "result.txt").read_text())
            usage_result = run("usage", job, "--json")
            self.assertEqual(usage_result.returncode, 0, usage_result.stderr)
            usage = json.loads(usage_result.stdout)
            self.assertTrue(usage["usage_complete"], usage)
            before = first["usage_baseline"][first["session_id"]]
            after = second["usage_baseline"][second["session_id"]]
            for key in ("input_tokens", "output_tokens", "total_tokens"):
                self.assertEqual(usage["turn"][key], after[key] - before[key])
            self.assertEqual(git("status", "--porcelain"), "")
            (base / "shown.json").write_text(run("show", job, "--json").stdout)
            (base / "usage.json").write_text(usage_result.stdout)
        finally:
            run("close", job)
        read_prompt = base / "read.md"
        read_prompt.write_text("Read tracked.txt and report its content. Do not change files.")
        read = run("run", "--engine", "agy", "--read-only", "--workspace", str(repo),
                   "--prompt-file", str(read_prompt), "--turn-timeout", "180", timeout=210)
        self.assertEqual(read.returncode, 0, read.stdout + read.stderr)
        read_job = re.search(r"Worker ([0-9a-f]{12})", read.stdout).group(1)
        shown = json.loads(run("show", read_job, "--json").stdout)
        self.assertEqual(shown["read_mode"], "soft")
        self.assertEqual(git("status", "--porcelain"), "")
        (base / "receipt.json").write_text(json.dumps(dict(job=job, read_job=read_job,
                                                          registry=str(registry)), indent=2))
        print(f"AGY live acceptance artifacts: {base}", flush=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
