"""Claude context budget messages and handoff prompts."""

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

PREAMBLE = """[claive budget] The previous session of this worker reached its context budget and wrote the
handoff below. This is a new session with an empty context. Continue the assignment from the
handoff's Next step. The original assignment follows the handoff for reference."""


def warn_message(tokens, values):
    return WARN.format(k=tokens // 1000, stop_k=values["stop"] // 1000, template=TEMPLATE)


def stop_message(tokens, values, with_template):
    message = STOP.format(k=tokens // 1000, stop_k=values["stop"] // 1000)
    return message + "\n\n" + TEMPLATE if with_template else message


def is_handoff(text):
    first = next((line for line in text.splitlines() if line.strip()), "")
    return first.strip() == "[claive-handoff]"


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
