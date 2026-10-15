"""Extract a short, safe intent line from provider steps."""
import re
import unicodedata


# Intent sanitization is adapted from lop's local_operator/harness/intent.py
# and its local_operator/ansi.py control-sequence stripping.
# Copyright (c) 2024 Radient Inc. Licensed under the MIT License:
# Permission is hereby granted, free of charge, to any person obtaining a copy of this
# software and associated documentation files (the "Software"), to deal in the Software
# without restriction, including without limitation the rights to use, copy, modify,
# merge, publish, distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to the following
# conditions: The above copyright notice and this permission notice shall be included
# in all copies or substantial portions of the Software. THE SOFTWARE IS PROVIDED "AS
# IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO
# THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY
# CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
# OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.


INTENT_MAX_CHARS = 120
_LIST_MARKER = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)")
_EMPHASIS = re.compile(r"(?<!\w)(\*\*|__|\*|_)(\S(?:.*?\S)?)\1(?!\w)")
_CONTROL_RE = re.compile(
    r"(?:\x1b[\]PX^_]|[\x9d\x90\x98\x9e\x9f])(?:(?!\x07|\x1b\\|\x9c).)*(?:\x07|\x1b\\|\x9c|$)"
    r"|(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]"
    r"|(?:\x1b\[|\x9b)[0-?]*[ -/]*$"
    r"|\x1b[ -/]+[0-~]"
    r"|\x1b[@-Z\\-_]|\x1b$"
    r"|[\x00-\x08\x0b-\x1f\x7f-\x9f]",
    re.DOTALL,
)


def _remove_emphasis(line):
    previous = None
    while previous != line:
        previous = line
        line = _EMPHASIS.sub(r"\2", line)
    return line


def intent_from_steps(steps) -> str | None:
    """Return the latest text step's first useful line, clipped to 120 chars."""
    latest = None
    for step in steps:
        if isinstance(step, dict) and step.get("kind") == "text":
            latest = step.get("text")
    if not isinstance(latest, str):
        return None

    for raw_line in latest.splitlines():
        line = _CONTROL_RE.sub("", raw_line)
        line = "".join(char for char in line if unicodedata.category(char) != "Cc")
        line = _LIST_MARKER.sub("", line).strip()
        line = _remove_emphasis(line).strip()
        if not line:
            continue
        if len(line) > INTENT_MAX_CHARS:
            return line[:INTENT_MAX_CHARS - 1].rstrip() + "…"
        return line
    return None
