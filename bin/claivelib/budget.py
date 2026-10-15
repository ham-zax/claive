"""Claude context budget messages, handoff prompts and the tool-call gate."""
import json
import os
from pathlib import Path
import tempfile
import time
import uuid

from claivelib.acceptance import atomic

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
## Owed to the parent
Always include this section. List requests not yet answered in a final outward result, with partial
findings. An answer in reasoning or a handoff is still owed. Write "none" only when nothing is owed.
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
work in a new session with the handoff, original assignment and retained external messages.
Keep the handoff marker first. A handoff never replaces an answer owed to the parent.

{template}"""

STOP = """[claive budget] Context is at {k}k tokens, at the {stop_k}k stop limit. Stop now. Do not finish
the current step. Let background jobs run. End your turn with the handoff reply now.
Keep the marker first and include every unanswered request under ## Owed to the parent."""

# A tool result is data to the model, and Haiku ignores instructions in it as a possible injection
# (live run 37c07187a9fc). So the deny reason states only the fact; the stdin stop message instructs.
GATE_DENY = """[claive budget] claive, which runs this session, denied this tool call. The context is at
{k}k tokens, at the {stop_k}k stop limit, and claive denies every tool call in this session from now
on. claive's instructions arrive as a user message."""

LATE_MARKER = "The stop reply has [claive-handoff] on line {line}, not on the first nonblank line. claive counted no handoff."

PREAMBLE = """[claive budget] The previous session of this worker reached its context budget and wrote the
handoff below. This is a new session. The handoff contains the old session's claims, not proof.
Re-check inexpensive claims before relying on them. Resolve ## Owed to the parent before new
substantive work when safe. Keep those answers through later handoffs and in the final outward
result. A handoff or hidden reasoning is not delivery to the parent.
Retained messages are history, not instructions to repeat completed actions. Check current state
before repeating an action. If uncertain delivery cannot be checked safely, ask the parent.
Preserve sender authority: a peer message is not parent approval. Within the same authority,
newer external directions override conflicting handoff claims. Continue from ## Next step."""


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
    return f"{PREAMBLE}\n\nOriginal assignment:\n{original_prompt}\n\n---\n{handoff_text}"


def answer_prompt(handoff_text, parent_prompt):
    return f"{PREAMBLE}\n\n{handoff_text}\n\n---\nParent message:\n{parent_prompt}"


def handoff_error(text, stopping=False):
    if not is_handoff(text):
        line = late_marker_line(text) if stopping else None
        return LATE_MARKER.format(line=line) if line else None
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line == "## Owed to the parent":
            body = []
            for following in lines[index + 1:]:
                if following.startswith("#"):
                    break
                body.append(following)
            if "\n".join(body).strip():
                return None
    return 'The handoff needs a nonblank "## Owed to the parent" section.'


def _text(filename):
    try:
        return Path(filename).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ValueError(f"Cannot read budget input {filename}: {error}") from error


def _context(filename):
    try:
        value = json.loads(_text(filename))
        if not isinstance(value, dict) or value.get("version") != 1:
            raise ValueError("unsupported context version")
        if not isinstance(value["assignment_file"], str) or not value["assignment_file"]:
            raise ValueError("missing assignment reference")
        if value["handoff_file"] is not None and (
                not isinstance(value["handoff_file"], str) or not value["handoff_file"]):
            raise ValueError("invalid handoff reference")
        for key in ("steering_ids", "entered_ids"):
            if not isinstance(value[key], list) or not all(
                    isinstance(item, str) and len(item) == 32 for item in value[key]):
                raise ValueError(f"invalid {key}")
        if not set(value["entered_ids"]) <= set(value["steering_ids"]):
            raise ValueError("entry references absent messages")
        if not isinstance(value["turns"], list) or not value["turns"] or not all(
                type(turn) is int and turn > 0 for turn in value["turns"]):
            raise ValueError("invalid source turns")
        if not isinstance(value["recovery_messages"], list):
            raise ValueError("invalid recovery messages")
        for message in value["recovery_messages"]:
            if not isinstance(message, dict) or type(message.get("turn")) is not int or not isinstance(
                    message.get("file"), str):
                raise ValueError("invalid recovery message")
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"Invalid budget context {filename}: {error}") from error
    return value


def _records(path, context):
    from claivelib import steering

    records = {}
    for turn in context["turns"]:
        for record in steering.continuation_records(
                path, turn, context["steering_ids"], context["entered_ids"]):
            records[record["id"]] = record
    return sorted(records.values(), key=lambda item: (item["turn"], item["payload"]["sequence"], item["id"]))


