"""End-to-end CLI integration tests for the Unified Background & Notification System
and companion orchestrator friction fixes.
"""
import hermetic  # noqa: F401
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


class CliBackgroundSystemTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="claive-bg-e2e-")
        self.path = Path(self.temp.name)
        self.registry = self.path / "registry"
        self.workspace = self.path / "repo"
        self.workspace.mkdir()
        subprocess.run(["git", "-C", str(self.workspace), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(self.workspace), "config", "user.email", "t@example.com"], check=True)
        subprocess.run(["git", "-C", str(self.workspace), "config", "user.name", "Test"], check=True)
        (self.workspace / ".gitignore").write_text("dist/\n.venv/\nnode_modules/\n*/node_modules/\n")
        (self.workspace / "README.md").write_text("hello\n")
        subprocess.run(["git", "-C", str(self.workspace), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.workspace), "commit", "-q", "-m", "init"], check=True)

        self.config_file = self.path / "config.json"
        self.config_file.write_text("{}\n")
        self.env = dict(
            os.environ,
            CLAIVE_DIR=str(self.registry),
            CLAIVE_CONFIG=str(self.config_file),
            TERM="dumb",
            FIXTURE_WORKER_BINARY=FIXTURE_WORKER,
            FIXTURE_MODE="success",
            FIXTURE_PROMPT_OUT=str(self.path / "captured.txt"),
            FIXTURE_ENV_OUT=str(self.path / "env.jsonl"),
        )
        for name in (
            "CLAIVE_WORKER_ID", "CLAIVE_ALLOW_NESTED", "CLAIVE_MISSION",
            "CLAIVE_NOTIFY_CMD", "CLAIVE_PARENT_SESSION", "CLAUDE_SESSION_ID",
            "CODEX_THREAD_ID", "ANTIGRAVITY_CONVERSATION_ID", "AGY_CONVERSATION_ID",
            "FIXTURE_TEXT_FILE",
        ):
            self.env.pop(name, None)
        self.prompt = self.path / "task.md"
        self.prompt.write_text("PLAIN TASK\n")

    def tearDown(self):
        if self.registry.exists():
            for folder in self.registry.iterdir():
                if (folder / "state.json").is_file():
                    state = json.loads((folder / "state.json").read_text())
                    for field in ("worker", "supervisor"):
                        pid = state.get(field + "_pid")
                        if pid and module.identity(pid) == state.get(field + "_identity"):
                            try:
                                os.killpg(pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
        self.temp.cleanup()

    def cli(self, *args, timeout=30, **env):
        return subprocess.run(
            [FIXTURE_CLI, *args],
            env=dict(self.env, **env),
            text=True,
            capture_output=True,
            timeout=timeout,
        )

    def launch(self, action, *extra, prompt=None, **env):
        res = self.cli(
            action,
            "--engine", "fixture",
            "--workspace", str(self.workspace),
            "--prompt-file", str(prompt or self.prompt),
            *extra,
            **env,
        )
        found = re.search(r"Worker ([0-9a-f]{12})", res.stdout)
        return res, found.group(1) if found else None

    def test_monotonic_seq_since_seq_and_compact_digest(self):
        report_file = self.path / "rep.txt"
        report_file.write_text(
            'done\n```claive-report\n{"status":"done","summary":"Built TradingView pane","changed_files":["src/chart.ts"]}\n```\n'
        )
        r1, j1 = self.launch("run", "--alias", "tv-one", "--report", "--session", "s-main", FIXTURE_TEXT_FILE=str(report_file))
        self.assertEqual(r1.returncode, 0, r1.stderr)
        r2, j2 = self.launch("run", "--alias", "tv-two", "--report", "--session", "s-main", FIXTURE_TEXT_FILE=str(report_file))
        self.assertEqual(r2.returncode, 0, r2.stderr)

        all_ev = json.loads(self.cli("inbox", "--since-seq", "0", "--json").stdout)
        self.assertEqual([e["seq"] for e in all_ev], [1, 2])
        self.assertEqual([e["id"] for e in all_ev], [j1, j2])

        digest1 = all_ev[0]["digest"]
        self.assertEqual(digest1["seq"], 1)
        self.assertEqual(digest1["worker_id"], j1)
        self.assertEqual(digest1["alias"], "@tv-one")
        self.assertEqual(digest1["exit_code"], 0)
        self.assertEqual(digest1["files_touched"], ["src/chart.ts"])
        self.assertEqual(digest1["report_headline"], "Built TradingView pane")

        after_1 = json.loads(self.cli("inbox", "--since-seq", "1", "--json").stdout)
        self.assertEqual([e["id"] for e in after_1], [j2])

        waited = self.cli("wait", "--since-seq", "1", "--session", "s-main", "--json", "--timeout", "5")
        self.assertEqual(waited.returncode, 0, waited.stderr)
        parsed = json.loads(waited.stdout)
        self.assertEqual(parsed["event"], "inbox")
        self.assertEqual([e["id"] for e in parsed["inbox_events"]], [j2])

    def test_session_isolation_across_concurrent_windows(self):
        r_a, job_a = self.launch("run", "--session", "win-a", "--label", "alpha-job")
        self.assertEqual(r_a.returncode, 0)
        r_b, job_b = self.launch("run", "--session", "win-b", "--label", "beta-job")
        self.assertEqual(r_b.returncode, 0)

        ev_a = json.loads(self.cli("inbox", "--session", "win-a", "--json").stdout)
        self.assertEqual([e["id"] for e in ev_a], [job_a])

        # Reading win-a did NOT steal win-b's event!
        ev_b = json.loads(self.cli("inbox", "--session", "win-b", "--json").stdout)
        self.assertEqual([e["id"] for e in ev_b], [job_b])

    def test_live_watcher_stop_hook_and_park_lifecycle(self):
        started, job = self.launch("start", "--session", "agy-live", FIXTURE_MODE="slow")
        self.assertEqual(started.returncode, 0, started.stderr)

        # Unwatched running worker blocks Stop hook on AGY/Claude (exit 2)
        stop_unwatched = self.cli("hook", "stop", "--session", "agy-live", "--harness", "agy", "--json")
        self.assertEqual(stop_unwatched.returncode, 2)
        stop_data = json.loads(stop_unwatched.stdout)
        self.assertEqual(stop_data["decision"], "block")
        self.assertEqual([w["id"] for w in stop_data["blocking_workers"]], [job])

        # Arm a live background `claive wait`: AGY Stop hook now approves (exit 0), Codex still blocks (exit 2)
        waiter = subprocess.Popen(
            [FIXTURE_CLI, "wait", "--session", "agy-live", "--timeout", "15"],
            env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                status_res = json.loads(self.cli("hook", "status", "--session", "agy-live", "--json").stdout)
                if status_res["counts"]["watched"] == 1:
                    break
                time.sleep(0.05)
            stop_watched_agy = self.cli("hook", "stop", "--session", "agy-live", "--harness", "agy")
            self.assertEqual(stop_watched_agy.returncode, 0, stop_watched_agy.stdout + stop_watched_agy.stderr)

            stop_watched_codex = self.cli("hook", "stop", "--session", "agy-live", "--harness", "codex")
            self.assertEqual(stop_watched_codex.returncode, 2)

            # Explicit park allows Codex Stop hook too
            parked = self.cli("park", job, "--reason", "handing off to user")
            self.assertEqual(parked.returncode, 0)
            stop_parked_codex = self.cli("hook", "stop", "--session", "agy-live", "--harness", "codex")
            self.assertEqual(stop_parked_codex.returncode, 0)
        finally:
            self.cli("cancel", job)
            waiter.communicate(timeout=15)

    def test_notify_cmd_and_notify_worker_wake_receipts(self):
        opened, coord = self.launch("open", "--detach", "--alias", "coord")
        self.assertEqual(opened.returncode, 0, opened.stderr)
        self.assertEqual(self.cli("wait", "@coord", "--timeout", "10").returncode, 0)

        sink_out = self.path / "sink.json"
        ran, child = self.launch(
            "run",
            "--notify-cmd", f"cat > {sink_out}",
            "--notify-worker", "@coord",
        )
        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.assertTrue(sink_out.is_file())
        self.assertEqual(json.loads(sink_out.read_text())["id"], child)

        # Wait for @coord's woken turn to settle and verify receipts
        self.assertEqual(self.cli("wait", "@coord", "--timeout", "10").returncode, 0)
        receipts = json.loads(self.cli("notifications", "--json").stdout)
        by_sink = {r["sink"]: r["status"] for r in receipts if r["event_id"] == child}
        self.assertEqual(by_sink.get("worker-notify-cmd"), "ok")
        self.assertEqual(by_sink.get("worker-notify-target"), "ok")
        self.cli("close", "@coord")

    def test_cli_reuse_alias_link_deps_shell_check_and_agy_family_effort(self):
        # 1. --reuse-alias rebinds settled worker without manual alias remove
        r1, j1 = self.launch("run", "--alias", "lane")
        self.assertEqual(r1.returncode, 0, r1.stderr)
        refused, _ = self.launch("run", "--alias", "lane")
        self.assertNotEqual(refused.returncode, 0)
        r2, j2 = self.launch("run", "--alias", "lane", "--reuse-alias")
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertNotEqual(j1, j2)
        shown = json.loads(self.cli("show", "@lane", "--json").stdout)
        self.assertEqual(shown["id"], j2)

        # 2. --worktree --link-deps + --check with shell cd/&& creating gitignored dist/
        (self.workspace / ".venv").mkdir()
        (self.workspace / ".venv" / "marker").write_text("venv-ok")
        (self.workspace / "sub").mkdir()
        (self.workspace / "sub" / "node_modules").mkdir()
        subprocess.run(["git", "-C", str(self.workspace), "add", "sub"], check=True)
        # sub has only ignored node_modules so add a tracked file inside sub
        (self.workspace / "sub" / "index.js").write_text("export default 1;\n")
        subprocess.run(["git", "-C", str(self.workspace), "add", "sub/index.js"], check=True)
        subprocess.run(["git", "-C", str(self.workspace), "commit", "-q", "-m", "add sub"], check=True)

        wt_run, wt_job = self.launch(
            "open", "--detach",
            "--worktree", "--link-deps", "--verify-in-worktree",
            "--check", "build=cd sub && test -d node_modules && mkdir -p ../dist && echo built > ../dist/app.js",
        )
        self.assertEqual(wt_run.returncode, 0, wt_run.stderr)
        self.assertEqual(self.cli("wait", wt_job, "--timeout", "10").returncode, 0)
        wt_path = self.registry / wt_job / "worktree"
        self.assertTrue((wt_path / ".venv").is_symlink())
        self.assertTrue((wt_path / "sub" / "node_modules").is_symlink())

        verified = self.cli("verify", wt_job)
        self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)
        dry = json.loads(self.cli("worktree", "apply", wt_job, "--dry-run", "--json").stdout)
        self.assertEqual(dry["applied"], [], "linked-deps and gitignored dist/ must not pollute worktree apply")
        self.cli("close", wt_job)

        # 3. AGY family stem + --effort resolution via CLI create_job
        fake_agy = Path(__file__).resolve().parent / "fake_agy.py"
        agy_res = self.cli(
            "start",
            "--engine", "agy",
            "--model", "gemini-4-hydrogen",
            "--effort", "low",
            "--workspace", str(self.workspace),
            "--prompt-file", str(self.prompt),
            AGY_WORKER_BINARY=str(fake_agy),
        )
        self.assertEqual(agy_res.returncode, 0, agy_res.stderr)
        agy_job = re.search(r"Worker ([0-9a-f]{12})", agy_res.stdout).group(1)
        agy_state = json.loads(self.cli("show", agy_job, "--json").stdout)
        self.assertEqual(agy_state["model"], "gemini-4-hydrogen-low")
        self.assertEqual(agy_state["reasoning_effort"], "low")
        self.cli("cancel", agy_job)

    def test_claude_stdin_hook_payload_stop_hook_active_and_worker_noop(self):
        started, job = self.launch("start", CLAUDE_CODE_SESSION_ID="cc-99", FIXTURE_MODE="slow")
        self.assertEqual(started.returncode, 0, started.stderr)
        try:
            # 1. Worker processes (CLAIVE_WORKER_ID set) never trigger parent hooks
            worker_hook = subprocess.run(
                [FIXTURE_CLI, "hook", "stop"],
                env=dict(self.env, CLAIVE_WORKER_ID=job, CLAUDE_CODE_SESSION_ID="cc-99"),
                text=True, capture_output=True, timeout=10,
            )
            self.assertEqual(worker_hook.returncode, 0)
            self.assertEqual(worker_hook.stdout + worker_hook.stderr, "")

            # 2. First Stop hook via Claude stdin JSON blocks (exit 2, message on stderr)
            first_stop = subprocess.run(
                [FIXTURE_CLI, "hook", "stop"],
                input=json.dumps({"hook_event_name": "Stop", "session_id": "cc-99", "stop_hook_active": False}),
                env=self.env, text=True, capture_output=True, timeout=10,
            )
            self.assertEqual(first_stop.returncode, 2)
            self.assertIn(job, first_stop.stderr)

            # 3. Retry with stop_hook_active=true downgrades to warn (exit 0) so Claude never loops
            retry_stop = subprocess.run(
                [FIXTURE_CLI, "hook", "stop", "--json"],
                input=json.dumps({"hook_event_name": "Stop", "session_id": "cc-99", "stop_hook_active": True}),
                env=self.env, text=True, capture_output=True, timeout=10,
            )
            self.assertEqual(retry_stop.returncode, 0)
            retry_data = json.loads(retry_stop.stdout)
            self.assertEqual(retry_data["decision"], "approve")
            self.assertIn(job, retry_data["systemMessage"])
        finally:
            self.cli("cancel", job)

    def test_top_level_apply_close_and_agy_preinvocation_hook_envelope(self):
        # 1. AGY PreInvocation hook returns {} when empty, and injectSteps envelope when an event arrives
        empty_hook = subprocess.run(
            [FIXTURE_CLI, "hook", "pre-turn", "--harness", "agy", "--agy-hook"],
            input=json.dumps({"hookEventName": "PreInvocation", "conversationId": "conv-777"}),
            env=self.env, text=True, capture_output=True, timeout=10,
        )
        self.assertEqual(empty_hook.returncode, 0)
        self.assertEqual(json.loads(empty_hook.stdout), {})

        # Launch a worktree writer under conv-777 that edits README.md and also leaves .pyc noise
        mut_prompt = self.path / "mut.md"
        mut_prompt.write_text("PLAIN TASK\n")
        custom_worker = self.path / "writer_worker.py"
        custom_worker.write_text(
            "#!/usr/bin/python3\n"
            "import json, pathlib\n"
            "pathlib.Path('README.md').write_text('updated by worker\\n')\n"
            "pathlib.Path('__pycache__').mkdir(exist_ok=True)\n"
            "pathlib.Path('__pycache__/noise.cpython-314.pyc').write_bytes(b'00')\n"
            "rep = {'status': 'done', 'summary': 'Updated README', 'changed_files': ['README.md']}\n"
            "txt = 'ok\\n```claive-report\\n' + json.dumps(rep) + '\\n```\\n'\n"
            "print(json.dumps({'kind': 'session', 'session_id': 's-w1'}), flush=True)\n"
            "print(json.dumps({'kind': 'model'}), flush=True)\n"
            "print(json.dumps({'kind': 'terminal', 'status': 'completed', 'text': txt}), flush=True)\n"
        )
        custom_worker.chmod(0o755)
        opened, wt_job = self.launch(
            "open", "--detach",
            "--worktree", "--verify-in-worktree",
            "--owned-path", "README.md",
            "--check", "readme=grep -q 'updated by worker' README.md",
            "--report",
            prompt=mut_prompt,
            ANTIGRAVITY_CONVERSATION_ID="conv-777",
            FIXTURE_WORKER_BINARY=str(custom_worker),
        )
        self.assertEqual(opened.returncode, 0, opened.stderr)
        self.assertEqual(self.cli("wait", wt_job, "--timeout", "10").returncode, 0)

        # PreInvocation hook injects the settled event for conv-777 and shows clean changed_files (no .pyc!)
        fired_hook = subprocess.run(
            [FIXTURE_CLI, "hook", "pre-turn", "--harness", "agy", "--agy-hook"],
            input=json.dumps({"hookEventName": "PreInvocation", "conversationId": "conv-777"}),
            env=self.env, text=True, capture_output=True, timeout=10,
        )
        self.assertEqual(fired_hook.returncode, 0)
        hook_json = json.loads(fired_hook.stdout)
        injected_msg = hook_json["injectSteps"][0]["systemMessage"]["systemMessage"]
        self.assertIn(wt_job, injected_msg)
        self.assertNotIn(".pyc", injected_msg)

        # Top-level `claive apply` refuses before `claive verify`, then applies & closes cleanly after verify!
        unverified = self.cli("apply", wt_job)
        self.assertNotEqual(unverified.returncode, 0)
        self.assertIn("verification status", unverified.stderr)

        self.assertEqual(self.cli("verify", wt_job).returncode, 0)
        applied = self.cli("apply", wt_job, "--close", "--json")
        self.assertEqual(applied.returncode, 0, applied.stderr)
        apply_data = json.loads(applied.stdout)
        self.assertEqual(apply_data["applied"], ["README.md"])
        self.assertTrue(apply_data["closed"])
        self.assertEqual((self.workspace / "README.md").read_text(), "updated by worker\n")

    def test_wait_all_barrier_prompt_stdin_inline_and_agy_output_schema(self):
        # 1. --prompt (inline) and --prompt-file - (stdin heredoc) on start/open/followup
        r_inline = self.cli(
            "open", "--detach", "--engine", "fixture",
            "--workspace", str(self.workspace),
            "--prompt", "INLINE PROMPT ONE",
            "--alias", "p-one",
        )
        self.assertEqual(r_inline.returncode, 0, r_inline.stderr)
        j_one = re.search(r"Worker ([0-9a-f]{12})", r_inline.stdout).group(1)

        r_stdin = subprocess.run(
            [FIXTURE_CLI, "start", "--engine", "fixture",
             "--workspace", str(self.workspace), "--prompt-file", "-", "--alias", "p-two"],
            input="STDIN PROMPT TWO\n",
            env=dict(self.env, FIXTURE_MODE="brief"),
            text=True, capture_output=True, timeout=15,
        )
        self.assertEqual(r_stdin.returncode, 0, r_stdin.stderr)
        j_two = re.search(r"Worker ([0-9a-f]{12})", r_stdin.stdout).group(1)

        # 2. claive wait @p-one @p-two --all waits until BOTH workers settle!
        waited_all = self.cli("wait", "@p-one", "@p-two", "--all", "--json", "--timeout", "15")
        self.assertEqual(waited_all.returncode, 0, waited_all.stderr)
        all_data = json.loads(waited_all.stdout)
        self.assertEqual(all_data["event"], "workers")
        self.assertEqual(all_data["pending_workers"], [])
        self.assertEqual([w["id"] for w in all_data["workers"]], [j_one, j_two])

        # Follow-up via stdin (`--prompt-file -`)
        fu_stdin = subprocess.run(
            [FIXTURE_CLI, "followup", "@p-one", "--prompt-file", "-"],
            input="FOLLOWUP STDIN\n",
            env=self.env, text=True, capture_output=True, timeout=10,
        )
        self.assertEqual(fu_stdin.returncode, 0, fu_stdin.stderr)
        self.assertEqual(self.cli("wait", "@p-one", "--timeout", "10").returncode, 0)
        self.cli("close", "@p-one")

        # 3. claive wait --all early short-circuits on failure while slow sibling is still running
        _, slow_id = self.launch("start", FIXTURE_MODE="slow")
        _, fail_id = self.launch("start", FIXTURE_MODE="terminal-failure")
        try:
            short = self.cli("wait", slow_id, fail_id, "--all", "--json", "--timeout", "10")
            self.assertEqual(short.returncode, 1, short.stderr)
            short_data = json.loads(short.stdout)
            self.assertEqual(short_data["worker"], fail_id)
            self.assertEqual(short_data["pending_workers"], [slow_id])
        finally:
            self.cli("cancel", slow_id)

        # 4. AGY --output-schema passes --json-schema
        schema_file = self.path / "out-schema.json"
        schema_file.write_text(json.dumps({"type": "object", "properties": {"ok": {"type": "boolean"}}}))
        fake_agy = Path(__file__).resolve().parent / "fake_agy.py"
        agy_schema = self.cli(
            "start", "--engine", "agy", "--model", "gemini-3.8-flash", "--effort", "low",
            "--output-schema", str(schema_file),
            "--workspace", str(self.workspace), "--prompt", "Return ok=true",
            AGY_WORKER_BINARY=str(fake_agy),
        )
        self.assertEqual(agy_schema.returncode, 0, agy_schema.stderr)
        agy_id = re.search(r"Worker ([0-9a-f]{12})", agy_schema.stdout).group(1)
        agy_state = json.loads(self.cli("show", agy_id, "--json").stdout)
        self.assertIn("--json-schema", agy_state["command"])
        self.cli("cancel", agy_id)


if __name__ == "__main__":
    unittest.main()
