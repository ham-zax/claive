"""Live watcher registry, unwatched-worker detection, and Linux inotify wait."""
from contextlib import contextmanager
import ctypes
import json
import os
from pathlib import Path
import re
import select
import sys
import time
import uuid

from claivelib import acceptance, inbox

_JOB_ID_RE = re.compile(r"[0-9a-f]{12}")
_RUNNING_STATUSES = {"starting", "running", "cancelling"}

_IN_MODIFY = 0x2
_IN_ATTRIB = 0x4
_IN_CLOSE_WRITE = 0x8
_IN_MOVED_TO = 0x80
_IN_CREATE = 0x100
_IN_DELETE = 0x200
_IN_WATCH_MASK = _IN_MODIFY | _IN_ATTRIB | _IN_CLOSE_WRITE | _IN_MOVED_TO | _IN_CREATE | _IN_DELETE
_IN_NONBLOCK = getattr(os, "O_NONBLOCK", 0o0004000)
_IN_CLOEXEC = getattr(os, "O_CLOEXEC", 0o2000000)

_LIBC = None
_LIBC_LOADED = False


def _cli():
    from claivelib import cli
    return cli


def _resolved_root(root_dir=None):
    return Path(root_dir) if root_dir is not None else _cli().root()


def watchers_dir(root_dir=None) -> Path:
    return _resolved_root(root_dir) / "watchers"


def register_watcher(*, worker_ids=(), inbox_consumer=None, parent_session=None,
                     timeout=None, root_dir=None) -> dict:
    directory = watchers_dir(root_dir)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    pid = os.getpid()
    record = {
        "id": f"{pid}-{uuid.uuid4().hex[:8]}",
        "pid": pid,
        "pid_identity": _cli().identity(pid),
        "worker_ids": list(worker_ids),
        "inbox_consumer": inbox_consumer,
        "parent_session": parent_session,
        "started_at": time.time(),
        "timeout": timeout,
    }
    acceptance.atomic(directory / f"{record['id']}.json", record)
    return record


def unregister_watcher(watcher_id: str, root_dir=None) -> None:
    if not watcher_id:
        return
    (watchers_dir(root_dir) / f"{watcher_id}.json").unlink(missing_ok=True)


@contextmanager
def watching(**kwargs):
    record = register_watcher(**kwargs)
    try:
        yield record
    finally:
        unregister_watcher(record["id"], root_dir=kwargs.get("root_dir"))


def _watcher_alive(record) -> bool:
    if not isinstance(record, dict):
        return False
    pid = record.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    recorded_id = record.get("pid_identity")
    if recorded_id is not None:
        return _cli().identity(pid) == recorded_id
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def active_watchers(prune_dead=True, root_dir=None) -> list[dict]:
    directory = watchers_dir(root_dir)
    if not directory.is_dir():
        return []
    live = []
    for path in sorted(directory.glob("*.json")):
        if path.name.startswith("."):
            continue
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError):
            if prune_dead:
                path.unlink(missing_ok=True)
            continue
        if not _watcher_alive(record):
            if prune_dead:
                path.unlink(missing_ok=True)
            continue
        live.append(record)
    live.sort(key=lambda item: float(item.get("started_at") or 0))
    return live


def covers_worker(watcher: dict, worker_state: dict) -> bool:
    worker_id = worker_state.get("id")
    if worker_id and worker_id in (watcher.get("worker_ids") or ()):
        return True
    watcher_session = watcher.get("parent_session")
    worker_session = worker_state.get("parent_session")
    if watcher_session and watcher_session == worker_session:
        return True
    if watcher.get("inbox_consumer") and not watcher_session and not worker_session:
        return True
    return False


def is_worker_watched(worker_state: dict, watchers=None, root_dir=None) -> bool:
    if watchers is None:
        watchers = active_watchers(root_dir=root_dir)
    return any(covers_worker(watcher, worker_state) for watcher in watchers)


def _is_active_turn(state: dict) -> bool:
    status = state.get("status")
    return status in _RUNNING_STATUSES or (status == "idle" and state.get("queue_depth", 0) > 0)


def _load_jobs(root_dir=None) -> list[dict]:
    base = _resolved_root(root_dir)
    if not base.is_dir():
        return []
    cli_mod = _cli()
    records = []
    for path in base.iterdir():
        if path.is_dir() and _JOB_ID_RE.fullmatch(path.name) and (path / "state.json").is_file():
            try:
                records.append(cli_mod.load(path))
            except (OSError, ValueError, KeyError):
                continue
    records.sort(key=lambda item: float(item.get("started_at") or 0), reverse=True)
    return records


def unwatched_workers(parent_session=None, root_dir=None) -> list[dict]:
    watchers = active_watchers(root_dir=root_dir)
    result = []
    for state in _load_jobs(root_dir=root_dir):
        if not _is_active_turn(state):
            continue
        if parent_session is not None and state.get("parent_session") != parent_session:
            continue
        if state.get("parked") is True:
            continue
        if not is_worker_watched(state, watchers=watchers, root_dir=root_dir):
            result.append(state)
    return result


