"""Decode schema-v1 Muse worker state without requiring Muse itself."""
from pathlib import Path


def _value(command, flag, default=None):
    try:
        return command[command.index(flag) + 1]
    except (ValueError, IndexError):
        return default


def decode_launch(state):
    command = list(state.get("command") or [])
    if not command:
        raise ValueError("legacy worker state has no command to decode")

    isolation_mode = "none"
    worktree_mode = _value(command, "-w")
    if worktree_mode in {"create", "existing"}:
        isolation_mode = worktree_mode

    return {
        "binary": str(Path(command[0]).resolve()) if command[0] else command[0],
        "provider": _value(command, "--provider", "meta"),
        "model": _value(command, "--model"),
        "read_only": "--disable-write" in command and "--disable-shell" in command,
        "web": "--disable-web-tools" not in command,
        "output_schema": _value(command, "--output-schema"),
        "session_logging": "--no-session-log" not in command,
        "isolation": {
            "mode": isolation_mode,
            "base": _value(command, "--worktree-base"),
            "existing_path": _value(command, "--worktree-existing"),
        },
    }


def decode_session_id(state):
    return state.get("muse_session_id") or _value(list(state.get("command") or []), "--session-id")
