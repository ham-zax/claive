"""Process lifecycle, detached grandchild cleanup, tool-idle recovery, and interrupt-tool tests."""
import hermetic  # noqa: F401
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unittest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "bin"))
from claivelib import procgroup

CLI = str(REPO / "bin/claive")
MEMCAP = str(REPO / "bin/claive-memcap")


def _pid_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    table = procgroup.proc_table()
    info = table.get(pid)
    return info is not None and info[0] != "Z"


class ProcessLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="claive-lifecycle-")
        self.base = Path(self.temp.name)
        self.registry = self.base / "registry"
        self.workspace = self.base / "repo"
        self.workspace.mkdir()
        subprocess.run(["git", "init", "-q", str(self.workspace)], check=True)
        (self.workspace / "README.md").write_text("hello\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.workspace), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(self.workspace), "-c", "user.name=Test", "-c", "user.email=t@e.invalid",
             "commit", "-qm", "init"],
            check=True,
        )
        self.prompt = self.base / "prompt.md"
        self.prompt.write_text("test prompt\n", encoding="utf-8")
        self.worker_bin = self.base / "fake_claude.py"
        self._write_fake_worker()
        self.env = dict(
            os.environ,
            CLAIVE_DIR=str(self.registry),
            CLAUDE_WORKER_BINARY=str(self.worker_bin),
        )

    def tearDown(self):
        for state_file in self.registry.glob("*/state.json"):
            try:
                st = json.loads(state_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            for key in ("worker_pid", "supervisor_pid"):
                pid = st.get(key)
                if isinstance(pid, int) and pid > 0:
                    procgroup.terminate_tree(pid, grace=0.0)
        self.temp.cleanup()

    def _write_fake_worker(self):
        script = '''#!/usr/bin/python3
import json
import os
from pathlib import Path
import subprocess
import sys
import time

mode = os.environ.get("LIFECYCLE_MODE", "leak_cancel")
marker_dir = Path(os.environ["LIFECYCLE_MARKERS"])
memcap_bin = os.environ["LIFECYCLE_MEMCAP"]

def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\\n")
    sys.stdout.flush()

emit({"type": "system", "subtype": "init", "session_id": "sess-1"})

if mode in ("leak_cancel", "leak_timeout"):
    emit({
        "type": "assistant",
        "message": {
            "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "node --test"}}],
        },
    })
    grandchild_code = (
        "import os, time; "
        f"open({str(marker_dir / 'grandchild.pid')!r}, 'w').write(str(os.getpid())); "
        "time.sleep(60)"
    )
    # Spawn detached grandchild inheriting stdout (the supervisor event pipe) and holding heavy lock!
    if mode == "leak_cancel":
        cmd = [memcap_bin, "1G", "--lock", "heavy", "--", sys.executable, "-c", grandchild_code]
    else:
        cmd = [sys.executable, "-c", grandchild_code]
    subprocess.Popen(
        cmd,
        stdout=sys.stdout,
        stderr=sys.stderr,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    time.sleep(60)

elif mode == "tool_idle_recover":
    # Read initial user message if --enable-steering (stream-json stdin) is active.
    emit({
        "type": "assistant",
        "message": {
            "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "hang"}}],
        },
    })
    hung = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    (marker_dir / "tool.pid").write_text(str(hung.pid))
    rc = hung.wait()
    emit({
        "type": "user",
        "message": {
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": f"killed rc={rc}", "is_error": True}],
        },
    })
    report = {
        "status": "done",
        "summary": "recovered after hung tool",
        "changed_files": [],
        "verification": [],
        "risks": [],
    }
    answer = "Recovered.\\n```claive-report\\n" + json.dumps(report) + "\\n```"
    emit({"type": "assistant", "message": {"content": [{"type": "text", "text": answer}]}})
    emit({"type": "result", "subtype": "success", "is_error": False, "result": answer})

elif mode == "interrupt_preserve_infra":
    # Spawn a long-lived helper (e.g. MCP server) BEFORE model_step/first tool.
    infra = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    (marker_dir / "infra.pid").write_text(str(infra.pid))
    emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "starting tool"}]}})
    time.sleep(0.15)
    emit({
        "type": "assistant",
        "message": {
            "content": [{"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "sleep 60"}}],
        },
    })
    tool_proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    (marker_dir / "tool.pid").write_text(str(tool_proc.pid))
    tool_proc.wait()
    infra_alive = infra.poll() is None
    (marker_dir / "infra_alive.txt").write_text("yes" if infra_alive else "no")
    infra.terminate()
    infra.wait(timeout=3)
    emit({
        "type": "user",
        "message": {
            "content": [{"type": "tool_result", "tool_use_id": "t2", "content": "interrupted", "is_error": True}],
        },
    })
    emit({"type": "result", "subtype": "success", "is_error": False, "result": "ok"})
'''
        self.worker_bin.write_text(script, encoding="utf-8")
        self.worker_bin.chmod(0o755)

    def _cli(self, *args, extra_env=None, timeout=15):
        env = dict(self.env)
        if extra_env:
            env.update(extra_env)
        return subprocess.run([CLI, *args], env=env, capture_output=True, text=True, timeout=timeout)

    def _wait_file(self, path, timeout=8.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.is_file() and path.read_text(encoding="utf-8").strip():
                return path.read_text(encoding="utf-8").strip()
            time.sleep(0.03)
        self.fail(f"timed out waiting for {path}")

    def test_cancel_kills_setsid_grandchild_holding_stdout_and_heavy_lock(self):
        markers = self.base / "m1"
        markers.mkdir()
        started = self._cli(
            "start", "--engine", "claude", "--workspace", str(self.workspace),
            "--prompt-file", str(self.prompt), "--disable-steering",
            extra_env={"LIFECYCLE_MODE": "leak_cancel", "LIFECYCLE_MARKERS": str(markers), "LIFECYCLE_MEMCAP": MEMCAP},
        )
        self.assertEqual(started.returncode, 0, started.stderr)
        job_id = re.search(r"Worker ([0-9a-f]{12})", started.stdout).group(1)
        grandchild_pid = int(self._wait_file(markers / "grandchild.pid"))
        self.assertTrue(_pid_alive(grandchild_pid))

        cancelled = self._cli("cancel", job_id)
        self.assertEqual(cancelled.returncode, 0, cancelled.stderr)

        waited = self._cli("wait", job_id, "--timeout", "8")
        self.assertEqual(waited.returncode, 130, waited.stdout + waited.stderr)

        state = json.loads(self._cli("show", job_id, "--json").stdout)
        self.assertEqual(state["status"], "cancelled")
        self.assertFalse(_pid_alive(grandchild_pid))

        lock_probe = subprocess.run(
            [MEMCAP, "1G", "--lock", "heavy", "--lock-timeout", "0.5", "--", "/bin/true"],
            env=self.env, capture_output=True, text=True, timeout=5,
        )
        self.assertEqual(lock_probe.returncode, 0, lock_probe.stderr)

    def test_turn_timeout_preempts_silent_tool_and_kills_setsid_grandchild(self):
        markers = self.base / "m2"
        markers.mkdir()
        started = self._cli(
            "start", "--engine", "claude", "--workspace", str(self.workspace),
            "--prompt-file", str(self.prompt), "--turn-timeout", "1.2", "--disable-steering",
            extra_env={"LIFECYCLE_MODE": "leak_timeout", "LIFECYCLE_MARKERS": str(markers), "LIFECYCLE_MEMCAP": MEMCAP},
        )
        self.assertEqual(started.returncode, 0, started.stderr)
        job_id = re.search(r"Worker ([0-9a-f]{12})", started.stdout).group(1)
        grandchild_pid = int(self._wait_file(markers / "grandchild.pid"))

        waited = self._cli("wait", job_id, "--timeout", "10")
        self.assertNotEqual(waited.returncode, 0)
        state = json.loads(self._cli("show", job_id, "--json").stdout)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["failure_kind"], "timeout")
        self.assertFalse(_pid_alive(grandchild_pid))

    def test_tool_idle_timeout_rescues_hung_tool_and_preserves_report(self):
        markers = self.base / "m3"
        markers.mkdir()
        started = self._cli(
            "start", "--engine", "claude", "--workspace", str(self.workspace),
            "--prompt-file", str(self.prompt), "--report", "--tool-idle-timeout", "0.8", "--enable-steering",
            extra_env={"LIFECYCLE_MODE": "tool_idle_recover", "LIFECYCLE_MARKERS": str(markers), "LIFECYCLE_MEMCAP": MEMCAP},
        )
        self.assertEqual(started.returncode, 0, started.stderr)
        job_id = re.search(r"Worker ([0-9a-f]{12})", started.stdout).group(1)
        tool_pid = int(self._wait_file(markers / "tool.pid"))

        waited = self._cli("wait", job_id, "--timeout", "10")
        self.assertEqual(waited.returncode, 0, waited.stdout + waited.stderr)
        state = json.loads(self._cli("show", job_id, "--json").stdout)
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["report_state"], "ok")
        self.assertEqual(state["report"]["summary"], "recovered after hung tool")
        self.assertIn(tool_pid, state["last_tool_interrupt"]["killed_pids"])
        self.assertFalse(_pid_alive(tool_pid))

    def test_interrupt_tool_kills_hung_tool_while_preserving_infra_child(self):
        markers = self.base / "m4"
        markers.mkdir()
        started = self._cli(
            "start", "--engine", "claude", "--workspace", str(self.workspace),
            "--prompt-file", str(self.prompt), "--enable-steering",
            extra_env={
                "LIFECYCLE_MODE": "interrupt_preserve_infra",
                "LIFECYCLE_MARKERS": str(markers),
                "LIFECYCLE_MEMCAP": MEMCAP,
            },
        )
        self.assertEqual(started.returncode, 0, started.stderr)
        job_id = re.search(r"Worker ([0-9a-f]{12})", started.stdout).group(1)
        infra_pid = int(self._wait_file(markers / "infra.pid"))
        tool_pid = int(self._wait_file(markers / "tool.pid"))

        interrupted = self._cli("interrupt-tool", job_id, "--message", "skip hung command", "--json")
        self.assertEqual(interrupted.returncode, 0, interrupted.stderr)
        receipt = json.loads(interrupted.stdout)
        self.assertIn(tool_pid, receipt["killed_pids"])
        self.assertNotIn(infra_pid, receipt["killed_pids"])
        self.assertTrue(receipt["steered"])

        waited = self._cli("wait", job_id, "--timeout", "8")
        self.assertEqual(waited.returncode, 0, waited.stdout + waited.stderr)
        self.assertEqual((markers / "infra_alive.txt").read_text(encoding="utf-8").strip(), "yes")
        self.assertFalse(_pid_alive(tool_pid))
        self.assertFalse(_pid_alive(infra_pid))

    def test_memcap_kills_setsid_grandchild_and_releases_lock(self):
        pid_file = self.base / "memcap_grandchild.pid"
        code = (
            "import subprocess, sys, time; "
            "subprocess.Popen([sys.executable, '-c', "
            "'import os, sys, time; open(sys.argv[1], \"w\").write(str(os.getpid())); time.sleep(60)', "
            "sys.argv[1]], start_new_session=True); "
            "time.sleep(60)"
        )
        res = subprocess.run(
            [MEMCAP, "1G", "--timeout", "0.6", "--lock", "heavy", "--", sys.executable, "-c", code, str(pid_file)],
            env=self.env, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(res.returncode, 124)
        gpid = int(pid_file.read_text(encoding="utf-8").strip())
        self.assertFalse(_pid_alive(gpid))
        reacquire = subprocess.run(
            [MEMCAP, "1G", "--lock", "heavy", "--lock-timeout", "0.5", "--", "/bin/true"],
            env=self.env, capture_output=True, text=True, timeout=5,
        )
        self.assertEqual(reacquire.returncode, 0, reacquire.stderr)


if __name__ == "__main__":
    unittest.main()
