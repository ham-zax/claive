"""Per-host claive configuration: the default worker engine and role overrides.

The file is ${CLAIVE_CONFIG:-${XDG_CONFIG_HOME:-~/.config}/claive/config.json}; a missing file means
the built-in defaults. CLAIVE_ENGINE overrides default_engine. Example (a Pi-only host):

    {"default_engine": "pi",
     "roles": {"worker": {"engine": "pi", "model": "muse-spark-1.3-contributor-free"}},
     "providers": {"opencode2api": {"base_url": "http://127.0.0.1:8080/v1"}}}

providers.opencode2api.base_url must be a loopback URL; see claivelib.provider.

"workspaces": [{"path": "~/work", "write": true}, {"path": "~/repo/notes"}] is an allowlist: when the
key is present, workers may only run inside a listed directory, and only read-only unless the
nearest listed ancestor sets "write": true. Without the key, any workspace is allowed.
"""
import json
import os
from pathlib import Path

TOP_LEVEL_KEYS = {"default_engine", "roles", "providers", "workspaces"}
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
    providers = data.get("providers", {})
    if not isinstance(providers, dict) or set(providers) - {"opencode2api"}:
        raise ValueError(f"invalid claive config {path}: providers may only hold opencode2api")
    for name, entry in providers.items():
        if not isinstance(entry, dict) or set(entry) - {"base_url"} or not isinstance(entry.get("base_url"), str):
            raise ValueError(f"invalid claive config {path}: providers.{name} must be {{\"base_url\": \"...\"}}")
        from claivelib.provider import check_loopback
        try:
            check_loopback(entry["base_url"])
        except ValueError as error:
            raise ValueError(f"invalid claive config {path}: providers.{name}.base_url {error}") from error
    workspaces = data.get("workspaces")
    if workspaces is not None:
        if not isinstance(workspaces, list):
            raise ValueError(f"invalid claive config {path}: workspaces must be a list")
        for index, entry in enumerate(workspaces):
            where = f"invalid claive config {path}: workspaces[{index}]"
            if not isinstance(entry, dict) or set(entry) - {"path", "write"}:
                raise ValueError(f"{where} must be {{\"path\": ..., \"write\": true|false}}")
            if not isinstance(entry.get("path"), str) or not Path(entry["path"]).expanduser().is_absolute():
                raise ValueError(f"{where}.path must be an absolute or ~ path")
            if not isinstance(entry.get("write", False), bool):
                raise ValueError(f"{where}.write must be true or false")
    return data


def check_workspace(directories, read_only):
    """Refuse a launch outside the workspaces allowlist, or a writable one in a read-only entry."""
    allowed = load().get("workspaces")
    if allowed is None:
        return
    entries = [(Path(os.path.realpath(Path(entry["path"]).expanduser())), entry.get("write", False))
               for entry in allowed]
    for directory in directories:
        real = Path(os.path.realpath(directory))
        matches = [(base, write) for base, write in entries if real == base or base in real.parents]
        if not matches:
            raise ValueError(f"workspace {real} is outside the claive config workspaces allowlist")
        _base, write = max(matches, key=lambda match: len(match[0].parts))
        if not write and not read_only:
            raise ValueError(f"workspace {real} is read-only in the claive config workspaces allowlist; "
                             "pass --read-only or use a read-only role")


def default_engine(builtin):
    """Engine for new workers that name neither --engine nor a role."""
    override = os.environ.get("CLAIVE_ENGINE")
    if override:
        return override
    return load().get("default_engine") or builtin


def role_overrides():
    return load().get("roles", {})
