"""Isolate the suite from the host: imported first by every test module.

Host settings (a claive config.json, CLAIVE_ENGINE, the legacy CLAIVE_PI_ONLY draft, a parent
worker's CLAIVE_WORKER_ID, installed Muse/Pi binaries and ~/.pi/agent) must not change results or
be touched. Tests build their child environments from os.environ, so cleaning it here covers both
in-process and subprocess checks. Set CLAIVE_LIVE_TESTS=1 to also run the checks that drive the
real installed Muse binary.
"""
import atexit
import os
from pathlib import Path
import shutil
import tempfile

LIVE = os.environ.get("CLAIVE_LIVE_TESTS") == "1"
HOST_HOME = Path.home()

for _name in list(os.environ):
    if _name.startswith(("CLAIVE_", "MUSE_", "PI_", "CLAUDE_WORKER_", "CODEX_WORKER_")) or _name in {"TMUX", "XDG_CONFIG_HOME", "XDG_STATE_HOME"}:
        del os.environ[_name]

SANDBOX = Path(tempfile.mkdtemp(prefix="claive-tests-"))
atexit.register(shutil.rmtree, SANDBOX, True)
os.environ.update(
    CLAIVE_CONFIG=str(SANDBOX / "no-config.json"),
    CLAIVE_DIR=str(SANDBOX / "state"),
    MUSE_WORKER_BINARY=str(SANDBOX / "no-muse"),
    PI_WORKER_BINARY=str(SANDBOX / "no-pi"),
    CLAUDE_WORKER_BINARY=str(SANDBOX / "no-claude"),
    CODEX_WORKER_BINARY=str(SANDBOX / "no-codex"),
    PI_CODING_AGENT_DIR=str(SANDBOX / "pi-agent"),
    # Fixtures ignore SIGTERM to prove SIGKILL escalation; the 3 s production grace only adds waiting.
    CLAIVE_KILL_GRACE="0.3",
)
