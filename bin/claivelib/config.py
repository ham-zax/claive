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

"engines" is the user's per-engine policy, enforced on every launch and every turn:

    {"engines": {"claude": {"enabled": false, "reason": "subscription quota is for interactive use"},
                 "pi": {"default_model": "big-pickle", "default_reasoning_effort": "high",
                        "disabled_reasoning_efforts": ["max"],
                        "disabled_models": {"space-bunny-free": "too slow"}}}}

"enabled": false refuses the engine outright and reports the optional "reason". The defaults apply
when neither a flag nor a role chooses; the engine still validates them. disabled_models and
disabled_reasoning_efforts are a list of names or an object mapping each name to a reason.
"""
import json
import os
from pathlib import Path

TOP_LEVEL_KEYS = {"default_engine", "roles", "providers", "workspaces", "engines"}
ENGINE_KEYS = {
    "enabled": bool, "reason": str, "default_model": str, "default_reasoning_effort": str,
    "disabled_models": (list, dict), "disabled_reasoning_efforts": (list, dict),
}
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
    engines = data.get("engines", {})
    if not isinstance(engines, dict):
        raise ValueError(f"invalid claive config {path}: engines must be an object")
    for name, policy in engines.items():
        where = f"invalid claive config {path}: engines.{name}"
        if not isinstance(policy, dict):
            raise ValueError(f"{where} must be an object")
        for key, value in policy.items():
            expected = ENGINE_KEYS.get(key)
            if expected is None:
                raise ValueError(f"{where}: unknown key {key}")
            if not isinstance(value, expected):
                raise ValueError(f"{where}.{key} has the wrong type")
        for key, default in (("disabled_models", "default_model"),
                             ("disabled_reasoning_efforts", "default_reasoning_effort")):
            listed = policy.get(key, [])
            reasons = listed.values() if isinstance(listed, dict) else []
            if not all(isinstance(item, str) and item for item in listed) or not all(
                    isinstance(reason, str) for reason in reasons):
                raise ValueError(f"{where}.{key} must hold nonempty names (with string reasons)")
            if default in policy and (not policy[default] or _disabled(listed, policy[default]) is not None):
                raise ValueError(f"{where}.{default} must be a nonempty name that {key} does not disable")
    if engine is not None and engines.get(engine, {}).get("enabled") is False:
        raise ValueError(f"invalid claive config {path}: default_engine {engine} is disabled in engines.{engine}")
    return data


def _disabled(listed, name):
    """The reason (possibly empty) when name is in a disabled list or object, else None."""
    for item in listed:
        if item.lower() == str(name).lower():
            return listed[item] if isinstance(listed, dict) else ""
    return None


def engine_policy(name):
    """The user's policy for one engine ({} when it has none)."""
    return load().get("engines", {}).get(name, {})


def model_disabled(name, model):
    """Whether the user disabled this model on the engine (or the engine itself)."""
    policy = engine_policy(name)
    return policy.get("enabled") is False or _disabled(policy.get("disabled_models", []), model) is not None


def check_engine(name, model=None, reasoning_effort=None):
    """Refuse an engine, model or reasoning effort the user disabled in the claive config."""
    policy = engine_policy(name)
    refused = None
    if policy.get("enabled") is False:
        refused = f"worker engine {name}", policy.get("reason", "")
    for what, key, value in (("model", "disabled_models", model),
                             ("reasoning effort", "disabled_reasoning_efforts", reasoning_effort)):
        reason = _disabled(policy.get(key, []), value) if refused is None and value else None
        if reason is not None:
            refused = f"{what} {value} on worker engine {name}", reason
    if refused is None:
        return
    subject, reason = refused
    raise ValueError(f"{subject} is explicitly disabled by the user in {config_path()}"
                     + (f" (reason: {reason})" if reason else "")
                     + "; do not retry or work around this, choose an enabled alternative or ask the user")


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
