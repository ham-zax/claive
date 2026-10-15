"""Read-only, numbered view of the steps in a worker's provider log."""
from collections import deque
import json
import os
import time

from claivelib.ansi import strip_controls


STEP_CHARS = 600
MAX_STEPS = 50


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def content_text(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(block["text"] for block in content if isinstance(block, dict)
                   and block.get("type") == "text" and isinstance(block.get("text"), str))


# clip() and the paging rules in steps_window() are adapted from lop
# (local_operator/harness/comms.py: _clip, _resolve_peek_range).
# Copyright (c) 2024 Radient Inc. Licensed under the MIT License:
# Permission is hereby granted, free of charge, to any person obtaining a copy of this
# software and associated documentation files (the "Software"), to deal in the Software
# without restriction, including without limitation the rights to use, copy, modify,
# merge, publish, distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to the following
# conditions: The above copyright notice and this permission notice shall be included
# in all copies or substantial portions of the Software. THE SOFTWARE IS PROVIDED "AS
# IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED.
def clip(text, limit=STEP_CHARS):
    """Keep the head (what was asked or run) and the tail (how it ended)."""
    text = text.strip()
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    tail = limit - head
    return text[:head] + f"\n[… {len(text) - limit} chars elided …]\n" + text[-tail:]


def steps_window(path, engine, count, start=None, human_output=False):
    """Return (steps, total, intent). Step numbers are stable across calls.

    The whole log is streamed so that numbers do not shift; memory stays bounded
    because only the requested window and latest text step are kept.
    """
    if start is not None and start < 1:
        raise ValueError("--start must be at least 1")
    from claivelib import intent, redaction

    window = deque(maxlen=count) if start is None else []
    total = 0
    try:
        stream = (path / "events.jsonl").open("rb")
    except FileNotFoundError:
        return [], 0, None
    last_dedupe_key = None
    latest_text_step = None
    with stream:
        for line in stream:
            if not line.endswith(b"\n"):
                break  # A running worker may be writing this line now.
            try:
                event = json.loads(line)
            except (ValueError, UnicodeError):
                last_dedupe_key = None
                continue
            if not isinstance(event, dict):
                last_dedupe_key = None
                continue
            dedupe_key = engine.dedupe_key(event)
            if dedupe_key is not None and dedupe_key == last_dedupe_key:
                continue
            last_dedupe_key = dedupe_key
            for step in engine.describe_event(event):
                total += 1
                if step["kind"] == "text":
                    latest_text_step = step
                if start is None or start <= total < start + count:
                    text = redaction.redact(step["text"])
                    if human_output:
                        from claivelib import messaging
                        text = messaging.human_parent_message(text) or text
                    window.append(dict(index=total, kind=step["kind"], text=clip(text)))
    if start is not None and total and start > total:
        raise ValueError(f"nothing at step {start}: the worker has {total} step(s) right now")
    latest_intent = None
    if latest_text_step:
        text = redaction.redact(latest_text_step["text"])
        if human_output:
            from claivelib import messaging
            text = messaging.human_parent_message(text) or text
        latest_intent = intent.intent_from_steps([dict(kind="text", text=text)])
    if latest_intent is not None:
        latest_intent = redaction.redact(latest_intent)
    return list(window), total, latest_intent


def latest_intent(path, engine, human_output=False):
    """Read the full event stream and return its latest redacted text intent."""
    return steps_window(path, engine, 1, human_output=human_output)[2]


def show(worker_id, count=5, json_output=False, start=None):
    from claivelib import cli
    from claivelib.engines import DEFAULT_ENGINE, get_engine

    if "CLAIVE_WORKER_ID" in os.environ:
        raise ValueError("only the parent can peek at workers")
    if not 1 <= count <= MAX_STEPS:
        raise ValueError(f"--steps must be between 1 and {MAX_STEPS}")
    path = cli.job_path(worker_id)
    state = cli.load(path)
    activity = state.get("last_activity")
    age = max(0, time.time() - activity) if isinstance(activity, (int, float)) else None
    result = dict(id=path.name, status=state.get("status", "unknown"),
                  phase=state.get("phase", "unknown"), turn=state.get("turn", 0),
                  last_activity_age_seconds=age, intent=None)
    result["steps"], result["total"], result["intent"] = steps_window(
        path, get_engine(state.get("engine", DEFAULT_ENGINE)), count, start,
        human_output=not json_output)
    if json_output:
        print(json.dumps(result, ensure_ascii=False))
    else:
        duration = cli.duration(age) if age is not None else "unknown"
        print(strip_controls(f"Worker {result['id']} | {result['status']} | turn {result['turn']} | "
                             f"{result['phase']} | last event {duration} ago"))
        if result["intent"] is not None:
            print(f"Intent: {cli.clean(result['intent'])}")
        steps = result["steps"]
        if steps:
            print(strip_controls(f"Steps {steps[0]['index']}-{steps[-1]['index']} of {result['total']}"))
        for step in steps:
            print(strip_controls(f"[{step['index']}] {step['kind']}: "
                                 + " ⏎ ".join(step["text"].splitlines())))
        if not steps:
            print("No steps recorded yet.")
        elif steps[-1]["index"] < result["total"]:
            print(strip_controls(f"More: claive peek {result['id']} --start {steps[-1]['index'] + 1}"))
    return 0
