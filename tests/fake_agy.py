#!/usr/bin/python3
"""Replay captured AGY output without calling a provider.

FAKE_AGY_SCENARIO selects a fixture. This is a protocol replay, not a model,
conversation store, or implementation of AGY's permission behavior.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import time
import uuid


FIXTURES = Path(__file__).parent / "fixtures" / "agy"

RUNTIME_SCENARIOS = {"lifecycle", "fallback", "mismatch", "declining", "missing", "malformed",
                     "pre-banner", "post-banner", "missing-usage", "unknown-status", "invalid-usage",
                     "no-init-corrupt"}


def lifecycle(args, scenario):
    """Synthetic lifecycle behavior for supervisor tests, separate from captured replay."""
    store = Path(os.environ["FAKE_AGY_STORE"])
    sessions = json.loads(store.read_text()) if store.exists() else {}
    identifier = args.conversation or str(uuid.uuid4())
    turn = sessions.get(identifier, 0) + 1
    sessions[identifier] = turn
    store.write_text(json.dumps(sessions))
    if scenario == "mismatch" and args.conversation:
        identifier = str(uuid.uuid4())
    if scenario == "pre-banner":
        print("synthetic banner")
    def emit(event, body):
        row = dict(event=event, **{event: body})
        if event == "init":
            row["conversation_id"] = identifier
        print(json.dumps(row), flush=True)
    if scenario != "no-init-corrupt":
        emit("init", dict(model=args.model, cwd=str(Path.cwd()), tools=[]))
    if scenario in {"post-banner", "malformed"}:
        print("synthetic invalid line", flush=True)
    prompt = args.prompt or ""
    answer = json.dumps(dict(prompt=prompt, model=args.model, cwd=str(Path.cwd()),
                             conversation=args.conversation, turn=turn))
    if os.environ.get("FAKE_AGY_EDIT") and "Read-only task." not in prompt:
        Path("edited.txt").write_text("edited\n")
    emit("step_update", dict(conversation_id=identifier, step_index=turn * 2,
                             state="ACTIVE", step_type="agent_response", text_delta=answer))
    usage = dict(input_tokens=100 * turn, output_tokens=10 * turn,
                 thinking_tokens=5 * turn, cache_read_tokens=0, total_tokens=110 * turn)
    if scenario == "declining" and args.conversation:
        usage["input_tokens"] = 50
    if scenario == "invalid-usage":
        usage["input_tokens"] = True
    emit("step_update", dict(conversation_id=identifier, step_index=turn * 2,
                             state="DONE", step_type="agent_response", usage=usage))
    if scenario == "no-init-corrupt":
        print("synthetic corrupt line after a valid step", flush=True)
    if scenario == "missing":
        return 0
    failed = scenario == "fallback" and args.model.endswith("-high")
    if failed and os.environ.get("FAKE_AGY_GATE"):
        deadline = time.monotonic() + 8
        while not Path(os.environ["FAKE_AGY_GATE"]).exists() and time.monotonic() < deadline:
            time.sleep(0.02)
    terminal = dict(conversation_id=identifier, status="ERROR" if failed else "SUCCESS",
                    response=answer, usage=None if scenario == "missing-usage" else usage)
    if scenario == "unknown-status":
        terminal["status"] = "MAYBE"
    emit("result", terminal)
    return 1 if failed else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--output-format", choices=("json", "stream-json"), default="stream-json")
    parser.add_argument("--conversation")
    parser.add_argument("--model")
    parser.add_argument("--dangerously-skip-permissions", action="store_true")
    parser.add_argument("-p", "--print", dest="prompt")
    parser.add_argument("command", nargs="?", choices=("models",))
    args = parser.parse_args()
    if args.version:
        print("1.4.12 (fixture replay)")
        return 0
    if args.command == "models":
        sys.stdout.write((FIXTURES / "models.txt").read_text())
        return 0
    scenario = os.environ.get("FAKE_AGY_SCENARIO", "success")
    if scenario in RUNTIME_SCENARIOS:
        return lifecycle(args, scenario)
    manifest = json.loads((FIXTURES / "manifest.json").read_text())
    filename = scenario + (".json" if args.output_format == "json" else ".ndjson")
    if filename not in manifest["files"]:
        parser.error(f"unknown fixture: {filename}")
    sys.stdout.write((FIXTURES / filename).read_text())
    stderr = manifest["files"][filename].get("stderr")
    if stderr:
        sys.stderr.write((FIXTURES / stderr).read_text())
    # A timeout capture really exited zero. The supervisor must inspect its result.
    return manifest["files"][filename]["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
