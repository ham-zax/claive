"""Role presets for claive workers."""
from copy import deepcopy

from claivelib import config

ROLES = {
    "scout": {
        "engine": "pi",
        "model": "mimo-v2.6-flash-free",
        "reasoning_effort": "max",
        "read_only": True,
        "max_model_steps": None,
        "preamble": (
            "You are a scout. Map the relevant code: entry points, data flow, "
            "and key files with file:line references. List risks and unknowns. "
            "No edits. Report findings concisely; do not implement or change anything."
        ),
    },
    "worker": {
        "engine": "muse",
        "model": "muse-spark-1.3-contributor",
        "reasoning_effort": "xhigh",
        "read_only": False,
        "max_model_steps": None,
        "preamble": (
            "You are a worker. Implement only the assigned files. No commit, no staging, "
            "no delegation. Run the named checks and report their results. When a choice "
            "would change design, behaviour, or scope, escalate with needs_decision "
            "rather than guess."
        ),
    },
    "reviewer": {
        "engine": "pi",
        "model": "big-pickle",
        "reasoning_effort": "max",
        "read_only": True,
        "max_model_steps": None,
        "preamble": (
            "You are a reviewer. No edits. First identify added locks, caps, retries, "
            "flags, gates, or queues that lack an observed failure or stated requirement. "
            "Trace ordinary use from entry to result before checking rare inputs. "
            "For each finding, show a reachable trigger and its consequence, then try "
            "to disprove the finding. Report only findings that survive that check, "
            "with file:line locations and evidence from the diff or verifier output. "
            "For process code, prioritize silent failures, waits without a timeout, "
            "incorrect control flow, and competing writers to the same resource. "
            "Say plainly if no concrete defects are found."
        ),
    },
    "oracle": {
        "engine": "pi",
        "model": "space-bunny-free",
        "reasoning_effort": "max",
        "read_only": True,
        "max_model_steps": None,
        "preamble": (
            "You are an oracle. No edits. Challenge assumptions, name risks and "
            "alternatives, and state what evidence would change the view. Prefer "
            "concrete reasoning over vague concerns."
        ),
    },
}


def resolved_roles(config_data=None):
    """Built-in roles with per-host overrides and custom roles applied."""
    roles = deepcopy(ROLES)
    for name, override in config.role_overrides(config_data).items():
        role = roles.setdefault(name, {})
        if "prompt_file" in override:
            role.pop("preamble", None)
        role.update(override)
    return roles
