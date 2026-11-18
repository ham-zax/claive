"""Unit tests for watchers.py and session-scoped inbox.py (Layer 1 & Layer 2)."""
import hermetic  # noqa: F401  (must run before claivelib reads the environment)
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "bin"))

from claivelib import acceptance, cli, inbox, watchers


def _write_worker(registry: Path, worker_id: str, *, status="running", label="worker",
                  engine="codex", model="gpt-5", parent_session=None, parked=False,
                  needs_parent=None, phase=None, queue_depth=0, started_at=None):
    wdir = registry / worker_id
    (wdir / "requests").mkdir(parents=True, exist_ok=True)
    state = {
        "id": worker_id,
        "label": label,
        "status": status,
        "engine": engine,
        "model": model,
        "workspace": str(registry),
        "started_at": time.time() if started_at is None else started_at,
        "supervisor_pid": os.getpid(),
        "supervisor_identity": cli.identity(os.getpid()),
    }
    if parent_session is not None:
        state["parent_session"] = parent_session
    if parked:
        state["parked"] = True
    if needs_parent is not None:
        state["needs_parent"] = needs_parent
    if phase is not None:
        state["phase"] = phase
    acceptance.atomic(wdir / "state.json", state)
    for idx in range(queue_depth):
        acceptance.atomic(wdir / "requests" / f"{idx:03d}.json", {"request_id": f"r{idx}", "label": "q"})
    return wdir


class SessionAndInboxChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="claive-inbox-session-")
        self.registry = Path(self.temp.name) / "registry"
        self.registry.mkdir(parents=True, exist_ok=True)
        self._old_claive_dir = os.environ.get("CLAIVE_DIR")
        os.environ["CLAIVE_DIR"] = str(self.registry)

    def tearDown(self):
        if self._old_claive_dir is None:
            os.environ.pop("CLAIVE_DIR", None)
        else:
            os.environ["CLAIVE_DIR"] = self._old_claive_dir
        self.temp.cleanup()

    def test_sanitize_session_normalizes_and_hashes_long_ids(self):
        self.assertIsNone(inbox.sanitize_session(""))
        self.assertIsNone(inbox.sanitize_session("...---..."))
        self.assertEqual(inbox.sanitize_session("..claude/abc:def   ghi--"), "claude-abc-def-ghi")
        self.assertEqual(inbox.sanitize_session("a_b.c-1"), "a_b.c-1")

        long_raw = "claude-" + ("x" * 80) + "/extra"
        sanitized = inbox.sanitize_session(long_raw)
        expected_digest = hashlib.sha256(long_raw.encode("utf-8")).hexdigest()[:12]
        self.assertEqual(len(sanitized), 64)
        self.assertTrue(sanitized.endswith(f"-{expected_digest}"))
        self.assertRegex(sanitized, inbox.SESSION_RE)

    def test_infer_parent_harness_precedence(self):
        self.assertIsNone(inbox.infer_parent_harness({}))
        self.assertEqual(inbox.infer_parent_harness({"CLAIVE_PARENT": "  custom-harness  ", "CLAUDECODE": "1"}),
                         "custom-harness")
        self.assertEqual(inbox.infer_parent_harness({"CLAUDE_SESSION_ID": "s1"}), "claude")
        self.assertEqual(inbox.infer_parent_harness({"CLAUDECODE": "1"}), "claude")
        self.assertEqual(inbox.infer_parent_harness({"CODEX_THREAD_ID": "t1"}), "codex")
        self.assertEqual(inbox.infer_parent_harness({"CODEX_CI": "1"}), "codex")
        self.assertEqual(inbox.infer_parent_harness({"ANTIGRAVITY_CONVERSATION_ID": "c1"}), "agy")
        self.assertEqual(inbox.infer_parent_harness({"AGY_CONVERSATION_ID": "c2"}), "agy")
        self.assertEqual(inbox.infer_parent_harness({"OPENCODE_SESSION_ID": "o1"}), "opencode")
        self.assertEqual(inbox.infer_parent_harness({"PI_SESSION_ID": "p1"}), "pi")

    def test_resolve_parent_session_and_session_consumer(self):
        self.assertIsNone(inbox.resolve_parent_session(env={}))
        self.assertEqual(inbox.resolve_parent_session(explicit="my-session_1", env={"CLAUDE_SESSION_ID": "ignored"}),
                         "my-session_1")
        with self.assertRaisesRegex(ValueError, "invalid session"):
            inbox.resolve_parent_session(explicit="../bad", env={})
        with self.assertRaisesRegex(ValueError, "invalid session"):
            inbox.resolve_parent_session(explicit="", env={})

        self.assertEqual(inbox.resolve_parent_session(env={"CLAIVE_PARENT_SESSION": "env_sess", "CLAUDE_SESSION_ID": "x"}),
                         "env_sess")
        with self.assertRaisesRegex(ValueError, "invalid session"):
            inbox.resolve_parent_session(env={"CLAIVE_PARENT_SESSION": "bad/session"})

        self.assertEqual(inbox.resolve_parent_session(env={"CLAUDE_SESSION_ID": "abc/1", "CODEX_THREAD_ID": "t1"}),
                         "claude-abc-1")
        self.assertEqual(inbox.resolve_parent_session(env={"CODEX_THREAD_ID": "t1", "AGY_CONVERSATION_ID": "a1"}),
                         "codex-t1")
        self.assertEqual(inbox.resolve_parent_session(env={"ANTIGRAVITY_CONVERSATION_ID": "ag1", "AGY_CONVERSATION_ID": "ag2"}),
                         "agy-ag1")
        self.assertEqual(inbox.resolve_parent_session(env={"AGY_CONVERSATION_ID": "ag2", "OPENCODE_SESSION_ID": "op1"}),
                         "agy-ag2")
        self.assertEqual(inbox.resolve_parent_session(env={"OPENCODE_SESSION_ID": "op1", "PI_SESSION_ID": "pi1"}),
                         "opencode-op1")
        self.assertEqual(inbox.resolve_parent_session(env={"PI_SESSION_ID": "pi1"}), "pi-pi1")

        self.assertEqual(inbox.session_consumer("claude-123"), "session-claude-123")
        long_valid = "a" * 64
        consumer = inbox.session_consumer(long_valid)
        self.assertLessEqual(len(consumer), 64)
        self.assertRegex(consumer, inbox.CONSUMER_RE)
        with self.assertRaisesRegex(ValueError, "invalid session"):
            inbox.session_consumer("bad/session")

    def test_event_builders_preserve_default_keys_and_attach_parent_session(self):
        t_plain = inbox.turn_event("j1", "lbl", 1, "completed", 0, None, None, "done", None, None)
        self.assertNotIn("parent_session", t_plain)
        t_sess = inbox.turn_event("j1", "lbl", 1, "completed", 0, None, None, "done", None, None, parent_session="s1")
        self.assertEqual(t_sess["parent_session"], "s1")

        b_plain = inbox.batch_event("b1", "lbl", "completed", 0, None)
        self.assertNotIn("parent_session", b_plain)
        b_sess = inbox.batch_event("b1", "lbl", "completed", 0, None, parent_session="s1")
        self.assertEqual(b_sess["parent_session"], "s1")

        g_plain = inbox.budget_event("j1", "lbl", 1, 1, 1, 1000, "sid", "next")
        self.assertNotIn("parent_session", g_plain)
        g_sess = inbox.budget_event("j1", "lbl", 1, 1, 1, 1000, "sid", "next", parent_session="s1")
        self.assertEqual(g_sess["parent_session"], "s1")

    def test_filtered_read_new_and_has_unread(self):
        e1 = inbox.turn_event("j1", "a", 1, "completed", 0, None, None, "done", None, "m1", parent_session="s1")
        e2 = inbox.turn_event("j2", "b", 1, "completed", 0, None, None, "done", None, "m2", parent_session="s2")
        e3 = inbox.turn_event("j3", "c", 1, "completed", 0, None, None, "done", None, "m1", parent_session="s2")
        for ev in (e1, e2, e3):
            inbox.append(ev)

        self.assertFalse(inbox.has_unread("c1", parent_session="s3"))
        self.assertTrue(inbox.has_unread("c1", parent_session="s1"))
        self.assertTrue(inbox.has_unread("c1", parent_session="s2", mission="m1"))
        self.assertFalse(inbox.has_unread("c1", parent_session="s1", mission="m2"))

        # Peek does not move cursor.
        peeked = inbox.read_new("c1", peek=True, parent_session="s2")
        self.assertEqual([e["id"] for e in peeked], ["j2", "j3"])
        self.assertTrue(inbox.has_unread("c1", parent_session="s1"))

        # Filtered read advances cursor across all scanned lines.
        got = inbox.read_new("c1", parent_session="s2", mission="m1")
        self.assertEqual([e["id"] for e in got], ["j3"])
        self.assertFalse(inbox.has_unread("c1"))
        self.assertEqual(inbox.read_new("c1"), [])

        # Half-written line counts for unfiltered has_unread, not for filtered has_unread.
        with (self.registry / "inbox.jsonl").open("ab") as stream:
            stream.write(b'{"id": "half"')
        self.assertTrue(inbox.has_unread("c1"))
        self.assertFalse(inbox.has_unread("c1", parent_session="s1"))

    def test_notify_delegates_to_notifications_when_available(self):
        dispatched = []
        fake_mod = types.ModuleType("claivelib.notifications")
        fake_mod.dispatch = lambda event: dispatched.append(event)
        ev = {"id": "w1", "status": "completed", "code": 0}
        with mock.patch.dict(sys.modules, {"claivelib.notifications": fake_mod}):
            inbox.notify(ev)
        self.assertEqual(dispatched, [ev])


class WatcherRegistryChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="claive-watchers-")
        self.registry = Path(self.temp.name) / "registry"
        self.registry.mkdir(parents=True, exist_ok=True)
        self._old_claive_dir = os.environ.get("CLAIVE_DIR")
        os.environ["CLAIVE_DIR"] = str(self.registry)

    def tearDown(self):
        if self._old_claive_dir is None:
            os.environ.pop("CLAIVE_DIR", None)
        else:
            os.environ["CLAIVE_DIR"] = self._old_claive_dir
        self.temp.cleanup()

    def test_register_unregister_and_watching_context(self):
        with watchers.watching(worker_ids=["aaaaaaaaaaaa"], parent_session="s1", timeout=30) as rec:
            path = watchers.watchers_dir(self.registry) / f"{rec['id']}.json"
            self.assertTrue(path.is_file())
            self.assertEqual(rec["pid"], os.getpid())
            self.assertEqual(rec["worker_ids"], ["aaaaaaaaaaaa"])
            self.assertEqual(rec["parent_session"], "s1")
            live = watchers.active_watchers(root_dir=self.registry)
            self.assertEqual([w["id"] for w in live], [rec["id"]])
        self.assertFalse(path.exists())
        self.assertEqual(watchers.active_watchers(root_dir=self.registry), [])

        with self.assertRaises(RuntimeError):
            with watchers.watching(parent_session="s1") as rec2:
                self.assertTrue((watchers.watchers_dir(self.registry) / f"{rec2['id']}.json").is_file())
                raise RuntimeError("boom")
        self.assertEqual(watchers.active_watchers(root_dir=self.registry), [])

    def test_active_watchers_prunes_dead_mismatched_and_corrupt_entries(self):
        wdir = watchers.watchers_dir(self.registry)
        wdir.mkdir(parents=True, exist_ok=True)
        acceptance.atomic(wdir / "dead-pid.json", {
            "id": "dead-pid", "pid": 99999999, "pid_identity": None, "started_at": 1.0,
        })
        acceptance.atomic(wdir / "reused-pid.json", {
            "id": "reused-pid", "pid": os.getpid(), "pid_identity": "stale-boot:999", "started_at": 2.0,
        })
        (wdir / "corrupt.json").write_text("{not json")

        live = watchers.register_watcher(parent_session="s1", root_dir=self.registry)
        found = watchers.active_watchers(prune_dead=True, root_dir=self.registry)
        self.assertEqual([w["id"] for w in found], [live["id"]])
        self.assertFalse((wdir / "dead-pid.json").exists())
        self.assertFalse((wdir / "reused-pid.json").exists())
        self.assertFalse((wdir / "corrupt.json").exists())

    def test_covers_worker_and_unwatched_workers(self):
        _write_worker(self.registry, "111111111111", status="running", parent_session="s1")
        _write_worker(self.registry, "222222222222", status="idle", queue_depth=1, parent_session="s1")
        _write_worker(self.registry, "333333333333", status="idle", queue_depth=0, parent_session="s1")
        _write_worker(self.registry, "444444444444", status="running", parent_session="s1", parked=True)
        _write_worker(self.registry, "555555555555", status="running", parent_session=None)
        _write_worker(self.registry, "666666666666", status="running", parent_session="s2")

        # Without watchers, running + queued idle workers are unwatched (idle/0 and parked excluded).
        unwatched_s1 = [w["id"] for w in watchers.unwatched_workers(parent_session="s1", root_dir=self.registry)]
        self.assertCountEqual(unwatched_s1, ["111111111111", "222222222222"])

        # Legacy global --inbox watcher covers untagged workers, not session-tagged ones.
        with watchers.watching(inbox_consumer="ops", root_dir=self.registry):
            unwatched_all = [w["id"] for w in watchers.unwatched_workers(root_dir=self.registry)]
            self.assertNotIn("555555555555", unwatched_all)
            self.assertIn("111111111111", unwatched_all)
            self.assertIn("666666666666", unwatched_all)

        # Session watcher covers all workers in s1, not s2.
        with watchers.watching(parent_session="s1", root_dir=self.registry):
            self.assertEqual(watchers.unwatched_workers(parent_session="s1", root_dir=self.registry), [])
            unwatched_s2 = [w["id"] for w in watchers.unwatched_workers(parent_session="s2", root_dir=self.registry)]
            self.assertEqual(unwatched_s2, ["666666666666"])

        # Direct worker_ids watcher covers only that specific worker.
        with watchers.watching(worker_ids=["111111111111"], root_dir=self.registry):
            unwatched_s1 = [w["id"] for w in watchers.unwatched_workers(parent_session="s1", root_dir=self.registry)]
            self.assertEqual(unwatched_s1, ["222222222222"])

    def test_watch_status_reports_watchers_workers_needs_parent_and_unread(self):
        _write_worker(self.registry, "aaaaaaaaaaaa", status="running", label="build",
                      engine="codex", model="gpt-5", parent_session="s1")
        _write_worker(self.registry, "bbbbbbbbbbbb", status="running", label="parked-job",
                      parent_session="s1", parked=True)
        _write_worker(self.registry, "cccccccccccc", status="idle", label="asker",
                      parent_session="s1", needs_parent={"kind": "needs_decision", "question": "A or B?"})
        _write_worker(self.registry, "dddddddddddd", status="completed", label="closed-asker",
                      parent_session="s1", needs_parent={"kind": "blocked", "question": "stale"},
                      phase="session closed")
        _write_worker(self.registry, "eeeeeeeeeeee", status="running", label="other-session",
                      parent_session="s2")

        inbox.append(inbox.turn_event("cccccccccccc", "asker", 1, "completed", 3, None,
                                      {"kind": "needs_decision", "question": "A or B?"},
                                      "needs_decision", None, None, parent_session="s1"))

        with watchers.watching(worker_ids=["bbbbbbbbbbbb"], root_dir=self.registry), \
                watchers.watching(parent_session="s2", root_dir=self.registry):
            status = watchers.watch_status(parent_session="s1", root_dir=self.registry)

        self.assertEqual(status["parent_session"], "s1")
        self.assertEqual(len(status["watchers"]), 1)
        self.assertEqual(status["watchers"][0]["worker_ids"], ["bbbbbbbbbbbb"])
        self.assertEqual({w["id"]: w for w in status["active_workers"]}["aaaaaaaaaaaa"], {
            "id": "aaaaaaaaaaaa",
            "label": "build",
            "status": "running",
            "engine": "codex",
            "model": "gpt-5",
            "parked": False,
            "watched": False,
        })
        self.assertEqual(status["unwatched_worker_ids"], ["aaaaaaaaaaaa"])
        self.assertEqual(status["needs_parent"], [{
            "id": "cccccccccccc",
            "label": "asker",
            "question": "A or B?",
            "kind": "needs_decision",
        }])
        self.assertTrue(status["has_unread"])
        self.assertFalse(watchers.watch_status(root_dir=self.registry)["has_unread"])


