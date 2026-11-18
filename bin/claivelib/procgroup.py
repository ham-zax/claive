"""Linux process-tree, subreaper, and systemd user-scope containment for claive."""
import ctypes
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time

_SYSTEMD_SCOPE_CACHE = None
_SUBREAPER_ENABLED = False
_PR_SET_CHILD_SUBREAPER = 36


def enable_subreaper():
    """Reparent orphaned descendants to this process instead of PID 1 (Linux)."""
    global _SUBREAPER_ENABLED
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) == 0:
            _SUBREAPER_ENABLED = True
            return True
    except (OSError, AttributeError):
        pass
    return False


def subreaper_active():
    """Return True when enable_subreaper() succeeded in this process."""
    return _SUBREAPER_ENABLED


def systemd_scope_available():
    """Return True when `systemd-run --user --scope` works in this environment."""
    global _SYSTEMD_SCOPE_CACHE
    if os.environ.get("CLAIVE_NO_SYSTEMD_SCOPE") == "1":
        return False
    if _SYSTEMD_SCOPE_CACHE is not None:
        return _SYSTEMD_SCOPE_CACHE
    binary = shutil.which("systemd-run")
    if not binary:
        _SYSTEMD_SCOPE_CACHE = False
        return False
    try:
        result = subprocess.run(
            [binary, "--user", "--scope", "--quiet", "--collect", "--", "/bin/true"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            timeout=1.5,
        )
        _SYSTEMD_SCOPE_CACHE = result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        _SYSTEMD_SCOPE_CACHE = False
    return _SYSTEMD_SCOPE_CACHE


def wrap_scoped_command(command, unit_name):
    """Wrap command in a transient systemd user scope when supported."""
    if not systemd_scope_available():
        return list(command), None
    unit = "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in str(unit_name))
    scoped = [
        "systemd-run", "--user", "--scope", "--quiet", "--collect",
        f"--unit={unit}", "--", *command,
    ]
    return scoped, unit


def cgroup_scope_dir(pid, unit_name):
    """Return the cgroup v2 directory for unit_name if pid runs inside it, else None."""
    if not pid or not unit_name:
        return None
    suffix = f"/{unit_name}.scope"
    try:
        lines = Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in lines:
        if not line.startswith("0::"):
            continue
        rel = line[3:].strip()
        if rel.endswith(suffix):
            candidate = Path("/sys/fs/cgroup") / rel.lstrip("/")
            if candidate.is_dir():
                return candidate
    return None


def _boot_id():
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return "boot"


def proc_table():
    """Return {pid: (state, ppid, pgid, sid, identity)} from /proc."""
    boot = _boot_id()
    table = {}
    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        return table
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            if len(fields) <= 19:
                continue
            state = fields[0]
            ppid = int(fields[1])
            pgid = int(fields[2])
            sid = int(fields[3])
            ident = None if state == "Z" else f"{boot}:{fields[19]}"
            table[pid] = (state, ppid, pgid, sid, ident)
        except (OSError, IndexError, ValueError):
            continue
    return table


def _cgroup_pids(cgroup_dir):
    if not cgroup_dir:
        return set()
    try:
        text = (Path(cgroup_dir) / "cgroup.procs").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()
    return {int(line.strip()) for line in text.splitlines() if line.strip().isdigit()}


def tree_pids(leader_pid, *, subreaper_pid=None, cgroup_dir=None, exclude_pids=frozenset(), table=None):
    """Collect live descendant/session/cgroup PIDs and leader-owned PGIDs."""
    if table is None:
        table = proc_table()
    exclude = set(exclude_pids)
    if subreaper_pid:
        exclude.add(subreaper_pid)
    exclude.add(os.getpid())

    children_by_ppid = {}
    for pid, (state, ppid, _pgid, _sid, _ident) in table.items():
        if state == "Z":
            continue
        children_by_ppid.setdefault(ppid, []).append(pid)

    seeds = set()
    if leader_pid and leader_pid in table and table[leader_pid][0] != "Z":
        seeds.add(leader_pid)
    if subreaper_pid:
        for pid in children_by_ppid.get(subreaper_pid, ()):
            if pid not in exclude:
                seeds.add(pid)
    if cgroup_dir:
        for pid in _cgroup_pids(cgroup_dir):
            if pid in table and table[pid][0] != "Z" and pid not in exclude:
                seeds.add(pid)
    if leader_pid:
        for pid, (state, _ppid, pgid, sid, _ident) in table.items():
            if state != "Z" and pid not in exclude and (sid == leader_pid or pgid == leader_pid):
                seeds.add(pid)

    pids = set()
    stack = list(seeds)
    while stack:
        current = stack.pop()
        if current in pids or current in exclude:
            continue
        info = table.get(current)
        if info is None or info[0] == "Z":
            continue
        pids.add(current)
        for child in children_by_ppid.get(current, ()):
            if child not in pids and child not in exclude:
                stack.append(child)

    self_pgid = os.getpgrp()
    pgids = set()
    if leader_pid and leader_pid != self_pgid and leader_pid not in exclude:
        pgids.add(leader_pid)
    for pid in pids:
        pgid = table[pid][2]
        if pgid > 1 and pgid != self_pgid and (pgid in pids or pgid == leader_pid):
            pgids.add(pgid)
    return pids, pgids


def reap_reparented(exclude_pids=frozenset()):
    """Reap zombie children reparented to this process without touching exclude_pids."""
    my_pid = os.getpid()
    exclude = set(exclude_pids)
    table = proc_table()
    reaped = []
    for pid, (state, ppid, _pgid, _sid, _ident) in table.items():
        if ppid == my_pid and state == "Z" and pid not in exclude:
            try:
                done_pid, _ = os.waitpid(pid, os.WNOHANG)
                if done_pid:
                    reaped.append(done_pid)
            except ChildProcessError:
                pass
    return reaped


def _send_signal(pids, pgids, sig):
    for pgid in sorted(pgids):
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            pass
    for pid in sorted(pids):
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass


def signal_tree(leader_pid, sig, *, subreaper_pid=None, cgroup_dir=None, preserve_pids=frozenset()):
    """Send one signal to the worker tree while protecting preserve_pids."""
    table = proc_table()
    pids, pgids = tree_pids(leader_pid, subreaper_pid=subreaper_pid, cgroup_dir=cgroup_dir,
                            exclude_pids=preserve_pids, table=table)
    safe_pgids = {pgid for pgid in pgids if pgid not in preserve_pids}
    if leader_pid in preserve_pids:
        pids.discard(leader_pid)
        safe_pgids.discard(leader_pid)
    _send_signal(pids, safe_pgids, sig)
    return pids


def terminate_tree(leader_pid, *, grace=3.0, subreaper_pid=None, cgroup_dir=None,
                   preserve_pids=frozenset(), reap_exclude=None):
    """Terminate a process tree and any detached grandchildren; escalate to cgroup.kill + SIGKILL."""
    preserve = set(preserve_pids)
    table = proc_table()
    pids, pgids = tree_pids(leader_pid, subreaper_pid=subreaper_pid, cgroup_dir=cgroup_dir,
                            exclude_pids=preserve, table=table)
    if leader_pid in preserve:
        pids.discard(leader_pid)
        pgids.discard(leader_pid)
    if pids or pgids:
        if grace > 0:
            _send_signal(pids, pgids, signal.SIGTERM)
            deadline = time.monotonic() + grace
            while time.monotonic() < deadline:
                table = proc_table()
                pids, pgids = tree_pids(leader_pid, subreaper_pid=subreaper_pid, cgroup_dir=cgroup_dir,
                                        exclude_pids=preserve, table=table)
                if leader_pid in preserve:
                    pids.discard(leader_pid)
                    pgids.discard(leader_pid)
                if not pids:
                    break
                time.sleep(0.025)
        for _ in range(2):
            if cgroup_dir and not preserve:
                kill_file = Path(cgroup_dir) / "cgroup.kill"
                if kill_file.exists():
                    try:
                        kill_file.write_text("1", encoding="utf-8")
                    except OSError:
                        pass
            table = proc_table()
            pids, pgids = tree_pids(leader_pid, subreaper_pid=subreaper_pid, cgroup_dir=cgroup_dir,
                                    exclude_pids=preserve, table=table)
            if leader_pid in preserve:
                pids.discard(leader_pid)
                pgids.discard(leader_pid)
            if not pids and not pgids:
                break
            _send_signal(pids, pgids, signal.SIGKILL)
            time.sleep(0.025)
    exclude_reap = preserve | ({leader_pid} if leader_pid else set())
    if reap_exclude is not None:
        exclude_reap |= set(reap_exclude)
    reap_reparented(exclude_reap)


def snapshot_infra(leader_pid, *, subreaper_pid=None, cgroup_dir=None):
    """Record {(pid, identity)} for baseline worker + infra processes (never treated as tool children)."""
    if not leader_pid:
        return set()
    table = proc_table()
    pids, _ = tree_pids(leader_pid, subreaper_pid=subreaper_pid, cgroup_dir=cgroup_dir, table=table)
    return {(pid, table[pid][4]) for pid in pids if pid in table and table[pid][4] is not None}


def interrupt_tool_children(leader_pid, infra_snapshot, *, grace=0.5, subreaper_pid=None, cgroup_dir=None):
    """Kill only transient tool children (and their descendants), preserving leader and infra processes."""
    if not leader_pid:
        return []
    table = proc_table()
    pids, _ = tree_pids(leader_pid, subreaper_pid=subreaper_pid, cgroup_dir=cgroup_dir, table=table)
    protected = {leader_pid}
    if subreaper_pid:
        protected.add(subreaper_pid)
    live_infra = set()
    for pid in pids:
        info = table.get(pid)
        if info and (pid, info[4]) in infra_snapshot:
            live_infra.add(pid)
    # Protect every live infra process and its ancestors up to the leader/subreaper.
    for pid in list(live_infra):
        cursor = pid
        while cursor and cursor not in protected and cursor in table:
            protected.add(cursor)
            cursor = table[cursor][1]

    targets = sorted(pid for pid in pids if pid not in protected)
    if not targets:
        return []

    target_set = set(targets)
    self_pgid = os.getpgrp()
    target_pgids = {
        table[pid][2] for pid in targets
        if table[pid][2] in target_set and table[pid][2] != self_pgid
    }
    _send_signal(target_set, target_pgids, signal.SIGTERM)
    deadline = time.monotonic() + max(0.0, grace)
    while time.monotonic() < deadline:
        table = proc_table()
        remaining = {
            pid for pid in target_set
            if pid in table and table[pid][0] != "Z"
        }
        if not remaining:
            break
        time.sleep(0.025)
    table = proc_table()
    remaining = {pid for pid in target_set if pid in table and table[pid][0] != "Z"}
    if remaining:
        rem_pgids = {
            table[pid][2] for pid in remaining
            if table[pid][2] in remaining and table[pid][2] != self_pgid
        }
        _send_signal(remaining, rem_pgids, signal.SIGKILL)
        time.sleep(0.025)
    reap_reparented({leader_pid})
    return targets
