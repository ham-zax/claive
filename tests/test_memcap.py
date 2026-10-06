"""Tests: claive-memcap (session RSS cap) and claive-orch init --verify-memory."""
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]
MEMCAP = str(REPO_ROOT / "bin/claive-memcap")
sys.path.insert(0, str(REPO_ROOT / "bin"))
from claivelib import memcap
import test_orch_more as more  # module import, so its tests are not collected twice

# Grows to about 600 MB in 10 MB steps unless killed first.
HOG = ("import time\nb = []\nfor _ in range(60):\n    b.append(bytearray(10 ** 7))\n    time.sleep(0.02)\n"
       "time.sleep(5)\n")


class MemcapUnit(unittest.TestCase):
    def test_parse_size(self):
        self.assertEqual(memcap.parse_size("2G"), 2 * 1024 ** 3)
        self.assertEqual(memcap.parse_size("512m"), 512 * 1024 ** 2)
        self.assertEqual(memcap.parse_size("1.5GiB"), int(1.5 * 1024 ** 3))
        self.assertEqual(memcap.parse_size("4096"), 4096)
        for bad in ("0", "-1G", "lots", "", "2X"):
            with self.assertRaises(ValueError, msg=bad):
                memcap.parse_size(bad)

    def test_under_limit_passes_through(self):
        result = memcap.run_capped(["bash", "-c", "echo out; echo err >&2; exit 3"], memcap.parse_size("1G"))
        self.assertEqual((result["code"], result["stdout"], result["stderr"]), (3, "out\n", "err\n"))
        self.assertFalse(result["exceeded"] or result["timed_out"])

    def test_grandchild_over_limit_is_killed(self):
        started = time.monotonic()
        # The hog is a grandchild (bash -> python3), so the cap must count the whole session.
        result = memcap.run_capped(["bash", "-c", f"python3 -c '{HOG}'; echo survived"],
                                   memcap.parse_size("100M"))
        self.assertTrue(result["exceeded"])
        self.assertIsNone(result["code"])
        self.assertNotIn("survived", result["stdout"])
        self.assertGreater(result["peak"], memcap.parse_size("100M"))
        self.assertLess(time.monotonic() - started, 5)

    def test_timeout_kills_children(self):
        marker = f"claive-memcap-test-{os.getpid()}"
        started = time.monotonic()
        result = memcap.run_capped(["bash", "-c", f"sleep 30 & exec -a {marker} sleep 30"], timeout=0.5)
        self.assertTrue(result["timed_out"])
        self.assertLess(time.monotonic() - started, 5)
        left = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True)
        self.assertEqual(left.stdout, "")

    def test_cli_exit_codes(self):
        hog = subprocess.run([MEMCAP, "100M", "--", "python3", "-c", HOG], capture_output=True, text=True,
                             timeout=30)
        self.assertEqual(hog.returncode, memcap.EXCEEDED_EXIT)
        self.assertIn("memory exceeded 100M", hog.stderr)
        ok = subprocess.run([MEMCAP, "1G", "--", "bash", "-c", "exit 4"], capture_output=True, text=True)
        self.assertEqual(ok.returncode, 4)
        slow = subprocess.run([MEMCAP, "1G", "--timeout", "0.3", "--", "sleep", "10"], capture_output=True,
                              text=True, timeout=10)
        self.assertEqual(slow.returncode, memcap.TIMEOUT_EXIT)
        for args in (["0", "--", "true"], ["1G"]):
            self.assertNotEqual(subprocess.run([MEMCAP, *args], capture_output=True).returncode, 0)


class OrchVerifyMemory(unittest.TestCase):
    setUp, tearDown, git = more.OrchMore.setUp, more.OrchMore.tearDown, more.OrchMore.git
    orch, init = more.OrchMore.orch, more.OrchMore.init

    def test_verifier_over_limit_is_an_error(self):
        hog = self.base / "hog.py"
        hog.write_text(HOG)
        output = self.orch("init", "--repo", str(self.repo), "--task-file", str(self.task), "--arm", "D",
                           "--verify", f"python3 {hog}", "--verify-memory", "100M").stdout
        run_dir = Path(re.search(r"\| (/\S+)$", output.splitlines()[0]).group(1))
        text = (run_dir / "verify" / "base.txt").read_text()
        self.assertIn("exceeded memory limit 100M", text)

    def test_worker_prompt_wraps_visible_check(self):
        run = self.init("D", "--verify-memory", "1G")
        self.orch("lane", run, "a", "--engine", "muse", "--model", more.MUSE)
        prompt = Path(self.orch("prompt", run, "implement", "a").stdout.strip()).read_text()
        self.assertIn("claive-memcap 1G -- bash -c 'python3 -m unittest -q'", prompt)

    def test_invalid_size_refused(self):
        result = self.orch("init", "--repo", str(self.repo), "--task-file", str(self.task), "--arm", "D",
                           "--verify", "python3 -m unittest -q", "--verify-memory", "lots", ok=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid memory size", result.stderr)


if __name__ == "__main__":
    unittest.main()
