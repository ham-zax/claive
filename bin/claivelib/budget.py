"""Claude context budget messages, handoff prompts and the tool-call gate."""
import json
import os
from pathlib import Path
import tempfile
import time

# Claude Code delivers stdin messages only after the pending tool result, which can add ~25k tokens.
# So a PreToolUse hook asks the supervisor about each call and denies it after the stop limit.
GATE_FILE = "budget-gate.json"
GATE_WAIT_SECONDS = 2.0  # the hook allows the call when the supervisor has not seen it by then
GATE_IDS = 256
HOOK_SETTINGS = {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [
    {"type": "command", "command": '"$CLAIVE_BIN" _budget_gate'}]}]}}

TEMPLATE = """The handoff reply replaces this whole session for the next one. Its first line must be exactly:
[claive-handoff]
Keep only what is expensive or impossible to rebuild from git and the repository. About 2000
words at most. Omit empty sections. Keep paths, commands, IDs and error messages verbatim. Give
pointers (file:line, command) instead of file contents. Do not list modified files: git shows them.

## Goal
The assignment and its done condition. Quote the parent's direction-setting instructions verbatim.
## State
What is done and how it was verified (command and result). What is in progress.
## Decisions
DECISION: choice, reason.
## Dead ends
FAILED: what was tried, why it failed.
## Facts to keep
file:line locations, commands, measured numbers, environment quirks.
## Live state
Running processes, background jobs, temp files.
## Next step
The exact next action and its stop condition."""

WARN = """[claive budget] Context is at {k}k tokens. At {stop_k}k claive tells you to stop at once.
Reach a checkpoint before then: finish and verify the current step. Do not start large new work.
If the assignment is complete at the checkpoint, give your normal final answer.
Otherwise end your turn with a handoff reply in the format below. claive then continues the
work in a new session that sees only the handoff and the original assignment.

{template}"""

STOP = """[claive budget] Context is at {k}k tokens, at the {stop_k}k stop limit. Stop now. Do not finish
the current step. Let background jobs run. End your turn with the handoff reply now."""

# A tool result is data to the model, and Haiku ignores instructions in it as a possible injection
# (live run 37c07187a9fc). So the deny reason states only the fact; the stdin stop message instructs.
GATE_DENY = """[claive budget] claive, which runs this session, denied this tool call. The context is at
{k}k tokens, at the {stop_k}k stop limit, and claive denies every tool call in this session from now
on. claive's instructions arrive as a user message."""

LATE_MARKER = "The stop reply has [claive-handoff] on line {line}, not on the first nonblank line. claive counted no handoff."

PREAMBLE = """[claive budget] The previous session of this worker reached its context budget and wrote the
handoff below. This is a new session with an empty context. Continue the assignment from the
handoff's Next step. The original assignment follows the handoff for reference."""


def warn_message(tokens, values):
    return WARN.format(k=tokens // 1000, stop_k=values["stop"] // 1000, template=TEMPLATE)


def stop_message(tokens, values, with_template):
    message = STOP.format(k=tokens // 1000, stop_k=values["stop"] // 1000)
    return message + "\n\n" + TEMPLATE if with_template else message


def gate_deny_message(tokens, values):
    return GATE_DENY.format(k=tokens // 1000, stop_k=values["stop"] // 1000)


def is_handoff(text):
    first = next((line for line in text.splitlines() if line.strip()), "")
    return first.strip() == "[claive-handoff]"


def late_marker_line(text):
    if is_handoff(text):
        return None
    return next((number for number, line in enumerate(text.splitlines(), 1)
                 if line.strip() == "[claive-handoff]"), None)


def next_step(text):
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line == "## Next step":
            body = []
            for following in lines[index + 1:]:
                if following.startswith(("# ", "## ")):
                    break
                body.append(following)
            return "\n".join(body).strip()[:300]
    return ""


def continuation_prompt(handoff_text, original_prompt):
    return f"{PREAMBLE}\n\n{handoff_text}\n\n---\nOriginal assignment:\n{original_prompt}"


def answer_prompt(handoff_text, parent_prompt):
    return f"{PREAMBLE}\n\n{handoff_text}\n\n---\nParent message:\n{parent_prompt}"


def write_gate(job_dir, stop, seen):
    """Record the tool calls the supervisor has seen and the stop text (None while under stop)."""
    job_dir = Path(job_dir)
    fd, temporary = tempfile.mkstemp(prefix=".gate-", dir=job_dir)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump({"stop": stop, "seen": list(seen)[-GATE_IDS:]}, stream)
        os.replace(temporary, job_dir / GATE_FILE)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def gate_reason(job_dir, tool_use_id, wait=GATE_WAIT_SECONDS):
    """The deny reason for a tool call, or None to allow it.

    The hook can start before the supervisor reads the assistant event that requested the call,
    so it waits until the supervisor has seen the call and only then trusts the stop state."""
    path = Path(job_dir) / GATE_FILE
    deadline = time.monotonic() + wait
    while True:
        try:
            gate = json.loads(path.read_text())
        except FileNotFoundError:
            return None
        except ValueError:
            gate = {}
        if gate.get("stop"):
            return gate["stop"]
        if not tool_use_id or tool_use_id in gate.get("seen", ()) or time.monotonic() >= deadline:
            return None
        time.sleep(0.01)


def gate_output(job_dir, hook_input):
    """The PreToolUse hook's stdout for one call: a deny decision or nothing."""
    try:
        tool_use_id = json.loads(hook_input).get("tool_use_id")
    except (ValueError, AttributeError):
        tool_use_id = None
    reason = gate_reason(job_dir, tool_use_id)
    if reason is None:
        return ""
    return json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                              "permissionDecisionReason": reason}})
