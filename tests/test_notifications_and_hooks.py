"""Tests for notification config, routing/receipts, and harness lifecycle hooks."""
import hermetic  # noqa: F401
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

from claivelib import config, hooks, messaging, notifications


class SandboxTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="claive-notif-hooks-")
        self.root = Path(self.temp.name)
        self.state_dir = self.root / "state"
        self.state_dir.mkdir(parents=True)
        self.xdg_dir = self.root / "xdg"
        (self.xdg_dir / "claive").mkdir(parents=True)
        self.prev_env = {
            key: os.environ.get(key)
            for key in (
                "CLAIVE_DIR",
                "CLAIVE_CONFIG",
                "XDG_CONFIG_HOME",
                "CLAIVE_NOTIFY_CMD",
                "CLAIVE_PARENT_SESSION",
                "CLAIVE_PARENT_HARNESS",
                "CLAUDE_SESSION_ID",
                "CODEX_THREAD_ID",
                "AGY_CONVERSATION_ID",
            )
        }
        os.environ["CLAIVE_DIR"] = str(self.state_dir)
        os.environ["XDG_CONFIG_HOME"] = str(self.xdg_dir)
        os.environ.pop("CLAIVE_CONFIG", None)
        os.environ.pop("CLAIVE_NOTIFY_CMD", None)
        os.environ.pop("CLAIVE_PARENT_SESSION", None)
        os.environ.pop("CLAIVE_PARENT_HARNESS", None)
        os.environ.pop("CLAUDE_SESSION_ID", None)
        os.environ.pop("CODEX_THREAD_ID", None)
        os.environ.pop("AGY_CONVERSATION_ID", None)

    def tearDown(self):
        config.set_overrides()
        for key, value in self.prev_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp.cleanup()

    def write_config(self, payload: dict):
        path = self.xdg_dir / "claive" / "config.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def make_worker(self, job_id="aaaa1111bbbb", *, status="running", label="worker-task",
                    parent_session=None, parked=False, needs_parent=None, launch=None,
                    reusable=False, phase=None, queue_depth=0):
        job_dir = self.state_dir / job_id
        (job_dir / "requests").mkdir(parents=True, exist_ok=True)
        for idx in range(queue_depth):
            (job_dir / "requests" / f"{idx:04d}-req.json").write_text("{}", encoding="utf-8")
        state = {
            "schema_version": 2,
            "id": job_id,
            "label": label,
            "engine": "pi",
            "model": "test-model",
            "workspace": str(self.root),
            "status": status,
            "started_at": time.time(),
            "turn": 1,
            "reusable": reusable,
            "launch": launch or {"engine": "pi", "read_only": False},
        }
        if parent_session is not None:
            state["parent_session"] = parent_session
        if parked:
            state["parked"] = True
        if needs_parent is not None:
            state["needs_parent"] = needs_parent
        if phase is not None:
            state["phase"] = phase
        (job_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
        return job_dir, state

    def append_raw_event(self, event: dict):
        inbox_path = self.state_dir / "inbox.jsonl"
        with inbox_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event) + "\n")


