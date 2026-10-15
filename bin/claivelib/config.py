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

"models" is an allowlist of models, each with the reasoning efforts it may use:

    {"engines": {"codex": {"models": {"gpt-6-luna": {"efforts": ["max"]},
                                      "gpt-6.1-sol": {"efforts": ["low", "medium", "high", "xhigh"]}}},
                 "claude": {"models": {"claude-haiku-5-5": {"efforts": ["high", "xhigh"]}}}}}

When the key is present, a model that is not listed is refused, and so is an effort that is
not in its "efforts" list ({} or no "efforts" allows every effort the engine supports). The
allowlist can only narrow what the engine supports: a model or effort the engine does not
support makes the config invalid, and claive then refuses every launch.
"""
import json
import os
from pathlib import Path
import re

TOP_LEVEL_KEYS = {"default_engine", "roles", "providers", "workspaces", "engines", "teams"}
ENGINE_KEYS = {
    "enabled": bool, "reason": str, "default_model": str, "default_reasoning_effort": str,
    "disabled_models": (list, dict), "disabled_reasoning_efforts": (list, dict), "models": dict,
    "user_config": bool, "extra_args": list,
}
# Flags each engine's command builder owns: output format, print mode, model, effort, session, resume.
OWNED_FLAGS = {
    "claude": {"--print", "-p", "--output-format", "--input-format", "--model", "--effort", "--resume", "-r",
               "--session-id", "--continue", "-c", "--fork-session", "--no-session-persistence", "--verbose",
               "--json-schema", "--replay-user-messages"},
    "codex": {"--json", "-m", "--model", "--cd", "-C", "--ephemeral"},
    "muse": {"--json", "--prompt-file", "--provider", "--model", "--reasoning-effort", "--max-model-steps",
             "--session-id", "--workspace", "-w", "--worktree-existing", "--worktree-base"},
    "pi": {"--mode", "--print", "-p", "--provider", "--model", "--thinking", "--session", "--session-id",
           "--session-dir", "--no-session", "--fork", "--continue", "-c", "--resume", "-r"},
}
ROLE_KEYS = {
    "engine": str, "model": str, "reasoning_effort": str, "read_only": bool,
    "max_model_steps": (int, type(None)), "preamble": str, "prompt_file": str,
}
NAME_PATTERN = r"[a-z][a-z0-9_-]{0,31}"


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
        if not isinstance(name, str) or not re.fullmatch(NAME_PATTERN, name):
            raise ValueError(f"invalid claive config {path}: role name {name} is invalid")
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
        prompt_file = override.get("prompt_file")
        if prompt_file is not None and not Path(prompt_file).is_absolute():
            raise ValueError(f"invalid claive config {path}: roles.{name}.prompt_file must be an absolute path")
        if "preamble" in override and "prompt_file" in override:
            raise ValueError(f"invalid claive config {path}: roles.{name}: use preamble or prompt_file, not both")
    from claivelib.roles import ROLES as BUILTIN_ROLES
    for name, override in roles.items():
        if name not in BUILTIN_ROLES and not all(key in override for key in ("engine", "read_only")):
            raise ValueError(f"invalid claive config {path}: roles.{name} is a new role and needs engine, read_only and preamble or prompt_file")
        if name not in BUILTIN_ROLES and not ("preamble" in override or "prompt_file" in override):
            raise ValueError(f"invalid claive config {path}: roles.{name} is a new role and needs engine, read_only and preamble or prompt_file")
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
            if key == "user_config" and name != "codex":
                raise ValueError(f"{where}.user_config is only for codex")
            expected = ENGINE_KEYS.get(key)
            if expected is None:
                raise ValueError(f"{where}: unknown key {key}")
            if not isinstance(value, expected):
                raise ValueError(f"{where}.{key} has the wrong type")
        extra = policy.get("extra_args", [])
        if not all(isinstance(item, str) and item for item in extra):
            raise ValueError(f"{where}.extra_args must hold nonempty strings")
        for item in extra:
            if item == "--" or item.split("=", 1)[0] in OWNED_FLAGS.get(name, set()):
                raise ValueError(f"{where}.extra_args: claive owns {item.split('=', 1)[0]}")
        for key, default in (("disabled_models", "default_model"),
                             ("disabled_reasoning_efforts", "default_reasoning_effort")):
            listed = policy.get(key, [])
            reasons = listed.values() if isinstance(listed, dict) else []
            if not all(isinstance(item, str) and item for item in listed) or not all(
                    isinstance(reason, str) for reason in reasons):
                raise ValueError(f"{where}.{key} must hold nonempty names (with string reasons)")
            if default in policy and (not policy[default] or _disabled(listed, policy[default]) is not None):
                raise ValueError(f"{where}.{default} must be a nonempty name that {key} does not disable")
        models = policy.get("models")
        if models is not None:
            for model, entry in models.items():
                efforts = entry.get("efforts", []) if isinstance(entry, dict) else None
                if (not model or not isinstance(entry, dict) or set(entry) - {"efforts"}
                        or not isinstance(efforts, list)
                        or not all(isinstance(item, str) and item for item in efforts)):
                    raise ValueError(f"{where}.models.{model} must be {{\"efforts\": [nonempty names]}}")
            if "default_model" in policy and policy["default_model"] not in models:
                raise ValueError(f"{where}.default_model must be one of engines.{name}.models")
            allowed = models.get(policy.get("default_model"), {}).get("efforts")
            if allowed and policy.get("default_reasoning_effort", allowed[0]) not in allowed:
                raise ValueError(f"{where}.default_reasoning_effort must be one of the efforts of default_model")
    if engine is not None and engines.get(engine, {}).get("enabled") is False:
        raise ValueError(f"invalid claive config {path}: default_engine {engine} is disabled in engines.{engine}")
    _validate_teams(data.get("teams", {}), path, set(BUILTIN_ROLES) | set(roles))
    return data


def _validate_teams(teams, path, role_names):
    if not isinstance(teams, dict):
        raise ValueError(f"invalid claive config {path}: teams must be an object")
    from claivelib.aliases import validate_name as validate_alias_name
    for team, value in teams.items():
        where = f"invalid claive config {path}: teams.{team}"
        if not isinstance(team, str) or not re.fullmatch(NAME_PATTERN, team):
            raise ValueError(f"invalid claive config {path}: teams.{team}: team name is invalid")
        if not isinstance(value, dict):
            raise ValueError(f"{where} must be an object")
        unknown = sorted(set(value) - {"collaboration", "project", "members"})
        if unknown:
            raise ValueError(f"{where}: unknown keys {', '.join(unknown)}")
        for key in ("collaboration", "project"):
            file_path = value.get(key)
            if file_path is not None and (not isinstance(file_path, str) or not Path(file_path).is_absolute()):
                raise ValueError(f"{where}.{key} must be an absolute path")
        members = value.get("members")
        if not isinstance(members, dict) or not members:
            raise ValueError(f"{where}.members must be a nonempty object")
        for member, role in members.items():
            member_where = f"{where}.members.{member}"
            if not isinstance(member, str) or not re.fullmatch(NAME_PATTERN, member):
                raise ValueError(f"{member_where}: member name is invalid")
            alias = f"{team}-{member}"
            try:
                validate_alias_name(alias)
            except ValueError as error:
                raise ValueError(f"{member_where}: alias {alias} is invalid") from error
            if not isinstance(role, str) or role not in role_names:
                raise ValueError(f"{member_where}: unknown role {role}")


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
    """Whether the user disabled this model on the engine (or the engine itself), or left it off the allowlist."""
    policy = engine_policy(name)
    models = policy.get("models")
    return (policy.get("enabled") is False or _disabled(policy.get("disabled_models", []), model) is not None
            or (models is not None and model not in models))


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
        _check_allowed(name, policy.get("models"), model, reasoning_effort)
        return
    subject, reason = refused
    raise ValueError(f"{subject} is explicitly disabled by the user in {config_path()}"
                     + (f" (reason: {reason})" if reason else "")
                     + "; do not retry or work around this, choose an enabled alternative or ask the user")


def _check_allowed(name, models, model, reasoning_effort):
    """Refuse a model or effort outside the engine's models allowlist (no allowlist allows all)."""
    if models is None or not model:
        return
    if model not in models:
        raise ValueError(f"model {model} on worker engine {name} is not in the models allowlist in "
                         f"{config_path()} (allowed: {', '.join(models) or 'none'}); "
                         "do not retry or work around this, choose an allowed model or ask the user")
    efforts = models[model].get("efforts") or []
    if reasoning_effort and efforts and reasoning_effort not in efforts:
        raise ValueError(f"reasoning effort {reasoning_effort} for model {model} on worker engine {name} "
                         f"is not allowed in {config_path()} (allowed: {', '.join(efforts)}); "
                         "do not retry or work around this, choose an allowed effort or ask the user")


def check_model_named(name, model):
    """With a models allowlist, a turn must name its model: an unnamed one could be any model."""
    models = engine_policy(name).get("models")
    if models is not None and not model:
        raise ValueError(f"worker engine {name} has a models allowlist in {config_path()}; pass --model "
                         f"(allowed: {', '.join(models) or 'none'}) or set engines.{name}.default_model")


def allowed_models(name, supported):
    """The effective models and efforts: the engine's support narrowed by the allowlist.

    None means any model (the engine is open and the config has no allowlist).
    """
    models = engine_policy(name).get("models")
    if models is None:
        return None if supported is None else {model: list(efforts) for model, efforts in supported.items()}
    result = {}
    for model, entry in models.items():
        efforts = entry.get("efforts") or (list(supported[model]) if supported else [])
        result[model] = list(efforts)
    return result


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


def role_overrides(data=None):
    return (data if data is not None else load()).get("roles", {})
