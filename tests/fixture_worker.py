#!/usr/bin/python3
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import re

mode = os.environ.get("FIXTURE_MODE", "success")
prompt_text = open(sys.argv[1]).read() if len(sys.argv) > 1 else ""


def flag(name):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else None


session_id = flag("--session-id")
session_dir = flag("--session-dir")
fork_session_id = flag("--fork-session-id")
fork_session_dir = flag("--fork-session-dir")
if session_id and session_dir:
    target_dir = Path(session_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    session_path = target_dir / "session.json"
    if fork_session_id:
        source_path = Path(fork_session_dir) / "session.json"
        session = json.loads(source_path.read_text())
    elif session_path.exists():
        session = json.loads(session_path.read_text())
    else:
        session = {"id": session_id, "messages": []}
    session["id"] = session_id
    session.setdefault("messages", []).append(prompt_text.strip())
    session_path.write_text(json.dumps(session))

# Per-prompt overrides let one batch mix outcomes: a line "FIXTURE_MODE=<mode>" in the prompt.
directive = re.search(r"FIXTURE_MODE=([a-z-]+)", prompt_text)
if directive:
    mode = directive.group(1)
# Per-model overrides let fallback tests fail one model and pass the next: FIXTURE_MODEL_MODES='{"m1": "slow"}'.
model = sys.argv[sys.argv.index("--model") + 1] if "--model" in sys.argv else None
if model and os.environ.get("FIXTURE_MODEL_MODES"):
    mode = json.loads(os.environ["FIXTURE_MODEL_MODES"]).get(model, mode)
if os.environ.get("FIXTURE_PROMPT_OUT"):
    with open(os.environ["FIXTURE_PROMPT_OUT"], "a") as copy:
        copy.write(prompt_text + "\n----\n")
if os.environ.get("FIXTURE_ENV_OUT"):
    with open(os.environ["FIXTURE_ENV_OUT"], "a") as copy:
        copy.write(json.dumps({"CLAIVE_WORKER_ID": os.environ.get("CLAIVE_WORKER_ID"),
                               "CLAIVE_BIN": os.environ.get("CLAIVE_BIN")}) + "\n")
ASK = ("Need a choice.\n\n```claive-report\n{\"status\": \"needs_decision\", \"summary\": \"Two designs.\", "
       "\"question\": \"Design A or B?\"}\n```\n")


def emit(kind, **payload):
    print(json.dumps({"kind": kind, **payload}), flush=True)


emit("model")
if mode == "reply":
    question_id = re.search(r'"\$CLAIVE_BIN" reply ([0-9a-f]{32})', prompt_text)[1]
    launcher = os.environ["CLAIVE_BIN"]
    replies = [subprocess.run([launcher, "reply", question_id, "--message", "fixture reply"],
                              text=True, capture_output=True) for _ in range(2)]
    if os.environ.get("FIXTURE_REPLY_OUT"):
        with open(os.environ["FIXTURE_REPLY_OUT"], "w") as output:
            json.dump([dict(code=result.returncode, stdout=result.stdout, stderr=result.stderr)
                       for result in replies], output)
    time.sleep(0.4)
if mode == "brief":
    time.sleep(0.3)
if mode == "slow":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    child = subprocess.Popen([
        sys.executable, "-c",
        "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    ])
    emit("tool", tool="child")
    emit("activity", pid=child.pid)
    time.sleep(60)
if mode == "malformed":
    print("NOT_JSON", flush=True)
if mode == "warning":
    emit("warning", reason="fixture task warning")
if mode == "terminal-failure":
    emit("terminal", status="failed", reason="fixture terminal failure", text="")
    sys.exit(0)
if mode in {"context-failure", "context-file-error"}:
    reason = ("Autocompact is thrashing: the context refilled to the limit within 3 turns of the "
              "previous compact, 3 times in a row.") if mode == "context-failure" else "cannot read context file"
    emit("terminal", status="failed", reason=reason, text="")
    sys.exit(0)
if mode == "ask":
    emit("terminal", status="completed", text=ASK)
elif mode != "missing":
    emit("terminal", status="completed", text=(open(os.environ["FIXTURE_TEXT_FILE"]).read()
                                                if os.environ.get("FIXTURE_TEXT_FILE") else "fixture result"))
sys.exit(7 if mode == "exit-failure" else 0)
