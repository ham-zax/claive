"""Supervisor process names on Linux."""
import hermetic  # noqa: F401  (must run before claivelib reads the environment)
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from test_workers import FIXTURE_CLI, FIXTURE_WORKER, module
from claivelib import procname


class SupervisorProcessNameChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="claive-procname-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.registry = self.root / "registry"
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.prompt = self.root / "prompt.md"
        self.prompt.write_text("fixture supervisor name test\n")
        self.env = dict(os.environ, CLAIVE_DIR=str(self.registry), TERM="dumb",
                        FIXTURE_WORKER_BINARY=FIXTURE_WORKER, FIXTURE_MODE="success")
        self.job = None

    def tearDown(self):
        if not self.registry.exists():
            return
        for folder in self.registry.iterdir():
            state_file = folder / "state.json"
            if not state_file.is_file():
                continue
            state = json.loads(state_file.read_text())
            pid = state.get("supervisor_pid")
            if pid and module.identity(pid) == state.get("supervisor_identity"):
                try:
                    os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def cli(self, *args):
        return subprocess.run([FIXTURE_CLI, *args], env=self.env, text=True,
                              capture_output=True, timeout=15)

    def state(self):
        return json.loads((self.registry / self.job / "state.json").read_text())

    def until_idle(self):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            state = self.state()
            if state.get("status") == "idle" and state.get("supervisor_pid"):
                return state
            time.sleep(0.025)
        self.fail(f"reusable supervisor did not become idle: {self.state()}")

    def test_empty_linux_sys_executable_is_restored_from_proc(self):
        with (mock.patch.object(procname.sys, "platform", "linux"),
              mock.patch.object(procname.sys, "executable", ""),
              mock.patch.object(procname.os, "readlink", return_value="/usr/bin/python3") as readlink):
            procname.restore_sys_executable()
            self.assertEqual(procname.sys.executable, "/usr/bin/python3")
        readlink.assert_called_once_with("/proc/self/exe")

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux /proc process names")
    def test_detached_fixture_supervisor_has_comm_and_argv_names(self):
        result = self.cli("open", "--detach", "--engine", "fixture", "--workspace", str(self.workspace),
                          "--prompt-file", str(self.prompt))
        self.assertEqual(result.returncode, 0, result.stderr)
        match = re.search(r"Worker ([0-9a-f]{12})", result.stdout)
        self.assertIsNotNone(match, result.stdout)
        self.job = match.group(1)
        state = self.until_idle()
        pid = state["supervisor_pid"]

        comm = Path(f"/proc/{pid}/comm").read_text().strip()
        argv = [part for part in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0") if part]
        self.assertEqual(comm, f"claive-{self.job[:8]}")
        self.assertEqual(argv[0], f"claive-supervisor {self.job}".encode())
        self.assertEqual(argv[1], FIXTURE_CLI.encode())
        self.assertEqual(argv[2:], [b"_reusable", self.job.encode()])

        closed = self.cli("close", self.job)
        self.assertEqual(closed.returncode, 0, closed.stderr)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.state().get("status") == "completed":
                return
            time.sleep(0.025)
        self.fail(f"supervisor did not close: {self.state()}")


if __name__ == "__main__":
    unittest.main()
