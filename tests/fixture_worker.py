#!/usr/bin/python3
import json
import os
import signal
import subprocess
import sys
import time

mode = os.environ.get("FIXTURE_MODE", "success")
if len(sys.argv) > 1 and os.environ.get("FIXTURE_PROMPT_OUT"):
    with open(sys.argv[1]) as source, open(os.environ["FIXTURE_PROMPT_OUT"], "a") as copy:
        copy.write(source.read() + "\n----\n")


def emit(kind, **payload):
    print(json.dumps({"kind": kind, **payload}), flush=True)


emit("model")
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
if mode != "missing":
    emit("terminal", status="completed", text=(open(os.environ["FIXTURE_TEXT_FILE"]).read()
                                                if os.environ.get("FIXTURE_TEXT_FILE") else "fixture result"))
sys.exit(7 if mode == "exit-failure" else 0)