class ConfigNotificationsTests(SandboxTestCase):
    def test_valid_notifications_config_and_normalization(self):
        self.write_config({
            "notifications": {
                "enabled": True,
                "sinks": [
                    {
                        "name": "desktop-alerts",
                        "command": "echo hi",
                        "on": ["ask", "failed", "parked", "unwatched"],
                        "timeout": 10,
                    },
                    {
                        "name": "coord-wake",
                        "worker": "@arm_coord",
                        "on": ["turn", "batch"],
                    },
                    {
                        "name": "local-hook",
                        "url": "https://127.0.0.1:8443/hook",
                        "enabled": False,
                    },
                    {
                        "name": "hex-worker",
                        "worker": "0123456789ab",
                    },
                ],
            }
        })
        norm = config.notifications_config()
        self.assertTrue(norm["enabled"])
        self.assertEqual(len(norm["sinks"]), 4)
        self.assertEqual(norm["sinks"][0]["kind"], "command")
        self.assertEqual(norm["sinks"][0]["timeout"], 10.0)
        self.assertTrue(norm["sinks"][0]["enabled"])
        self.assertEqual(norm["sinks"][1]["kind"], "worker")
        self.assertEqual(norm["sinks"][1]["worker"], "@arm_coord")
        self.assertEqual(norm["sinks"][2]["on"], ["all"])
        self.assertFalse(norm["sinks"][2]["enabled"])

    def test_unconfigured_returns_defaults(self):
        self.assertEqual(config.notifications_config(), {"enabled": True, "sinks": []})

    def test_invalid_notifications_shapes_rejected(self):
        bad_configs = [
            ({"notifications": "nope"}, "notifications must be an object"),
            ({"notifications": {"extra": 1}}, "unknown keys extra"),
            ({"notifications": {"enabled": "yes"}}, "notifications.enabled"),
            ({"notifications": {"sinks": {}}}, "notifications.sinks must be a list"),
            ({"notifications": {"sinks": ["bad"]}}, "notifications.sinks[0] must be an object"),
            ({"notifications": {"sinks": [{"name": "a", "command": "x", "bad": 1}]}}, "unknown keys bad"),
            ({"notifications": {"sinks": [{"name": "bad name!", "command": "x"}]}}, "notifications.sinks[0].name"),
            (
                {"notifications": {"sinks": [
                    {"name": "dup", "command": "x"},
                    {"name": "dup", "command": "y"},
                ]}},
                "duplicate notification sink dup",
            ),
            ({"notifications": {"sinks": [{"name": "a", "command": "x", "enabled": "yes"}]}}, "enabled must be"),
            ({"notifications": {"sinks": [{"name": "a"}]}}, "exactly one of command, worker, or url"),
            (
                {"notifications": {"sinks": [{"name": "a", "command": "x", "url": "http://127.0.0.1"}]}},
                "exactly one of command, worker, or url",
            ),
            ({"notifications": {"sinks": [{"name": "a", "command": "   "}]}}, "command must be a nonempty string"),
            ({"notifications": {"sinks": [{"name": "a", "url": "ftp://example.com"}]}}, "http:// or https://"),
            ({"notifications": {"sinks": [{"name": "a", "worker": "bad worker"}]}}, "whitespace"),
            ({"notifications": {"sinks": [{"name": "a", "worker": "@BAD"}]}}, "notifications.sinks[0].worker"),
            ({"notifications": {"sinks": [{"name": "a", "command": "x", "on": []}]}}, "nonempty list"),
            ({"notifications": {"sinks": [{"name": "a", "command": "x", "on": ["bogus"]}]}}, "unknown trigger"),
            ({"notifications": {"sinks": [{"name": "a", "command": "x", "timeout": 0}]}}, "timeout"),
            ({"notifications": {"sinks": [{"name": "a", "command": "x", "timeout": 121}]}}, "timeout"),
            ({"notifications": {"sinks": [{"name": "a", "command": "x", "timeout": True}]}}, "timeout"),
        ]
        for payload, fragment in bad_configs:
            with self.subTest(fragment=fragment, payload=payload):
                self.write_config(payload)
                with self.assertRaises(ValueError) as caught:
                    config.load()
                self.assertIn(fragment, str(caught.exception))

    def test_notifications_config_safe_ignores_bad_config(self):
        self.write_config({"notifications": "invalid"})
        with self.assertRaises(ValueError):
            config.notifications_config()
        self.assertEqual(config.notifications_config(safe=True), {"enabled": True, "sinks": []})