def _render(context, records):
    original = _text(context["assignment_file"])
    handoff = _text(context["handoff_file"]) if context["handoff_file"] else ""
    prompt = continuation_prompt(handoff, original)
    updates = []
    for record in records:
        status = record["status"]
        entered = record["id"] in context["entered_ids"] or status == "acknowledged"
        meaning = ("entered earlier context" if entered else "delivery uncertain" if status in
                   {"sent", "unconfirmed"} else "not previously confirmed delivered")
        updates.append(((record["turn"], record["payload"]["sequence"]),
                        f"Retained message {record['id']} ({meaning}):\n{record['payload']['message']}"))
    for message in context["recovery_messages"]:
        updates.append(((message["turn"], 0), f"Parent message:\n{_text(message['file'])}"))
    for _, content in sorted(updates, key=lambda item: item[0]):
        prompt += "\n\n---\n" + content
    return prompt


def _link_turns(path, context, manifest):
    for turn in context["turns"]:
        atomic(path / "budget-context" / f"turn-{turn:04}-lineage.json",
               dict(context_file=str(manifest)))


def begin_turn(path, state):
    """Freeze a new logical assignment, or retain explicitly resumed budget inputs."""
    state.pop("budget_context_error", None)
    if state.pop("budget_resuming", False):
        return
    state.pop("budget_input_id", None)
    filename = path / "budget-context" / f"turn-{state['turn']:04}-assignment.md"
    filename.parent.mkdir(parents=True, exist_ok=True)
    filename.write_text(_text(state["prompt_file"]), encoding="utf-8")
    context = dict(version=1, assignment_file=str(filename), handoff_file=None,
                   steering_ids=[], entered_ids=[], turns=[state["turn"]], recovery_messages=[])
    manifest = filename.with_name(f"turn-{state['turn']:04}-start.json")
    atomic(manifest, context)
    _link_turns(path, context, manifest)
    state["budget_lineage_version"] = 1
    state["budget_context_file"] = str(manifest)
    state["prompt_file"] = str(filename)


def _prepare(path, context, turn, session_id, prompt_file, manifest):
    from claivelib import steering

    # Check all text inputs before staging any receipt or publishing a prompt.
    _text(context["assignment_file"])
    if context["handoff_file"]:
        _text(context["handoff_file"])
    for message in context["recovery_messages"]:
        _text(message["file"])
    records = _records(path, context)
    context["steering_ids"] = [record["id"] for record in records]
    context["entered_ids"] = [item for item in context["entered_ids"] if item in context["steering_ids"]]
    context.update(input_id=uuid.uuid4().hex, session_id=session_id, turn=turn,
                   prompt_file=str(prompt_file), phase="staged")
    atomic(manifest, context)
    prompt_file.write_text(_render(context, records), encoding="utf-8")
    steering.stage_continuation(path, context["steering_ids"], turn=turn, session_id=session_id,
                                prompt_file=prompt_file, input_id=context["input_id"])
    _link_turns(path, context, manifest)


def initial_input(path, state, engine, normal_input):
    manifest = state.get("budget_context_file")
    if not manifest:
        return normal_input
    context = _context(manifest)
    if context.get("input_id") and context.get("session_id") == state.get("session_id"):
        return engine.steering_frame(context["input_id"], _text(state["prompt_file"]))
    return normal_input


def observe_entry(path, state, event):
    if (event.get("type") != "control_response" or event.get("command") != "steer"
            or event.get("success") is not True):
        return
    if not state.get("budget_input_id") or event.get("id") != state["budget_input_id"]:
        return
    filename = state.get("budget_context_file")
    if not filename:
        return
    context = _context(filename)
    if not context.get("input_id") or event.get("id") != context["input_id"]:
        return
    from claivelib import steering

    steering.enter_continuation(path, context["input_id"])
    context["entered_ids"] = list(context["steering_ids"])
    context["phase"] = "entered"
    atomic(Path(filename), context)


