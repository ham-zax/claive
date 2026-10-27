"""claive-memcap: run a command under a resident-memory cap without systemd.

The command runs in a new session. Every POLL seconds the RSS of every process
in that session is summed; above the limit (or after the timeout) the whole
session is killed. Unlike `ulimit -v` this does not break runtimes that reserve
large virtual address space (node, the JVM). It is a sampling cap: a burst
faster than one poll can briefly exceed it, and a process that calls setsid()
leaves the session and is not counted.
"""
import argparse
from contextlib import contextmanager, nullcontext
import fcntl
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time

POLL = 0.2
EXCEEDED_EXIT = 137
TIMEOUT_EXIT = 124
UNITS = {"": 1, "B": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}
PAGE = os.sysconf("SC_PAGE_SIZE")


class LockTimeout(Exception):
    pass


class LockCancelled(Exception):
    pass


@contextmanager
def hold_lock(name, timeout=None, cancelled=None):
    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,31}", name):
        raise ValueError("invalid lock name")
    if timeout is not None and timeout <= 0:
        raise ValueError("--lock-timeout must be positive")
    state = Path(os.environ.get("CLAIVE_DIR") or
                 str(Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "claive"))
    if not state.is_absolute():
        raise ValueError("CLAIVE_DIR must be absolute")
    locks = state / "locks"
    locks.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (locks / f"{name}.lock").open("a+") as stream:
        start = time.monotonic()
        warned = False
        while True:
            if cancelled and cancelled():
                raise LockCancelled()
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not warned:
                    stream.seek(0)
                    holder = stream.read().strip() or "unknown"
                    print(f"claive-memcap: waiting for lock {name} (held by pid {holder})", file=sys.stderr, flush=True)
                    warned = True
                if timeout is not None and time.monotonic() - start >= timeout:
                    raise LockTimeout(f"timed out waiting for lock {name}")
                time.sleep(min(0.1, max(0, timeout - (time.monotonic() - start))) if timeout else 0.1)
        stream.seek(0)
        stream.truncate()
        stream.write(str(os.getpid()))
        stream.flush()
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def parse_size(text):
    """Parse 512M, 2G, 1.5G or a byte count into bytes; ValueError when not positive."""
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([KMGTB]?)(?:i?B)?\s*", str(text), re.IGNORECASE)
    if not match:
        raise ValueError(f"invalid memory size: {text!r} (use e.g. 512M or 2G)")
    size = int(float(match.group(1)) * UNITS[match.group(2).upper()])
    if size <= 0:
        raise ValueError(f"memory size must be positive: {text!r}")
    return size


def format_size(size):
    for unit in ("G", "M", "K"):
        if size >= UNITS[unit]:
            return f"{size / UNITS[unit]:.1f}{unit}".replace(".0" + unit, unit)
    return f"{size}B"


def session_processes(session):
    """Return {pid: rss_bytes} for every live process in a session."""
    found = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
        except (OSError, IndexError):
            continue
        # After the command name: state ppid pgrp session ... rss is the 22nd.
        if len(fields) > 21 and fields[0] != "Z" and int(fields[3]) == session:
            found[int(entry.name)] = int(fields[21]) * PAGE
    return found


def kill_session(session):
    for _ in range(3):
        pids = session_processes(session)
        if not pids:
            return
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        time.sleep(0.05)


def _die_with_parent():
    """In the child: get SIGKILL when the parent dies, even if the parent was SIGKILLed (Linux)."""
    try:
        import ctypes
        ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGKILL)  # PR_SET_PDEATHSIG
    except (OSError, AttributeError):
        pass


class Stopped(Exception):
    """SIGTERM or SIGHUP reached claive-memcap; the capped session is killed on the way out."""


def _raise_stopped(signum, frame):
    raise Stopped(signal.Signals(signum).name)