class NotificationRouterTests(SandboxTestCase):
    def test_event_summary_strips_ansi_and_newlines(self):
        event = {
            "type": "turn",
            "id": "abcd1234abcd",
            "status": "completed",
            "code": 0,
            "label": "\x1b[31mred\x1b[0m\nline2",
        }
        summary = notifications.event_summary(event)
        self.assertNotIn("\x1b", summary)
        self.assertNotIn("\n", summary)
        self.assertIn("red line2", summary)

    def test_matches_triggers(self):
        ask_ev = {"type": "turn", "status": "completed", "code": 3, "needs_parent": {"question": "Which?"}}
        done_ev = {"type": "turn", "status": "completed", "code": 0, "needs_parent": None}
        fail_ev = {"type": "turn", "status": "failed", "code": 1, "needs_parent": None}
        code_fail_ev = {"type": "batch", "status": "done", "code": 2}

        self.assertTrue(notifications.matches_triggers(["ask"], ask_ev))
        self.assertFalse(notifications.matches_triggers(["completed"], ask_ev))
        self.assertFalse(notifications.matches_triggers(["failed"], ask_ev))

        self.assertTrue(notifications.matches_triggers(["completed"], done_ev))
        self.assertFalse(notifications.matches_triggers(["failed"], done_ev))

        self.assertTrue(notifications.matches_triggers(["failed"], fail_ev))
        self.assertTrue(notifications.matches_triggers(["failed"], code_fail_ev))
        self.assertTrue(notifications.matches_triggers(["batch"], code_fail_ev))

        self.assertTrue(notifications.matches_triggers(["parked"], done_ev, parked=True, watched=True))
        self.assertFalse(notifications.matches_triggers(["unwatched"], done_ev, parked=False, watched=True))
        self.assertTrue(notifications.matches_triggers(["unwatched"], done_ev, parked=False, watched=False))

    def test_collect_sinks_deduplicates_identical_launch_and_env_cmd(self):
        os.environ["CLAIVE_NOTIFY_CMD"] = "echo same"
        state = {"launch": {"notify_cmd": "echo same", "notify_worker": "@coord"}}
        sinks = notifications.collect_sinks({"type": "turn"}, state=state)
        names = [s["name"] for s in sinks]
        self.assertEqual(names, ["env-notify-cmd", "worker-notify-target"])

        state_diff = {"launch": {"notify_cmd": "echo other"}}
        sinks_diff = notifications.collect_sinks({"type": "turn"}, state=state_diff)
        self.assertEqual([s["name"] for s in sinks_diff], ["env-notify-cmd", "worker-notify-cmd"])

    def test_dispatch_command_sink_env_stdin_and_receipts(self):
        out_json = self.root / "event.json"
        out_env = self.root / "env.txt"
        self.make_worker("111122223333", parent_session="sess-abc", parked=True)
        self.write_config({
            "notifications": {
                "enabled": True,
                "sinks": [
                    {
                        "name": "capture",
                        "command": (
                            f"cat > {out_json}; "
                            f"printf '%s|%s|%s|%s|%s' "
                            "\"$CLAIVE_EVENT_ID\" \"$CLAIVE_EVENT_STATUS\" \"$CLAIVE_EVENT_CODE\" "
                            f"\"$CLAIVE_EVENT_TYPE\" \"$CLAIVE_EVENT_SESSION\" > {out_env}"
                        ),
                        "on": ["parked"],
                        "timeout": 5,
                    },
                    {
                        "name": "broken",
                        "command": "echo 'boom' >&2; exit 9",
                        "on": ["all"],
                    },
                    {
                        "name": "disabled-sink",
                        "command": "exit 1",
                        "enabled": False,
                    },
                ],
            }
        })
        event = {
            "type": "turn",
            "id": "111122223333",
            "label": "build",
            "status": "completed",
            "code": 0,
            "needs_parent": None,
        }
        receipts = notifications.dispatch(event)
        self.assertEqual([r["sink"] for r in receipts], ["capture", "broken"])
        self.assertEqual(receipts[0]["status"], "ok")
        self.assertEqual(receipts[0]["exit_code"], 0)
        self.assertEqual(receipts[1]["status"], "failed")
        self.assertEqual(receipts[1]["exit_code"], 9)
        self.assertIn("boom", receipts[1]["error"])

        self.assertEqual(json.loads(out_json.read_text(encoding="utf-8"))["id"], "111122223333")
        self.assertEqual(out_env.read_text(encoding="utf-8"), "111122223333|completed|0|turn|sess-abc")

        listed = notifications.list_receipts(limit=10)
        self.assertEqual([r["sink"] for r in listed], ["broken", "capture"])
        self.assertEqual(len(notifications.list_receipts(limit=1)), 1)

    def test_command_sink_detached_timeout(self):
        self.write_config({
            "notifications": {
                "sinks": [{"name": "slow", "command": "sleep 1", "timeout": 0.1}]
            }
        })
        receipts = notifications.dispatch({"type": "turn", "id": "deadbeef1234", "status": "completed", "code": 0})
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["status"], "detached_timeout")

    def test_worker_sink_and_self_delivery_guard(self):
        coord_dir, _ = self.make_worker("cccc11112222", status="idle", reusable=True, label="coord")
        self.make_worker(
            "dddd11112222",
            status="completed",
            launch={"engine": "pi", "read_only": False, "notify_worker": "cccc11112222"},
        )
        with mock.patch.object(messaging, "send", return_value=({"id": "r1"}, False)) as mocked_send:
            receipts = notifications.dispatch({
                "type": "turn",
                "id": "dddd11112222",
                "label": "subtask",
                "status": "completed",
                "code": 0,
                "needs_parent": None,
            })
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["status"], "ok")
        mocked_send.assert_called_once()
        args, kwargs = mocked_send.call_args
        self.assertEqual(args[0], coord_dir)
        self.assertEqual(kwargs["mode"], "wake")
        self.assertIn("[Claive Background Event]", kwargs["message"])

        # Self-delivery guard: event from coord itself must not wake coord
        self.make_worker(
            "cccc11112222",
            status="idle",
            reusable=True,
            launch={"engine": "pi", "read_only": False, "notify_worker": "cccc11112222"},
        )
        with mock.patch.object(messaging, "send") as mocked_send:
            receipts = notifications.dispatch({
                "type": "turn",
                "id": "cccc11112222",
                "status": "completed",
                "code": 0,
                "needs_parent": None,
            })
        mocked_send.assert_not_called()
        self.assertEqual(receipts, [])

    def test_url_sink_success_and_http_error(self):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length).decode("utf-8")
                received.append((self.path, json.loads(body)))
                code = 500 if self.path == "/fail" else 200
                self.send_response(code)
                self.end_headers()

            def log_message(self, format, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.write_config({
                "notifications": {
                    "sinks": [
                        {"name": "ok-hook", "url": f"http://127.0.0.1:{port}/ok", "timeout": 2},
                        {"name": "fail-hook", "url": f"http://127.0.0.1:{port}/fail", "timeout": 2},
                    ]
                }
            })
            receipts = notifications.dispatch({"type": "batch", "id": "b1", "status": "done", "code": 0})
            self.assertEqual(len(receipts), 2)
            self.assertEqual(receipts[0]["status"], "ok")
            self.assertEqual(receipts[1]["status"], "failed")
            self.assertEqual(receipts[1]["exit_code"], 500)
            self.assertEqual(len(received), 2)
            self.assertEqual(received[0][1]["id"], "b1")
        finally:
            server.shutdown()
            server.server_close()

    def test_dispatch_never_throws_and_uses_watchers_when_present(self):
        fake_watchers = types.ModuleType("claivelib.watchers")
        fake_watchers.is_worker_watched = lambda target: target.get("id") == "watched11111"
        self.write_config({
            "notifications": {
                "sinks": [
                    {"name": "unwatched-only", "command": "true", "on": ["unwatched"]},
                ]
            }
        })
        with mock.patch.dict(sys.modules, {"claivelib.watchers": fake_watchers}):
            r_watched = notifications.dispatch({"type": "turn", "id": "watched11111", "status": "completed", "code": 0})
            r_unwatched = notifications.dispatch({"type": "turn", "id": "other2222222", "status": "completed", "code": 0})
        self.assertEqual(r_watched, [])
        self.assertEqual(len(r_unwatched), 1)
        self.assertEqual(r_unwatched[0]["status"], "ok")
        self.assertEqual(notifications.dispatch(None), [])


class HooksTests(SandboxTestCase):
    def test_session_and_harness_resolution(self):
        sess, har = hooks._resolve_session_and_harness(env={"CLAUDE_SESSION_ID": "abc/123"})
        self.assertEqual(sess, "claude-abc-123")
        self.assertEqual(har, "claude")

        sess, har = hooks._resolve_session_and_harness(env={"CODEX_THREAD_ID": "th_99"})
        self.assertEqual(sess, "codex-th_99")
        self.assertEqual(har, "codex")

        sess, har = hooks._resolve_session_and_harness(env={"ANTIGRAVITY_CONVERSATION_ID": "conv1"})
        self.assertEqual(sess, "agy-conv1")
        self.assertEqual(har, "agy")

        with self.assertRaises(ValueError):
            hooks._resolve_session_and_harness(harness="unknown-harness")

    def test_pre_turn_silent_when_nothing_pending(self):
        code, out = hooks.run_pre_turn(session="s1", harness="claude")
        self.assertEqual(code, 0)
        self.assertEqual(out, "")

        code, out_json = hooks.run_pre_turn(session="s1", harness="claude", as_json=True)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out_json), {"events": [], "open_asks": []})

    def test_pre_turn_delivers_events_and_open_asks_and_respects_peek(self):
        self.make_worker(
            "aaaa00001111",
            status="idle",
            reusable=True,
            parent_session="s1",
            label="ask-worker",
            needs_parent={"kind": "needs_decision", "question": "Use SQLite\x1b[31m?\x1b[0m"},
        )
        # Worker from a different session must not leak into s1
        self.make_worker(
            "bbbb00002222",
            status="idle",
            reusable=True,
            parent_session="s2",
            needs_parent={"kind": "needs_decision", "question": "Other session"},
        )
        self.append_raw_event({
            "type": "turn", "id": "aaaa00001111", "label": "ask-worker", "status": "completed",
            "code": 3, "needs_parent": {"question": "Use SQLite?"}, "parent_session": "s1",
        })
        self.append_raw_event({
            "type": "turn", "id": "bbbb00002222", "label": "other", "status": "completed",
            "code": 0, "needs_parent": None, "parent_session": "s2",
        })

        # Peek does not consume
        code, peek_out = hooks.run_pre_turn(session="s1", harness="claude", peek=True, as_json=True)
        self.assertEqual(code, 0)
        payload = json.loads(peek_out)
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "PreToolUse")
        ctx = payload["hookSpecificOutput"]["additionalContext"]
        self.assertIn("<claive-notification>", ctx)
        self.assertIn("Use SQLite?", ctx)
        self.assertNotIn("\x1b", ctx)
        self.assertNotIn("Other session", ctx)
        self.assertEqual(len(payload["events"]), 1)
        self.assertEqual(len(payload["open_asks"]), 1)

        # Consuming run_post_tool drains the event
        code, text_out = hooks.run_post_tool(session="s1", harness="agy")
        self.assertEqual(code, 0)
        self.assertIn("aaaa00001111", text_out)

        # Next run_pre_turn sees only the still-open ASK (no events)
        code, next_json = hooks.run_pre_turn(session="s1", harness="generic", as_json=True)
        self.assertEqual(code, 0)
        next_payload = json.loads(next_json)
        self.assertEqual(next_payload["events"], [])
        self.assertEqual(len(next_payload["open_asks"]), 1)

    def test_stop_blocks_unread_events_and_unwatched_workers_and_exempts_parked_and_idle(self):
        # Idle worker with empty queue does not block
        self.make_worker("000011112222", status="idle", reusable=True, parent_session="s1", queue_depth=0)
        # Parked running worker does not block
        self.make_worker("000011113333", status="running", parent_session="s1", parked=True)
        # Worker from another session does not block s1
        self.make_worker("000011114444", status="running", parent_session="s2")
        code, out = hooks.run_stop(session="s1", harness="claude", as_json=True)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["decision"], "approve")

        # Active running unwatched worker blocks on reactive harness
        self.make_worker("000011115555", status="running", parent_session="s1", label="bg-job")
        code, out = hooks.run_stop(session="s1", harness="claude", mode="block", as_json=True)
        self.assertEqual(code, 2)
        payload = json.loads(out)
        self.assertEqual(payload["decision"], "block")
        self.assertIn("run_in_background=true", payload["reason"])
        self.assertIn("000011115555", payload["reason"])

        # Warn mode returns exit 0 with remediation
        code, warn_out = hooks.run_stop(session="s1", harness="agy", mode="warn")
        self.assertEqual(code, 0)
        self.assertIn("WaitMsBeforeAsync", warn_out)

        # When watched by a live watcher: reactive harness approves, codex still blocks
        fake_watchers = types.ModuleType("claivelib.watchers")
        fake_watchers.is_worker_watched = lambda state: state.get("id") == "000011115555"
        fake_watchers.watch_status = lambda parent_session=None: {"watchers": [{"id": "w1", "pid": os.getpid()}]}
        with mock.patch.dict(sys.modules, {"claivelib.watchers": fake_watchers}):
            code_claude, _ = hooks.run_stop(session="s1", harness="claude")
            self.assertEqual(code_claude, 0)

            code_codex, out_codex = hooks.run_stop(session="s1", harness="codex")
            self.assertEqual(code_codex, 2)
            self.assertIn("Codex cannot wake from background processes", out_codex)
            self.assertIn("claive wait 000011115555 --timeout 300", out_codex)

    def test_stop_blocks_on_unread_events_even_with_no_running_workers(self):
        self.append_raw_event({
            "type": "turn", "id": "999900001111", "label": "done", "status": "completed",
            "code": 0, "needs_parent": None, "parent_session": "s1",
        })
        code, out = hooks.run_stop(session="s1", harness="agy", mode="block")
        self.assertEqual(code, 2)
        self.assertIn("999900001111", out)

        # Unread event was consumed by stop delivery, so repeating stop approves
        code2, out2 = hooks.run_stop(session="s1", harness="agy", mode="block")
        self.assertEqual(code2, 0)
        self.assertEqual(out2, "")

    def test_status_reports_breakdown_without_consuming_events(self):
        self.make_worker("111100001111", status="running", parent_session="s1", label="w-unwatched")
        self.make_worker("111100002222", status="running", parent_session="s1", label="w-watched")
        self.make_worker("111100003333", status="running", parent_session="s1", parked=True, label="w-parked")
        self.append_raw_event({
            "type": "turn", "id": "111100001111", "status": "completed",
            "code": 0, "needs_parent": None, "parent_session": "s1",
        })
        fake_watchers = types.ModuleType("claivelib.watchers")
        fake_watchers.is_worker_watched = lambda state: state.get("id") == "111100002222"
        fake_watchers.watch_status = lambda parent_session=None: {"watchers": [{"id": "w1"}]}
        with mock.patch.dict(sys.modules, {"claivelib.watchers": fake_watchers}):
            code, out = hooks.run_status(session="s1", harness="claude", as_json=True)
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertTrue(data["unread"])
        self.assertEqual(data["counts"]["active"], 3)
        self.assertEqual(data["counts"]["watched"], 1)
        self.assertEqual(data["counts"]["unwatched"], 1)
        self.assertEqual(data["counts"]["parked"], 1)
        self.assertEqual(data["counts"]["live_watchers"], 1)

        # Ensure run_status did not consume the inbox event
        _, pre = hooks.run_pre_turn(session="s1", harness="claude", as_json=True)
        self.assertEqual(len(json.loads(pre)["events"]), 1)


if __name__ == "__main__":
    unittest.main()
