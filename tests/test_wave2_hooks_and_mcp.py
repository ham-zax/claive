"""Tests for Wave 2 Slice E: workspace session fallback, salvageable digests, AGY PreInvocation, and MCP lifecycle."""
import hermetic  # noqa: F401
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "bin"))

from claivelib import acceptance, cli, hooks, inbox, mcp_server  # noqa: E402


class InboxWorkspaceAndDigestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="claive-w2e-inbox-")
        self.base = Path(self.temp.name)
        self.registry = self.base / "registry"
        self.registry.mkdir(parents=True)
        self.workspace = self.base / "repo"
        self.workspace.mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def test_resolve_parent_session_workspace_fallback_and_precedence(self):
        self.assertIsNone(inbox.resolve_parent_session(env={}))
        self.assertIsNone(inbox.resolve_parent_session(env={}, workspace=""))
        self.assertIsNone(inbox.resolve_parent_session(env={}, workspace="   "))

        expected_hash = hashlib.sha1(str(self.workspace.resolve()).encode("utf-8")).hexdigest()[:10]
        expected = f"ws-{expected_hash}"

        self.assertEqual(inbox.resolve_parent_session(env={}, workspace=str(self.workspace)), expected)
        self.assertEqual(inbox.resolve_parent_session(env={}, workspace=str(self.workspace) + "/."), expected)

        # Explicit and env vars still take precedence over workspace fallback.
        self.assertEqual(
            inbox.resolve_parent_session(explicit="my-sess", env={}, workspace=str(self.workspace)),
            "my-sess",
        )
        self.assertEqual(
            inbox.resolve_parent_session(env={"CLAIVE_PARENT_SESSION": "env-sess"}, workspace=str(self.workspace)),
            "env-sess",
        )
        self.assertEqual(
            inbox.resolve_parent_session(env={"CLAUDE_SESSION_ID": "c1"}, workspace=str(self.workspace)),
            "claude-c1",
        )

    def test_build_digest_filters_noise_and_flags_salvageable_timeouts(self):
        worker_id = "abcdef012345"
        job_dir = self.registry / worker_id
        ev_dir = job_dir / "evidence" / "turn-0001"
        ev_dir.mkdir(parents=True)
        (ev_dir / "execution.json").write_text(
            json.dumps({
                "changed_paths": [
                    "__pycache__/mod.cpython-312.pyc",
                    ".pytest_cache/v/cache/nodeids",
                    ".mypy_cache/3.12/a.json",
                    ".ruff_cache/0.1/x",
                    ".tox/py/lib",
                    ".nox/test/lib",
                    "node_modules/pkg/index.js",
                    ".venv/bin/python",
                    "venv/bin/pytest",
                    ".git/index",
                    "foo.pyo",
                    "launch.lock",
                    "src/real_change.py",
                    "tests/test_real.py",
                ]
            }),
            encoding="utf-8",
        )
        state = {
            "id": worker_id,
            "turn": 1,
            "workspace": str(self.workspace),
            "status": "failed",
            "error": "turn timed out",
        }
        (job_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")

        for failure_kind in ("timeout", "stall", "tool_idle_timeout"):
            event = {
                "seq": 7,
                "type": "turn",
                "id": worker_id,
                "label": "impl",
                "status": "failed",
                "code": 124,
                "failure_kind": failure_kind,
            }
            digest = inbox.build_digest(event, state=state, root=self.registry)
            self.assertEqual(digest["files_touched"], ["src/real_change.py", "tests/test_real.py"])
            self.assertEqual(digest["files_touched_total"], 2)
            self.assertTrue(digest.get("salvageable"))
            self.assertEqual(digest.get("salvageable_files"), ["src/real_change.py", "tests/test_real.py"])
            event["digest"] = digest
            text = inbox.format_text([event])
            self.assertIn("[salvageable edits in worktree]", text)

        # Non-timeout failures or completed turns are not marked salvageable.
        non_timeout = {
            "seq": 8,
            "type": "turn",
            "id": worker_id,
            "label": "impl",
            "status": "failed",
            "code": 1,
            "failure_kind": "error",
        }
        d_err = inbox.build_digest(non_timeout, state=state, root=self.registry)
        self.assertNotIn("salvageable", d_err)
        non_timeout["digest"] = d_err
        self.assertNotIn("[salvageable edits in worktree]", inbox.format_text([non_timeout]))

    def test_build_digest_noise_only_not_salvageable_and_uses_acceptance_filter(self):
        worker_id = "123456abcdef"
        job_dir = self.registry / worker_id
        ev_dir = job_dir / "evidence" / "turn-0001"
        ev_dir.mkdir(parents=True)
        (ev_dir / "execution.json").write_text(
            json.dumps({"changed_paths": ["__pycache__/a.pyc", "launch.lock"]}),
            encoding="utf-8",
        )
        state = {"id": worker_id, "turn": 1, "workspace": str(self.workspace), "status": "failed"}
        event = {
            "seq": 1,
            "type": "turn",
            "id": worker_id,
            "label": "noise",
            "status": "failed",
            "code": 124,
            "failure_kind": "timeout",
        }
        digest = inbox.build_digest(event, state=state, root=self.registry)
        self.assertEqual(digest["files_touched"], [])
        self.assertNotIn("salvageable", digest)

        # Delegates to acceptance.filter_noise_paths when present.
        (ev_dir / "execution.json").write_text(
            json.dumps({"changed_paths": ["a.py", "b.tmp"]}),
            encoding="utf-8",
        )
        with mock.patch.object(
            acceptance,
            "filter_noise_paths",
            create=True,
            side_effect=lambda paths, workspace=None, job_dir=None: [p for p in paths if p == "a.py"],
        ) as mocked:
            digest2 = inbox.build_digest(event, state=state, root=self.registry)
        mocked.assert_called_once()
        self.assertEqual(digest2["files_touched"], ["a.py"])
        self.assertTrue(digest2.get("salvageable"))


class AgyHooksTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="claive-w2e-hooks-")
        self.base = Path(self.temp.name)
        self.state_dir = self.base / "state"
        self.state_dir.mkdir(parents=True)
        self.workspace = self.base / "ws"
        self.workspace.mkdir(parents=True)
        self._old_claive_dir = os.environ.get("CLAIVE_DIR")
        os.environ["CLAIVE_DIR"] = str(self.state_dir)

    def tearDown(self):
        if self._old_claive_dir is None:
            os.environ.pop("CLAIVE_DIR", None)
        else:
            os.environ["CLAIVE_DIR"] = self._old_claive_dir
        self.temp.cleanup()

    def test_resolve_session_and_harness_agy_payload_and_workspace_fallback(self):
        sess, har = hooks._resolve_session_and_harness(
            env={},
            stdin_payload={"hookEventName": "PreInvocation", "conversationId": "conv/42"},
        )
        self.assertEqual(har, "agy")
        self.assertEqual(sess, "agy-conv-42")

        sess2, har2 = hooks._resolve_session_and_harness(
            env={},
            stdin_payload={"conversationId": "only-conv"},
        )
        self.assertEqual(har2, "agy")
        self.assertEqual(sess2, "agy-only-conv")

        sess3, har3 = hooks._resolve_session_and_harness(
            env={},
            stdin_payload={"hookEventName": "PreInvocation", "cwd": str(self.workspace)},
        )
        self.assertEqual(har3, "agy")
        self.assertEqual(sess3, inbox.resolve_parent_session(env={}, workspace=str(self.workspace)))

        # Env harness takes precedence over payload heuristic.
        sess4, har4 = hooks._resolve_session_and_harness(
            env={"CLAUDE_SESSION_ID": "cs1"},
            stdin_payload={"hookEventName": "PreInvocation", "conversationId": "conv-ignored"},
        )
        self.assertEqual(har4, "claude")
        self.assertEqual(sess4, "claude-cs1")

    def test_run_pre_turn_agy_preinvocation_envelope(self):
        payload = json.dumps({"hookEventName": "PreInvocation", "conversationId": "agy-100"})
        code, empty_out = hooks.run_pre_turn(env={}, stdin_payload=payload)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(empty_out), {})

        # Append an event matching agy-agy-100.
        inbox_path = self.state_dir / "inbox.jsonl"
        inbox_path.write_text(
            json.dumps({
                "type": "turn",
                "id": "111122223333",
                "label": "build",
                "status": "completed",
                "code": 0,
                "needs_parent": None,
                "parent_session": "agy-agy-100",
                "wake": True,
            }) + "\n",
            encoding="utf-8",
        )
        code, out = hooks.run_pre_turn(env={}, stdin_payload=payload, peek=True)
        self.assertEqual(code, 0)
        parsed = json.loads(out)
        self.assertIn("injectSteps", parsed)
        msg = parsed["injectSteps"][0]["systemMessage"]["systemMessage"]
        self.assertIn("<claive-notification>", msg)
        self.assertIn("111122223333", msg)

        # Explicit agy_hook=True on run_post_tool produces the same envelope and consumes.
        code2, out2 = hooks.run_post_tool(session="agy-agy-100", harness="agy", env={}, agy_hook=True)
        self.assertEqual(code2, 0)
        self.assertIn("injectSteps", json.loads(out2))

        # Subsequent call is empty `{}`.
        code3, out3 = hooks.run_pre_turn(env={}, stdin_payload=payload)
        self.assertEqual(code3, 0)
        self.assertEqual(json.loads(out3), {})

        # Unwatched active worker owned by agy-agy-100 triggers <claive-unwatched-workers> alert!
        w_dir = self.state_dir / "deadbeef1234"
        w_dir.mkdir(parents=True, exist_ok=True)
        (w_dir / "state.json").write_text(
            json.dumps({
                "schema_version": 2,
                "id": "deadbeef1234",
                "label": "long-audit",
                "engine": "pi",
                "model": "gemini-4-hydrogen",
                "workspace": str(self.workspace),
                "status": "running",
                "phase": "running tests",
                "event_steps": 9,
                "started_at": time.time() - 45,
                "turn": 1,
                "parent_session": "agy-agy-100",
                "launch": {"engine": "pi", "read_only": True},
            }),
            encoding="utf-8",
        )
        code_unw, out_unw = hooks.run_pre_turn(env={}, stdin_payload=payload)
        self.assertEqual(code_unw, 0)
        unw_parsed = json.loads(out_unw)
        self.assertIn("injectSteps", unw_parsed)
        unw_msg = unw_parsed["injectSteps"][0]["systemMessage"]["systemMessage"]
        self.assertIn("<claive-unwatched-workers>", unw_msg)
        self.assertIn("deadbeef1234", unw_msg)
        self.assertIn("--heartbeat 600", unw_msg)
        self.assertNotIn("running tests", unw_msg)
        self.assertIn("running tests", unw_parsed["injectSteps"][1]["userMessage"])
        # Clean up dummy worker so legacy test below sees clean state.
        import shutil
        shutil.rmtree(w_dir)

        # Legacy harness="agy", as_json=True (without agy_hook/PreInvocation) keeps existing shape.
        code4, legacy_out = hooks.run_pre_turn(session="agy-agy-100", harness="agy", as_json=True, env={})
        self.assertEqual(code4, 0)
        legacy = json.loads(legacy_out)
        self.assertEqual(legacy["harness"], "agy")
        self.assertEqual(legacy["events"], [])


class McpLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="claive-w2e-mcp-")
        self.base = Path(self.temp.name)
        self.registry = self.base / "registry"
        self.workspace = self.base / "repo"
        self.workspace.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(self.workspace)], check=True)
        (self.workspace / "app.py").write_text("val = 1\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.workspace), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(self.workspace), "-c", "user.name=Test", "-c", "user.email=t@e.invalid",
             "commit", "-qm", "init"],
            check=True,
        )
        self.counter_file = self.base / "turn_count.txt"
        self.worker_bin = self.base / "fake_claude.py"
        self.worker_bin.write_text(
            "#!/usr/bin/python3\n"
            "import json\n"
            "from pathlib import Path\n"
            f"counter = Path({str(self.counter_file)!r})\n"
            "step = int(counter.read_text(encoding='utf-8')) + 1 if counter.exists() else 1\n"
            "counter.write_text(str(step), encoding='utf-8')\n"
            "Path('app.py').write_text(f'val = {step + 1}\\n', encoding='utf-8')\n"
            "if step >= 2:\n"
            "    Path('extra.txt').write_text('from-followup\\n', encoding='utf-8')\n"
            "report = {'status': 'done', 'summary': f'step {step}', 'changed_files': ['app.py'], 'verification': [], 'risks': []}\n"
            "ans = 'Done.\\n```claive-report\\n' + json.dumps(report) + '\\n```'\n"
            "print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': f's{step}'}), flush=True)\n"
            "print(json.dumps({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': ans}]}}), flush=True)\n"
            "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': ans}), flush=True)\n",
            encoding="utf-8",
        )
        self.worker_bin.chmod(0o755)
        self._saved_env = {
            k: os.environ.get(k)
            for k in (
                "CLAIVE_DIR",
                "CLAUDE_WORKER_BINARY",
                "CLAIVE_PARENT_SESSION",
                "CLAUDE_SESSION_ID",
                "CLAUDE_CODE_SESSION_ID",
                "CODEX_THREAD_ID",
                "ANTIGRAVITY_CONVERSATION_ID",
                "AGY_CONVERSATION_ID",
                "OPENCODE_SESSION_ID",
                "PI_SESSION_ID",
            )
        }
        os.environ["CLAIVE_DIR"] = str(self.registry)
        os.environ["CLAUDE_WORKER_BINARY"] = str(self.worker_bin)
        cli.set_launcher_path(REPO / "bin" / "claive")
        for var in (
            "CLAIVE_PARENT_SESSION",
            "CLAUDE_SESSION_ID",
            "CLAUDE_CODE_SESSION_ID",
            "CODEX_THREAD_ID",
            "ANTIGRAVITY_CONVERSATION_ID",
            "AGY_CONVERSATION_ID",
            "OPENCODE_SESSION_ID",
            "PI_SESSION_ID",
        ):
            os.environ.pop(var, None)

    def tearDown(self):
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.temp.cleanup()

    def _start_slow_worker(self, name, sleep_seconds, session=None):
        slow_bin = self.base / f"slow_{name}.py"
        slow_bin.write_text(
            "#!/usr/bin/python3\n"
            "import json, time\n"
            f"print(json.dumps({{'type': 'system', 'subtype': 'init', 'session_id': 'slow-{name}'}}), flush=True)\n"
            f"time.sleep({sleep_seconds})\n"
            "ans = 'Done.'\n"
            "print(json.dumps({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': ans}]}}), flush=True)\n"
            "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': ans}), flush=True)\n",
            encoding="utf-8",
        )
        slow_bin.chmod(0o755)
        os.environ["CLAUDE_WORKER_BINARY"] = str(slow_bin)
        prompt = self.base / f"p_slow_{name}.md"
        prompt.write_text("SLOW\n", encoding="utf-8")
        cmd = [
            sys.executable,
            cli.launcher_path(),
            "start",
            "--workspace",
            str(self.workspace),
            "--prompt-file",
            str(prompt),
            "--engine",
            "claude",
            "--fit-checked",
        ]
        if session is not None:
            cmd.extend(["--session", session])
        before = {state["id"] for state in cli.jobs(limit=None)}
        started = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        self.assertEqual(started.returncode, 0, started.stderr)
        new_ids = {state["id"] for state in cli.jobs(limit=None)} - before
        self.assertEqual(len(new_ids), 1, new_ids)
        return new_ids.pop()

    def _wait_for_workers(self, worker_ids, timeout=20):
        result = subprocess.run(
            [sys.executable, cli.launcher_path(), "wait", *worker_ids, "--all", "--timeout", str(timeout)],
            capture_output=True,
            text=True,
            timeout=timeout + 15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def _run_wait(self, *args, timeout=15):
        return subprocess.run(
            [sys.executable, cli.launcher_path(), "wait", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def test_flat_schemas_include_wave2_tools(self):
        names = mcp_server.validate_flat_schemas()
        for expected in ("claive_followup", "claive_apply", "claive_close"):
            self.assertIn(expected, names)
            self.assertIn(expected, mcp_server.HANDLERS)
        serialized = json.dumps(mcp_server.TOOL_DEFINITIONS)
        for forbidden in mcp_server.FORBIDDEN_SCHEMA_KEYS:
            self.assertNotIn(f'"{forbidden}"', serialized)

    def test_tool_start_parses_multivalue_owned_path_check_and_workspace_session(self):
        recorded_cmds = []

        def fake_run(cmd, timeout=60.0):
            recorded_cmds.append(cmd)
            return 0, "Worker aabbccddeeff started\n", ""

        fake_job = self.registry / "aabbccddeeff"
        fake_job.mkdir(parents=True)
        fake_state = {
            "id": "aabbccddeeff",
            "status": "running",
            "workspace": str(self.workspace),
            "started_at": time.time(),
        }
        (fake_job / "state.json").write_text(json.dumps(fake_state), encoding="utf-8")

        with mock.patch.object(mcp_server, "_run_cli", side_effect=fake_run):
            res = mcp_server._tool_start({
                "workspace": str(self.workspace),
                "prompt": "Do work",
                "owned_path": "bin/a.py, tests/test_a.py\nbin/b.py",
                "check": "lint=ruff check\nunit=python3 -m unittest",
            })

        self.assertEqual(res["worker"]["id"], "aabbccddeeff")
        cmd = recorded_cmds[0]
        owned_args = [cmd[i + 1] for i, tok in enumerate(cmd) if tok == "--owned-path"]
        check_args = [cmd[i + 1] for i, tok in enumerate(cmd) if tok == "--check"]
        session_args = [cmd[i + 1] for i, tok in enumerate(cmd) if tok == "--session"]
        self.assertEqual(owned_args, ["bin/a.py", "tests/test_a.py", "bin/b.py"])
        self.assertEqual(check_args, ["lint=ruff check", "unit=python3 -m unittest"])
        self.assertEqual(session_args, [inbox.resolve_parent_session(env={}, workspace=str(self.workspace))])

    def test_followup_apply_and_close_end_to_end(self):
        prompt1 = self.base / "p1.md"
        prompt1.write_text("TURN1\n", encoding="utf-8")
        opened = subprocess.run(
            [
                sys.executable,
                cli.launcher_path(),
                "open",
                "--detach",
                "--workspace",
                str(self.workspace),
                "--prompt-file",
                str(prompt1),
                "--engine",
                "claude",
                "--worktree",
                "--alias",
                "reusable-wt",
                "--enable-steering",
                "--fit-checked",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(opened.returncode, 0, opened.stderr)
        worker_id = None
        for state in cli.jobs(limit=10):
            if state.get("label") or state.get("id"):
                worker_id = state["id"]
                break
        self.assertIsNotNone(worker_id)

        wait1 = mcp_server.dispatch_tool("claive_wait", {"id": worker_id, "timeout": 15})
        self.assertFalse(wait1["isError"], wait1)
        wait1_data = json.loads(wait1["content"][0]["text"])
        self.assertEqual(wait1_data["worker_status"]["status"], "idle")

        # Follow-up turn via MCP tool.
        fu_res = mcp_server.dispatch_tool(
            "claive_followup",
            {"id": "@reusable-wt", "prompt": "TURN2", "label": "turn-two"},
        )
        self.assertFalse(fu_res["isError"], fu_res)
        fu_data = json.loads(fu_res["content"][0]["text"])
        self.assertTrue(fu_data["queued"])

        wait2 = mcp_server.dispatch_tool("claive_wait", {"id": worker_id, "timeout": 15})
        self.assertFalse(wait2["isError"], wait2)
        wait2_data = json.loads(wait2["content"][0]["text"])
        self.assertEqual(wait2_data["worker_status"]["status"], "idle")

        # Dry-run apply does not modify source workspace or close worker.
        dry_res = mcp_server.dispatch_tool("claive_apply", {"id": worker_id, "dry_run": True, "close": True})
        self.assertFalse(dry_res["isError"], dry_res)
        dry_data = json.loads(dry_res["content"][0]["text"])
        self.assertEqual(dry_data["status"], "dry_run")
        self.assertIn("app.py", dry_data["applied"])
        self.assertIn("extra.txt", dry_data["applied"])
        self.assertFalse(dry_data.get("closed", False))
        self.assertEqual((self.workspace / "app.py").read_text(encoding="utf-8"), "val = 1\n")

        # Real apply with close=True copies edits into source workspace and closes worker.
        apply_res = mcp_server.dispatch_tool("claive_apply", {"id": worker_id, "close": True})
        self.assertFalse(apply_res["isError"], apply_res)
        apply_data = json.loads(apply_res["content"][0]["text"])
        self.assertEqual(apply_data["status"], "applied")
        self.assertTrue(apply_data["closed"])
        self.assertEqual((self.workspace / "app.py").read_text(encoding="utf-8"), "val = 3\n")
        self.assertEqual((self.workspace / "extra.txt").read_text(encoding="utf-8"), "from-followup\n")

        # Poll for supervisor to finish closing after close.request.
        deadline = time.time() + 10.0
        final_state = cli.load(cli.job_path(worker_id))
        while final_state.get("phase") != "session closed" and time.time() < deadline:
            time.sleep(0.05)
            final_state = cli.load(cli.job_path(worker_id))
        self.assertEqual(final_state["status"], "completed")
        self.assertEqual(final_state.get("phase"), "session closed")

    def test_claive_close_directly_closes_open_worker(self):
        prompt1 = self.base / "p_close.md"
        prompt1.write_text("TURN1\n", encoding="utf-8")
        opened = subprocess.run(
            [
                sys.executable,
                cli.launcher_path(),
                "open",
                "--detach",
                "--workspace",
                str(self.workspace),
                "--prompt-file",
                str(prompt1),
                "--engine",
                "claude",
                "--fit-checked",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(opened.returncode, 0, opened.stderr)
        worker_id = cli.jobs(limit=1)[0]["id"]
        wait1 = mcp_server.dispatch_tool("claive_wait", {"id": worker_id, "timeout": 15})
        self.assertFalse(wait1["isError"], wait1)

        close_res = mcp_server.dispatch_tool("claive_close", {"id": worker_id})
        self.assertFalse(close_res["isError"], close_res)
        close_data = json.loads(close_res["content"][0]["text"])
        self.assertTrue(close_data["closed"])

        deadline = time.time() + 10.0
        final_state = cli.load(cli.job_path(worker_id))
        while final_state.get("phase") != "session closed" and time.time() < deadline:
            time.sleep(0.05)
            final_state = cli.load(cli.job_path(worker_id))
        self.assertEqual(final_state["status"], "completed")
        self.assertEqual(final_state.get("phase"), "session closed")

    def test_wait_heartbeat_preview_wakes_early_with_live_progress(self):
        slow_bin = self.base / "slow_claude.py"
        slow_bin.write_text(
            "#!/usr/bin/python3\n"
            "import json, time\n"
            "print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 'slow1'}), flush=True)\n"
            "time.sleep(4.0)\n"
            "ans = 'Done.'\n"
            "print(json.dumps({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': ans}]}}), flush=True)\n"
            "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': ans}), flush=True)\n",
            encoding="utf-8",
        )
        slow_bin.chmod(0o755)
        os.environ["CLAUDE_WORKER_BINARY"] = str(slow_bin)
        prompt = self.base / "p_slow.md"
        prompt.write_text("SLOW\n", encoding="utf-8")
        started = subprocess.run(
            [
                sys.executable,
                cli.launcher_path(),
                "start",
                "--workspace",
                str(self.workspace),
                "--prompt-file",
                str(prompt),
                "--engine",
                "claude",
                "--fit-checked",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(started.returncode, 0, started.stderr)
        worker_id = cli.jobs(limit=1)[0]["id"]

        # CLI wait with --heartbeat 0.3 returns exit 125 with live preview!
        hb_cli = subprocess.run(
            [sys.executable, cli.launcher_path(), "wait", worker_id, "--heartbeat", "0.3", "--timeout", "10"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(hb_cli.returncode, 125, hb_cli.stdout + hb_cli.stderr)
        self.assertIn("Progress preview after 0.3s", hb_cli.stdout)
        self.assertIn(worker_id, hb_cli.stdout)
        self.assertIn("Re-arm wait:", hb_cli.stdout)

        # MCP claive_wait with heartbeat=0.2 also returns event=heartbeat, exit_code=125, previews!
        hb_mcp = mcp_server.dispatch_tool("claive_wait", {"id": worker_id, "heartbeat": 0.2, "timeout": 10})
        self.assertFalse(hb_mcp["isError"], hb_mcp)
        hb_data = json.loads(hb_mcp["content"][0]["text"])
        self.assertEqual(hb_data["event"], "heartbeat")
        self.assertEqual(hb_data["exit_code"], 125)
        self.assertIn(worker_id, hb_data["still_running"])
        self.assertEqual(len(hb_data["previews"]), 1)

        # Final wait finishes exit 0.
        done = mcp_server.dispatch_tool("claive_wait", {"id": worker_id, "timeout": 15})
        self.assertFalse(done["isError"], done)
        done_data = json.loads(done["content"][0]["text"])
        self.assertEqual(done_data["event"], "worker")
        self.assertEqual(done_data["worker"], worker_id)
        self.assertEqual(done_data["exit_code"], 0)

    def test_wait_heartbeat_without_running_workers_times_out_cleanly(self):
        # Selectorless waits have no worker to preview; --heartbeat must not report 125 / "still running: none".
        for selector in (("--inbox", "emptyconsumer"), ("--session", "emptysession")):
            started = time.monotonic()
            result = self._run_wait(*selector, "--heartbeat", "0.2", "--timeout", "0.8", "--json")
            elapsed = time.monotonic() - started
            self.assertEqual(result.returncode, 124, result.stdout + result.stderr)
            data = json.loads(result.stdout)
            self.assertEqual(data["event"], "timeout")
            self.assertIsNone(data["worker"])
            self.assertEqual(data["code"], 124)
            self.assertEqual(data["inbox_events"], [])
            self.assertNotIn("still_running", data)
            self.assertGreaterEqual(elapsed, 0.8)

    def test_wait_heartbeat_excludes_parked_worker_in_session_scope(self):
        worker_id = self._start_slow_worker("parked", 2.0, session="parkedsession")
        parked = subprocess.run(
            [sys.executable, cli.launcher_path(), "park", worker_id],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(parked.returncode, 0, parked.stderr)

        result = self._run_wait("--session", "parkedsession", "--heartbeat", "0.2", "--timeout", "0.8", "--json")
        self.assertEqual(result.returncode, 124, result.stdout + result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["event"], "timeout")
        self.assertNotIn(worker_id, data.get("still_running", []))
        self.assertEqual(data.get("previews", []), [])
        self._wait_for_workers([worker_id])

    def test_wait_inbox_heartbeat_without_session_ignores_other_session_workers(self):
        worker_id = self._start_slow_worker("othersession", 2.0, session="someothers")

        result = self._run_wait("--inbox", "isolatedconsumer", "--heartbeat", "0.2", "--timeout", "0.8", "--json")
        self.assertEqual(result.returncode, 124, result.stdout + result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["event"], "timeout")
        self.assertNotIn(worker_id, data.get("still_running", []))
        self.assertEqual(data.get("previews", []), [])
        self._wait_for_workers([worker_id])

    def test_wait_session_timeout_includes_live_previews(self):
        worker_id = self._start_slow_worker("timeout", 3.0, session="waitsession")

        as_json = self._run_wait("--session", "waitsession", "--timeout", "0.5", "--json")
        self.assertEqual(as_json.returncode, 124, as_json.stdout + as_json.stderr)
        data = json.loads(as_json.stdout)
        self.assertEqual(data["event"], "timeout")
        self.assertEqual(data["worker"], None)
        self.assertEqual(data["code"], 124)
        self.assertEqual(data["still_running"], [worker_id])
        self.assertEqual([preview["id"] for preview in data["previews"]], [worker_id])

        human = self._run_wait("--session", "waitsession", "--timeout", "0.4")
        self.assertEqual(human.returncode, 124, human.stdout + human.stderr)
        self.assertIn(f"Timed out after 0.4s; still running: {worker_id}", human.stdout)
        self._wait_for_workers([worker_id])

    def test_wait_rearm_hint_preserves_scope_flags(self):
        scoped_id = self._start_slow_worker("rearm", 3.0, session="rearmsession")

        scoped = self._run_wait(
            "--session", "rearmsession", "--inbox", "rearmconsumer", "--since-seq", "999999",
            "--heartbeat", "0.2", "--timeout", "10",
        )
        self.assertEqual(scoped.returncode, 125, scoped.stdout + scoped.stderr)
        scoped_hint = next(line for line in scoped.stdout.splitlines() if line.startswith("Re-arm wait:"))
        self.assertIn("--session rearmsession", scoped_hint)
        self.assertIn("--inbox rearmconsumer", scoped_hint)
        self.assertIn("--since-seq 999999", scoped_hint)
        self.assertIn("--heartbeat 0.2", scoped_hint)
        self.assertIn("--timeout 10", scoped_hint)
        self.assertNotIn(scoped_id, scoped_hint)

        first = self._start_slow_worker("rearm-a", 3.0)
        second = self._start_slow_worker("rearm-b", 3.0)

        all_res = self._run_wait(
            first, second, "--all", "--no-fail-fast", "--heartbeat", "0.2", "--timeout", "10",
        )
        self.assertEqual(all_res.returncode, 125, all_res.stdout + all_res.stderr)
        all_hint = next(line for line in all_res.stdout.splitlines() if line.startswith("Re-arm wait:"))
        self.assertIn(first, all_hint)
        self.assertIn(second, all_hint)
        self.assertIn("--all", all_hint)
        self.assertIn("--no-fail-fast", all_hint)
        self.assertIn("--heartbeat 0.2", all_hint)
        self.assertIn("--timeout 10", all_hint)

        any_res = self._run_wait(first, second, "--any", "--heartbeat", "0.2", "--timeout", "10")
        self.assertEqual(any_res.returncode, 125, any_res.stdout + any_res.stderr)
        any_hint = next(line for line in any_res.stdout.splitlines() if line.startswith("Re-arm wait:"))
        self.assertIn(first, any_hint)
        self.assertIn(second, any_hint)
        self.assertIn("--any", any_hint)
        self.assertNotIn("--all", any_hint)
        self.assertNotIn("--no-fail-fast", any_hint)
        self._wait_for_workers([scoped_id, first, second])

    def test_wait_rearm_keeps_failed_workers_and_combined_inbox_scope(self):
        failed = "abcdeffedcba"
        failed_dir = self.registry / failed
        failed_dir.mkdir(parents=True)
        cli.save(failed_dir / "state.json", {
            "id": failed, "status": "failed", "exit_code": 1, "started_at": time.time(),
        })
        running = self._start_slow_worker("rearm-running", 2.0, session="rearmsession")
        try:
            result = self._run_wait(
                failed, running, "--all", "--no-fail-fast", "--session", "rearmsession",
                "--inbox", "rearmconsumer", "--since-seq", "999999", "--heartbeat", "0.2", "--timeout", "10",
            )
            self.assertEqual(result.returncode, 125, result.stdout + result.stderr)
            hint = next(line for line in result.stdout.splitlines() if line.startswith("Re-arm wait:"))
            self.assertIn(f"{failed} {running} --all --no-fail-fast", hint)
            self.assertIn("--session rearmsession --inbox rearmconsumer --since-seq 999999", hint)
        finally:
            self._wait_for_workers([running])


if __name__ == "__main__":
    unittest.main()