class FsWatcherChecks(unittest.TestCase):
    def test_inotify_detects_create_and_modify_and_times_out(self):
        with tempfile.TemporaryDirectory(prefix="claive-fswatch-") as tmp:
            target = Path(tmp) / "inbox.jsonl"

            # Timeout on quiet directory.
            self.assertFalse(watchers.wait_for_fs_change([target], timeout=0.05))

            # Creation of previously non-existent file wakes FsWatcher, and subsequent append wakes it again.
            with watchers.FsWatcher([target]) as fw:
                def writer():
                    time.sleep(0.03)
                    target.write_text("first\n")

                t1 = threading.Thread(target=writer)
                t1.start()
                self.assertTrue(fw.wait(timeout=1.0))
                t1.join(timeout=1.0)

                def appender():
                    time.sleep(0.03)
                    with target.open("a") as stream:
                        stream.write("second\n")

                t2 = threading.Thread(target=appender)
                t2.start()
                self.assertTrue(fw.wait(timeout=1.0))
                t2.join(timeout=1.0)

    def test_fallback_when_inotify_unavailable(self):
        with mock.patch.object(watchers, "_load_libc", return_value=None):
            began = time.monotonic()
            self.assertFalse(watchers.wait_for_fs_change([Path("/tmp")], timeout=0.02))
            self.assertGreaterEqual(time.monotonic() - began, 0.015)


if __name__ == "__main__":
    unittest.main()
