"""Strip terminal control sequences from text rendered for a human."""
import re


# Control stripping adapted from lop (local_operator/ansi.py: _CONTROL_RE,
# strip_control_sequences).
# Copyright (c) 2024 Radient Inc. Licensed under the MIT License:
# Permission is hereby granted, free of charge, to any person obtaining a copy of this
# software and associated documentation files (the "Software"), to deal in the Software
# without restriction, including without limitation the rights to use, copy, modify,
# merge, publish, distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to the following
# conditions: The above copyright notice and this permission notice shall be included
# in all copies or substantial portions of the Software. THE SOFTWARE IS PROVIDED "AS
# IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED.
_CONTROL_RE = re.compile(
    # OSC / DCS / SOS / PM / APC, 7- and 8-bit, including their payloads.
    r"(?:\x1b[\]PX^_]|[\x9d\x90\x98\x9e\x9f])(?:(?!\x07|\x1b\\|\x9c).)*(?:\x07|\x1b\\|\x9c|$)"
    # CSI, 7- and 8-bit: parameters, intermediates, final byte.
    r"|(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]"
    # An incomplete CSI at a truncation boundary is removed through end of input.
    r"|(?:\x1b\[|\x9b)[0-?]*[ -/]*$"
    # ESC + intermediate + final, then other two-character escapes and lone ESC.
    r"|\x1b[ -/]+[0-~]"
    r"|\x1b[@-Z\\-_]"
    r"|\x1b$"
    # C0 controls except TAB and LF, plus DEL and the remaining C1 controls.
    r"|[\x00-\x08\x0b-\x1f\x7f-\x9f]",
    re.DOTALL,
)


def strip_controls(text: str) -> str:
    """Remove terminal escape sequences and control characters, preserving text."""
    return _CONTROL_RE.sub("", text)
