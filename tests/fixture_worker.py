#!/usr/bin/python3
import json
import os
import signal
import subprocess
import sys
import time

import re

mode = os.environ.get("FIXTURE_MODE", "success")
prompt_text = open(sys.argv[1]).read() if len(sys.argv) > 1 else ""
# Per-prompt overrides let one batch mix outcomes: a line "FIXTURE_MODE=<mode>" in the prompt.
directive = re.search(r"FIXTURE_MODE=([a-z-]+)", prompt_text)
if directive:
    mode = directive.group(1)
if os.environ.get("FIXTURE_PROMPT_OUT"):
    with open(os.environ["FIXTURE_PROMPT_OUT"], "a") as copy:
        copy.write(prompt_text + "\n----\n")
if os.environ.get("FIXTURE_ENV_OUT"):
    with open(os.environ["FIXTURE_ENV_OUT"], "a") as copy:
        copy.write(json.dumps({"CLAIVE_WORKER_ID": os.environ.get("CLAIVE_WORKER_ID")}) + "\n")
ASK = ("Need a choice.\n\n```claive-report\n{\"status\": \"needs_decision\", \"summary\": \"Two designs.\", "
       "\"question\": \"Design A or B?\"}\n```\n")


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
if mode == "ask":
    emit("terminal", status="completed", text=ASK)
elif mode != "missing":
    emit("terminal", status="completed", text=(open(os.environ["FIXTURE_TEXT_FILE"]).read()
                                                if os.environ.get("FIXTURE_TEXT_FILE") else "fixture result"))
sys.exit(7 if mode == "exit-failure" else 0)