def run_capped(command, limit=None, timeout=None, cwd=None, env=None, capture=True, die_with_parent=False):
    """Run argv under the cap. Returns a dict: code, stdout, stderr, exceeded, timed_out, peak.

    code is None when the command was killed for memory or time. die_with_parent ties the
    command's lifetime to the calling thread; use it only from a thread that outlives the run.
    """
    outputs = [tempfile.TemporaryFile(mode="w+") for _ in range(2)] if capture else [None, None]
    process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL if capture else None,
                               stdout=outputs[0], stderr=outputs[1], text=True, start_new_session=True,
                               preexec_fn=_die_with_parent if die_with_parent else None)
    started = time.monotonic()
    exceeded = timed_out = False
    peak = 0
    try:
        while process.poll() is None:
            if limit:
                total = sum(session_processes(process.pid).values())
                peak = max(peak, total)
                if total > limit:
                    exceeded = True
                    break
            if timeout and time.monotonic() - started > timeout:
                timed_out = True
                break
            time.sleep(POLL)
    except BaseException:
        kill_session(process.pid)
        process.kill()
        process.wait()
        raise
    if exceeded or timed_out:
        kill_session(process.pid)
        process.kill()
    code = process.wait()
    # The leader may exit while children keep running in the session.
    kill_session(process.pid)
    texts = []
    for handle in outputs:
        if handle is None:
            texts.append("")
            continue
        handle.seek(0)
        texts.append(handle.read())
        handle.close()
    return {"code": None if exceeded or timed_out else code, "stdout": texts[0], "stderr": texts[1],
            "exceeded": exceeded, "timed_out": timed_out, "peak": peak}


def entrypoint(argv=None):
    parser = argparse.ArgumentParser(
        prog="claive-memcap", description=__doc__.splitlines()[0],
        epilog=f"Exit {EXCEEDED_EXIT} when the cap is exceeded, {TIMEOUT_EXIT} on timeout, "
               "otherwise the command's exit code. Example: claive-memcap 2G -- npm test")
    parser.add_argument("limit", help="resident memory cap for the whole command, e.g. 512M or 2G")
    parser.add_argument("--timeout", type=float, help="kill the command after this many seconds")
    parser.add_argument("--lock", help="serialize commands using a named lock")
    parser.add_argument("--lock-timeout", type=float, help="limit the wait for the lock in seconds")
    parser.usage = "claive-memcap LIMIT [--timeout S] -- COMMAND [ARGS...]"
    argv = sys.argv[1:] if argv is None else list(argv)
    split = argv.index("--") if "--" in argv else len(argv)
    args, command = parser.parse_args(argv[:split]), argv[split + 1:]
    if not command:
        parser.error("missing command (put it after --)")
    try:
        limit = parse_size(args.limit)
        if args.timeout is not None and args.timeout <= 0:
            raise ValueError("--timeout must be positive")
        if args.lock_timeout is not None and not args.lock:
            raise ValueError("--lock-timeout requires --lock")
        # Default SIGTERM/SIGHUP handling would exit without killing the session.
        for signum in (signal.SIGTERM, signal.SIGHUP):
            signal.signal(signum, _raise_stopped)
        with hold_lock(args.lock, args.lock_timeout) if args.lock else nullcontext():
            result = run_capped(command, limit, args.timeout, capture=False, die_with_parent=True)
    except LockTimeout as error:
        print(f"claive-memcap: {error}", file=sys.stderr)
        return 75
    except (ValueError, OSError) as error:
        print(f"claive-memcap: {error}", file=sys.stderr)
        return 2
    except Stopped as stopped:
        print(f"claive-memcap: stopped by {stopped}; killed the command", file=sys.stderr)
        return 128 + signal.Signals[str(stopped)].value
    if result["exceeded"]:
        print(f"claive-memcap: killed: memory exceeded {format_size(limit)} "
              f"(peak sampled {format_size(result['peak'])})", file=sys.stderr)
        return EXCEEDED_EXIT
    if result["timed_out"]:
        print(f"claive-memcap: killed: timed out after {args.timeout:g}s", file=sys.stderr)
        return TIMEOUT_EXIT
    code = result["code"]
    return 128 - code if code < 0 else code
