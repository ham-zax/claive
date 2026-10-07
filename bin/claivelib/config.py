"""Per-host claive configuration: the default worker engine and role overrides.

The file is ${CLAIVE_CONFIG:-${XDG_CONFIG_HOME:-~/.config}/claive/config.json}; a missing file means
the built-in defaults. CLAIVE_ENGINE overrides default_engine. Example (a Pi-only host):

    {"default_engine": "pi",
     "roles": {"worker": {"engine": "pi", "model": "muse-spark-1.3-contributor-free"}}}
"""
import json
import os
from pathlib import Path

TOP_LEVEL_KEYS = {"default_engine", "roles"}
ROLE_KEYS = {
    "engine": str, "model": str, "reasoning_effort": str, "read_only": bool,
    "max_model_steps": (int, type(None)), "preamble": str,
}


def config_path():
    explicit = os.environ.get("CLAIVE_CONFIG")
    if explicit:
        return Path(explicit).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "claive" / "config.json"


def load():
    """Return the validated config dict ({} when no file exists); raise ValueError when it is invalid."""
    path = config_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"invalid claive config {path}: {error}") from error
    if not isinstance(data, dict):
        raise ValueError(f"invalid claive config {path}: must be a JSON object")
    unknown = sorted(set(data) - TOP_LEVEL_KEYS)
    if unknown:
        raise ValueError(f"invalid claive config {path}: unknown keys {', '.join(unknown)}")
    engine = data.get("default_engine")
    if engine is not None and (not isinstance(engine, str) or not engine):
        raise ValueError(f"invalid claive config {path}: default_engine must be a nonempty string")
    roles = data.get("roles", {})
    if not isinstance(roles, dict):
        raise ValueError(f"invalid claive config {path}: roles must be an object")
    for name, override in roles.items():
        if not isinstance(override, dict):
            raise ValueError(f"invalid claive config {path}: roles.{name} must be an object")
        for key, value in override.items():
            expected = ROLE_KEYS.get(key)
            if expected is None:
                raise ValueError(f"invalid claive config {path}: roles.{name}: unknown key {key}")
            if not isinstance(value, expected) or (expected is int and isinstance(value, bool)):
                raise ValueError(f"invalid claive config {path}: roles.{name}.{key} has the wrong type")
        steps = override.get("max_model_steps")
        if isinstance(steps, bool) or (steps is not None and steps < 1):
            raise ValueError(f"invalid claive config {path}: roles.{name}.max_model_steps must be positive or null")
    return data


def default_engine(builtin):
    """Engine for new workers that name neither --engine nor a role."""
    override = os.environ.get("CLAIVE_ENGINE")
    if override:
        return override
    return load().get("default_engine") or builtin


def role_overrides():
    return load().get("roles", {})
