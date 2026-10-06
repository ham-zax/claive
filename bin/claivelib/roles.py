"""Role presets for claive workers."""

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
        "max_model_steps": 100,
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
            "You are a reviewer. No edits. Report concrete defects with file:line "
            "locations and evidence from the diff or verifier output. Be specific about "
            "what is wrong and why. Say plainly if none found."
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
