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


FIXTURES = Path(__file__).parent / "fixtures" / "agy"


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
