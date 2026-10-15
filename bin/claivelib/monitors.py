"""Pure monitor interval and snapshot helpers.

The normalization, content hash and bounded unified-diff behavior are adapted
from lop ``local_operator/monitors/diff.py``.
Copyright (c) 2024 Radient Inc. Licensed under the MIT License:
Permission is hereby granted, free of charge, to any person obtaining a copy of this
software and associated documentation files (the "Software"), to deal in the Software
without restriction, including without limitation the rights to use, copy, modify,
merge, publish, distribute, sublicense, and/or sell copies of the Software, and to
permit persons to whom the Software is furnished to do so, subject to the following
conditions: The above copyright notice and this permission notice shall be included
in all copies or substantial portions of the Software. THE SOFTWARE IS PROVIDED "AS
IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED.
"""
import difflib
import hashlib
import re


MIN_INTERVAL_SECONDS = 30
MAX_INTERVAL_SECONDS = 24 * 60 * 60
MAX_DIFF_LINES = 40
MAX_DIFF_LINE_BYTES = 1024

_INTERVAL = re.compile(r"\s*(\d+)\s*([smh])\s*", re.IGNORECASE)


def parse_interval(value):
    """Parse the monitor cadence and enforce the supported 30s–24h range."""
    match = _INTERVAL.fullmatch(str(value))
    if not match:
        raise ValueError("--every must be a duration between 30s and 24h (for example 30s, 5m or 2h)")
    number, unit = match.groups()
    seconds = int(number) * {"s": 1, "m": 60, "h": 3600}[unit.lower()]
    if not MIN_INTERVAL_SECONDS <= seconds <= MAX_INTERVAL_SECONDS:
        raise ValueError("--every must be between 30s and 24h")
    return seconds


def next_due_at(now, interval_seconds):
    """Pure schedule step: the next check is one interval after this check."""
    return now + interval_seconds


def is_due(monitor, now):
    due_at = monitor.get("next_run_at")
    return (isinstance(due_at, (int, float)) and not isinstance(due_at, bool)
            and due_at <= now)


def normalize_output(text):
    """Normalize line endings and strip trailing whitespace on each line."""
    text = str(text).replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in text.split("\n"))


def content_hash(normalized):
    return hashlib.sha256(normalized.encode("utf-8", "replace")).hexdigest()


def render_unified_diff(old, new, max_lines=MAX_DIFF_LINES):
    """Render a bounded unified line diff, clipping pathological single lines."""
    lines = list(difflib.unified_diff(
        old.splitlines(), new.splitlines(), fromfile="previous", tofile="current", lineterm=""))
    if len(lines) > max_lines:
        lines = lines[:max(0, max_lines - 1)] + ["... diff truncated ..."]
    return "\n".join(_clip_utf8(line) for line in lines)


def _clip_utf8(line):
    encoded = line.encode("utf-8", "replace")
    if len(encoded) <= MAX_DIFF_LINE_BYTES:
        return line
    return encoded[:MAX_DIFF_LINE_BYTES - 3].decode("utf-8", "ignore") + "..."