def watch_status(parent_session=None, consumer=None, root_dir=None) -> dict:
    root_path = _resolved_root(root_dir)
    live_watchers = active_watchers(root_dir=root_path)
    all_jobs = _load_jobs(root_dir=root_path)

    session_jobs = [
        state for state in all_jobs
        if parent_session is None or state.get("parent_session") == parent_session
    ]
    active_states = [state for state in session_jobs if _is_active_turn(state)]

    if parent_session is not None:
        filtered_watchers = [
            wt for wt in live_watchers
            if wt.get("parent_session") == parent_session
            or any(covers_worker(wt, state) for state in active_states)
        ]
    else:
        filtered_watchers = live_watchers

    active_workers = []
    for state in active_states:
        launch = state.get("launch") if isinstance(state.get("launch"), dict) else {}
        active_workers.append({
            "id": state["id"],
            "label": state.get("label", ""),
            "status": state.get("status"),
            "engine": state.get("engine"),
            "model": state.get("model") if state.get("model") is not None else launch.get("model"),
            "parked": bool(state.get("parked")),
            "watched": is_worker_watched(state, watchers=live_watchers, root_dir=root_path),
        })

    unwatched_worker_ids = [
        w["id"] for w in active_workers if not w["watched"] and not w["parked"]
    ]

    needs_parent = []
    for state in session_jobs:
        np = state.get("needs_parent")
        if np and state.get("phase") != "session closed":
            np_dict = np if isinstance(np, dict) else {}
            needs_parent.append({
                "id": state["id"],
                "label": state.get("label", ""),
                "question": np_dict.get("question", ""),
                "kind": np_dict.get("kind"),
            })

    effective_consumer = consumer
    if effective_consumer is None and parent_session is not None:
        effective_consumer = inbox.session_consumer(parent_session)

    if effective_consumer is not None:
        has_unread = inbox._has_unread_at(root_path, consumer=effective_consumer, parent_session=parent_session)
    else:
        has_unread = False

    return {
        "parent_session": parent_session,
        "watchers": filtered_watchers,
        "active_workers": active_workers,
        "unwatched_worker_ids": unwatched_worker_ids,
        "needs_parent": needs_parent,
        "has_unread": has_unread,
    }


def _load_libc():
    global _LIBC, _LIBC_LOADED
    if _LIBC_LOADED:
        return _LIBC
    _LIBC_LOADED = True
    if not sys.platform.startswith("linux"):
        return None
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.inotify_init1.argtypes = [ctypes.c_int]
        libc.inotify_init1.restype = ctypes.c_int
        libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        libc.inotify_add_watch.restype = ctypes.c_int
        _LIBC = libc
    except OSError:
        _LIBC = None
    return _LIBC


def _fallback_sleep(timeout):
    time.sleep(min(max(float(timeout), 0.01), 0.1))
    return False


class FsWatcher:
    """Reusable inotify watcher so wait loops register watches before checking state."""

    def __init__(self, paths):
        if isinstance(paths, (str, os.PathLike)):
            self.paths = [Path(paths)]
        else:
            self.paths = [Path(p) for p in paths]
        self._fd = None
        self._poll = None
        self._libc = None
        self._watched = set()

    def _sync_watches(self):
        if self._fd is None or self._libc is None:
            return
        for path in self.paths:
            targets = [path]
            if path.parent != path:
                targets.append(path.parent)
            for target in targets:
                try:
                    st = target.stat()
                except OSError:
                    continue
                key = (str(target), st.st_dev, st.st_ino)
                if key in self._watched:
                    continue
                wd = self._libc.inotify_add_watch(self._fd, os.fsencode(str(target)), _IN_WATCH_MASK)
                if wd >= 0:
                    self._watched.add(key)

    def __enter__(self):
        libc = _load_libc()
        if libc is None:
            return self
        fd = libc.inotify_init1(_IN_NONBLOCK | _IN_CLOEXEC)
        if fd < 0:
            return self
        self._libc = libc
        self._fd = fd
        self._poll = select.poll()
        self._poll.register(fd, select.POLLIN)
        self._sync_watches()
        return self

    def wait(self, timeout=0.25) -> bool:
        if self._fd is None or self._poll is None:
            return _fallback_sleep(timeout)
        self._sync_watches()
        timeout_ms = max(1, int(float(timeout) * 1000)) if float(timeout) > 0 else 0
        try:
            events = self._poll.poll(timeout_ms)
        except OSError:
            return _fallback_sleep(timeout)
        if not events:
            return False
        while True:
            try:
                chunk = os.read(self._fd, 4096)
                if not chunk:
                    break
            except BlockingIOError:
                break
            except OSError:
                break
        return True

    def __exit__(self, exc_type, exc, tb):
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
            self._poll = None
            self._libc = None
            self._watched.clear()


def wait_for_fs_change(paths, timeout=0.25) -> bool:
    with FsWatcher(paths) as watcher:
        return watcher.wait(timeout=timeout)