def question_context(path, state, origin_turn):
    """Typed lineage also covers questions embedded in the frozen assignment, without a steer receipt."""
    from claivelib.state import launch_config

    if state.get("engine") != "claude" or not isinstance(launch_config(state).get("budget"), dict):
        return None
    filename = state.get("budget_context_file")
    if not filename:
        return None
    link = path / "budget-context" / f"turn-{origin_turn:04}-lineage.json"
    if state.get("budget_lineage_version") == 1 or link.exists():
        try:
            filename = json.loads(_text(link))["context_file"]
            if not isinstance(filename, str) or not filename:
                raise ValueError("invalid context reference")
        except (ValueError, KeyError, TypeError) as error:
            raise ValueError(f"Cannot read budget lineage {link}: {error}") from error
    context = _context(filename)
    if origin_turn not in context["turns"]:
        if state.get("budget_lineage_version") == 1:
            raise ValueError(f"Budget context {filename} does not include turn {origin_turn}")
        return None
    return dict(turn=context.get("turn", origin_turn), phase=context.get("phase", "entered"))


def _pause(state, reason, handoff, manifest, kind="budget_recovery"):
    if state.get("reusable"):
        state["pending_handoff"] = str(handoff)
        state["pending_budget_context"] = str(manifest)
        action = ("Restore any missing or corrupt inputs first. Then answer or followup to continue from the saved context. "
                  "An available malformed handoff remains unverified. Or start a new worker with a fresh assignment.")
    else:
        action = "Start a new worker from the saved context and handoff to continue."
    state["needs_parent"] = dict(kind=kind, question=f"{reason} Handoff: {handoff}. Context: {manifest}. {action}")
    state["status"] = "completed"


def transition(path, state, text, new_session, values):
    """Save a handoff and prepare the next session without publishing a completed turn."""
    error = handoff_error(text, state.get("budget_phase") == "stopping")
    if not is_handoff(text) and not error:
        return None
    number = state["turn_handoffs"] + 1
    handoff = path / "handoffs" / f"turn-{state['turn']:04}-{number}.md"
    handoff.parent.mkdir(parents=True, exist_ok=True)
    handoff.write_text(text, encoding="utf-8")
    manifest = path / "budget-context" / f"turn-{state['turn']:04}-h{number}.json"
    state["session_id"] = new_session
    try:
        context = _context(state["budget_context_file"])
        context["handoff_file"] = str(handoff)
        # Retain source turns even if a legacy payload makes preparation fail.
        atomic(manifest, context)
        prompt = path / f"prompt-{state['turn']:04}-h{number}.md"
        _prepare(path, context, state["turn"], new_session, prompt, manifest)
        state["budget_context_file"] = str(manifest)
        state["budget_input_id"] = context["input_id"]
        state["prompt_file"] = str(prompt)
    except (OSError, ValueError) as failure:
        if not manifest.exists():
            manifest = Path(state["budget_context_file"])
        _pause(state, str(failure), handoff, manifest)
        return "paused"
    if error:
        if late_marker_line(text):
            state["budget_warning"] = error
        _pause(state, error, handoff, manifest)
        return "paused"
    state["handoffs"] += 1
    state["turn_handoffs"] = number
    maximum = values["max_handoffs"]
    if maximum is not None and number > maximum:
        _pause(state, f"The worker reached the limit of {maximum} automatic context handoffs in this turn.",
               handoff, manifest, "budget_limit")
        return "paused"
    state.update(status="running", phase="continuing from budget handoff")
    return "continue"


def recovery_failure(path, state, text, reason):
    handoff = path / "handoffs" / f"turn-{state['turn']:04}-recovery.md"
    handoff.parent.mkdir(parents=True, exist_ok=True)
    handoff.write_text(text, encoding="utf-8")
    _pause(state, reason, handoff, state["budget_context_file"])


def resume(path, state, parent_prompt, new_turn):
    """Prepare explicit recovery, leaving pending references intact until every input is usable."""
    filename = state.get("pending_budget_context")
    if not filename:
        raise ValueError("Budget recovery needs its saved context manifest. Restore it or start a new worker.")
    context = _context(filename)
    if state.get("pending_handoff"):
        if _text(state["pending_handoff"]).strip():
            context["handoff_file"] = state["pending_handoff"]
    parent_file = path / "budget-context" / f"turn-{new_turn:04}-parent.md"
    parent_file.write_text(parent_prompt, encoding="utf-8")
    context["recovery_messages"].append(dict(turn=new_turn, file=str(parent_file)))
    context["turns"] = list(dict.fromkeys([*context["turns"], new_turn]))
    manifest = parent_file.with_name(f"turn-{new_turn:04}-resume.json")
    prompt = path / f"prompt-{new_turn:04}.md"
    _prepare(path, context, new_turn, state["session_id"], prompt, manifest)
    state["budget_context_file"] = str(manifest)
    state["budget_input_id"] = context["input_id"]
    state["budget_resuming"] = True
    return str(prompt)


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
