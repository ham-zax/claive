#!/usr/bin/python3
"""Replay Pi's wire protocol and record received prompt/session/options."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def value(flag):
    return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else None


if sys.argv[1:] == ["--version"]:
    print("1.0.4")
    sys.exit(0)
mode = os.environ.get("PI_TEST_MODE", "success")
if os.environ.get("PI_TEST_RECORD"):
    # The provider URL this process would use, as Pi reads it from its agent directory.
    agent = Path(os.environ.get("PI_CODING_AGENT_DIR", ""))
    models = json.loads((agent / "models.json").read_text())
    Path(os.environ["PI_TEST_RECORD"]).write_text(models["providers"]["opencode2api"]["baseUrl"])
fixture_dir = Path(__file__).parent / "fixtures/pi"
prompt_argument = sys.argv[sys.argv.index("--") + 1]
prompt = Path(prompt_argument[1:]).read_text()
directory = value("--session-dir")
retained = value("--session")
identifier = value("--session-id")
resumed = False
if retained:
    path = Path(retained)
    header = json.loads(path.read_text().splitlines()[0])
    identifier = header["id"]
    resumed = True
elif directory:
    path = Path(directory) / (identifier + ".jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"type": "session", "id": identifier, "cwd": str(Path.cwd())}) + "\n")

rows = [json.loads(line) for line in (fixture_dir / ("failure.jsonl" if mode == "failure" else "success.jsonl")).read_text().splitlines()]
answer = json.dumps({"prompt": prompt, "resumed": resumed, "effort": value("--thinking"), "tools": value("--tools")})
for row in rows:
    if row["type"] == "session":
        row.update(id=identifier or "ephemeral", cwd=str(Path.cwd()))
    if row["type"] == "message_end" and row.get("message", {}).get("role") == "assistant":
        if mode != "failure":
            row["message"]["content"] = [{"type": "text", "text": answer}]
        if directory:
            with path.open("a") as stream:
                stream.write(json.dumps({"type": "message", "message": row["message"]}) + "\n")
    if row["type"] == "message_start" and row.get("message", {}).get("role") == "assistant" and mode == "slow":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        child = subprocess.Popen([sys.executable, "-c", "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"])
        print(json.dumps({"type": "probe_child", "pid": child.pid}), flush=True)
        time.sleep(60)
    if mode == "missing" and row["type"] == "agent_settled":
        continue
    print(json.dumps(row), flush=True)
if mode == "malformed":
    print("NOT_JSON", flush=True)
sys.exit(7 if mode == "exit-failure" else 0)
