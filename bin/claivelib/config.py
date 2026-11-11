"""Per-host claive configuration: the default worker engine and role overrides.

The file is YAML or JSON. Discovery: ${CLAIVE_CONFIG} when set, else the first of
config.yaml, config.yml and config.json in ${XDG_CONFIG_HOME:-~/.config}/claive/.
A missing file means the built-in defaults. CLAIVE_ENGINE overrides default_engine.
YAML is the preferred format; the JSON examples below have the same shape. Example
(a Pi-only host):

    {"default_engine": "pi",
     "roles": {"worker": {"engine": "pi", "model": "muse-spark-1.3-contributor-free"}},
     "providers": {"opencode2api": {"base_url": "http://127.0.0.1:8080/v1"}}}

providers.<name>.base_url redirects a provider Pi already knows to a loopback URL; see
claivelib.provider. It never defines a provider, and only localhost or loopback hosts are accepted.

"engines.<name>.providers" is the user's policy for that engine's provider labels, and
"engines.pi.default_provider" is the provider a bare Pi model resolves to:

    {"engines": {"pi": {"default_provider": "mistral",
                        "providers": {"opencode2api": {"enabled": false, "reason": "gateway retired"},
                                      "mistral": {"enabled": true}}}}}

Pi owns provider definitions, its built-in catalog and credentials. Claive owns this policy:
"enabled": false refuses a launch or follow-up with the optional "reason". Every engine accepts
the block for the provider label it is launched with. A Pi provider named here that Pi does not
know is accepted, because it may be added later; doctor reports it. Use
`claive pi provider add|remove|list|check` to edit Pi's models.json.

"workspaces": [{"path": "~/work", "write": true}, {"path": "~/repo/notes"}] is an allowlist: when the
key is present, workers may only run inside a listed directory, and only read-only unless the
nearest listed ancestor sets "write": true. Without the key, any workspace is allowed.

"engines" is the user's per-engine policy, enforced on every launch and every turn:

    {"engines": {"claude": {"enabled": false, "reason": "subscription quota is for interactive use"},
                 "pi": {"default_model": "big-pickle", "default_model_efforts": {"big-pickle": "high"},
                        "disabled_model_efforts": {"big-pickle": ["max"]},
                        "disabled_models": {"space-bunny-free": "too slow"}}}}

"enabled": false refuses the engine outright and reports the optional "reason". The model default
applies when neither a flag nor a role chooses; the engine still validates it. disabled_models is a
list of names or an object mapping each name to a reason. default_model_efforts maps a model to its
effort. disabled_model_efforts maps a model to the efforts it may not use, as a list or an object
mapping each effort to a reason.

Every engine also accepts the same four keys, so nothing an engine compiles in is unreachable
from this file. Each engine module declares the default it falls back to:

    {"engines": {"claude": {"binary": "/opt/claude/bin/claude", "provider": "bedrock",
                            "env": {"ANTHROPIC_LOG": "debug"}, "prompt_limit": 200000},
                 "agy": {"binary": "~/bin/agy"}}}

"binary" must be absolute (or start with ~), "provider" is the label the engine is launched with,
"env" is extra child environment merged over the engine's own, and "prompt_limit" is the maximum
prompt size in UTF-8 bytes. Precedence is flag, then this file, then the engine's
*_WORKER_BINARY environment variable, then its built-in path. `claive engine config` reports the
effective value and its source for every key; see docs/engine-configuration.md.

"models" is an allowlist of models, each with the reasoning efforts it may use:

    {"engines": {"codex": {"models": {"gpt-6-luna": {"efforts": ["max"]},
                                      "gpt-6.1-sol": {"efforts": ["low", "medium", "high", "xhigh"]}}},
                 "claude": {"models": {"claude-haiku-5-5": {"efforts": ["high", "xhigh"]}}}}}

When the key is present, a model that is not listed is refused, and so is an effort that is
not in its "efforts" list ({} or no "efforts" allows every effort the engine supports). The
allowlist can only narrow what the engine supports: a model or effort the engine does not
support makes the config invalid, and claive then refuses every launch.

Models come in three tiers. The default runs when no --model is given. The allowed tier is
"models" when present, else the engine's declared catalog, and it runs freely. Everything else
is an extra model: it is refused with the flag and the config edit that allow it, and
--allow-model permits it for one worker. "extra_models" extends the engine's declared catalog,
so a host can adopt a model released after this build without a code change:

    {"engines": {"claude": {"extra_models": {"claude-opus-6": {"efforts": ["high", "xhigh"]}}}}}

`claive models` prints the three tiers; see docs/engine-configuration.md.

"fallback_models" is the ordered list of models tried after a fallbackable failure. A Pi
entry may name "provider/model" to switch provider. The command-line --fallback-models
overrides the list:

    {"engines": {"pi": {"fallback_models": ["opencode2api/deepseek-v4.1-flash-free",
                                            "mistral/deepseek-v4-1-flash-1017"]}}}
"""
from copy import deepcopy
import json
import os
from pathlib import Path
import re

try:
    import yaml
except ImportError:  # YAML is optional; JSON uses only the standard library.
    yaml = None


if yaml is not None:
    class _UniqueKeyLoader(yaml.SafeLoader):
        """SafeLoader that refuses a duplicate mapping key instead of keeping the last one."""

        def construct_mapping(self, node, deep=False):
            if isinstance(node, yaml.MappingNode):
                self.flatten_mapping(node)
            seen = set()
            for key_node, _value in node.value:
                key = self.construct_object(key_node, deep=deep)
                try:
                    duplicate = key in seen
                    seen.add(key)
                except TypeError:
                    continue
                if duplicate:
                    raise yaml.constructor.ConstructorError(
                        None, None, f"duplicate key {key!r}", key_node.start_mark)
            return super().construct_mapping(node, deep=deep)


# Temporary --set overlay for this process; load() merges it over the file.
_OVERRIDES = {}

TOP_LEVEL_KEYS = {"default_engine", "roles", "providers", "workspaces", "engines", "teams", "launch"}
ENGINE_KEYS = {
    "enabled": bool, "reason": str, "default_model": str, "default_model_efforts": dict,
    "disabled_models": (list, dict), "disabled_model_efforts": dict, "models": dict,
    "user_config": bool, "extra_args": list, "budget": (bool, dict),
    "fallback_models": list, "providers": dict, "default_provider": str,
    "binary": str, "provider": str, "env": dict, "prompt_limit": int, "extra_models": dict,
}
# Every engine accepts these; each engine module declares the default it falls back to.
UNIFORM_ENGINE_KEYS = ("binary", "provider", "env", "prompt_limit")
# Engine policy keys that only one engine accepts.
ENGINE_ONLY_KEYS = {"user_config": "codex", "budget": "claude", "default_provider": "pi"}
CLAUDE_BUDGET_DEFAULTS = {
    "default": {"warn": 140000, "stop": 150000, "max_handoffs": 7},
    "claude-haiku-5-5": {"warn": 90000, "stop": 100000, "max_handoffs": 7},
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
    "agy": {"-p", "--print", "--prompt", "--prompt-interactive", "-i", "--input-format", "--output-format",
            "--model", "--effort", "--conversation", "--continue", "-c", "--print-timeout",
            "--dangerously-skip-permissions", "--mode", "--sandbox", "--json-schema", "--project",
            "--new-project", "--add-dir", "--settings"},
}
ROLE_KEYS = {
    "engine": str, "model": str, "reasoning_effort": str, "read_only": bool,
    "max_model_steps": (int, type(None)), "preamble": str, "prompt_file": str,
}
NAME_PATTERN = r"[a-z][a-z0-9_-]{0,31}"


CONFIG_NAMES = ("config.yaml", "config.yml", "config.json")


def config_dir():
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "claive"


def config_files():
    """Config files in discovery order: CLAIVE_CONFIG, else every default name that exists."""
    explicit = os.environ.get("CLAIVE_CONFIG")
    if explicit:
        return [Path(explicit).expanduser()]
    root = config_dir()
    return [root / name for name in CONFIG_NAMES if (root / name).is_file()]


def config_path():
    """The config file in use: CLAIVE_CONFIG, else the first existing default name."""
    found = config_files()
    return found[0] if found else config_dir() / "config.yaml"


def config_format(path):
    """The format named by the suffix: "yaml", "json", or None to decide by content."""
    suffix = Path(path).suffix.lower()
    if suffix in {".yaml", ".yml"}:
        return "yaml"
    if suffix == ".json":
        return "json"
    return None


def parse_text(text, path):
    """Parse config text as YAML or JSON: by suffix, else by the first non-space character."""
    fmt = config_format(path) or ("json" if text.lstrip()[:1] in {"{", "["} else "yaml")
    if fmt == "json":
        return json.loads(text)
    if yaml is None:
        raise ValueError("this is a YAML file but PyYAML is not installed; install it with "
                         "'sudo apt install python3-yaml' or 'uv pip install --system pyyaml', "
                         "or use a JSON config file")
    try:
        return yaml.load(text, Loader=_UniqueKeyLoader)
    except yaml.YAMLError as error:
        raise ValueError(str(error)) from error


def dump_yaml(data):
    """Serialize a config dict as YAML; raise ValueError when PyYAML is missing."""
    if yaml is None:
        raise ValueError("writing YAML needs PyYAML; install it with 'sudo apt install python3-yaml' "
                         "or 'uv pip install --system pyyaml'")
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False)


def deep_merge(base, overlay):
    """Merge overlay over base: nested objects merge, every other value replaces."""
    result = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def set_overrides(*layers):
    """Apply temporary config layers for this process; later layers win."""
    global _OVERRIDES
    merged = {}
    for layer in layers:
        if layer:
            merged = deep_merge(merged, layer)
    _OVERRIDES = merged


def overrides():
    """The temporary config overlay for this process ({} when none)."""
    return deepcopy(_OVERRIDES)


def parse_override(text):
    """Parse one --set value as a YAML or JSON object that overrides the config."""
    try:
        data = parse_text(text, "override.yaml")
    except ValueError as error:
        raise ValueError(f"invalid override: {error}") from error
    if not isinstance(data, dict):
        raise ValueError("an override must be a YAML or JSON object, for example "
                         "'{engines: {pi: {default_model: deepseek-v4-1-flash-1017}}}'")
    return data


def load():
    """Return the validated config dict ({} when no file exists); raise ValueError when it is invalid."""
    path = config_path()
    data = {}
    if path.is_file():
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as error:
            raise ValueError(f"cannot read claive config {path}: {error}") from error
        try:
            data = parse_text(text, path)
        except ValueError as error:
            raise ValueError(f"invalid claive config {path}: {error}") from error
    if data is None:
        data = {}
    if _OVERRIDES and isinstance(data, dict):
        data = deep_merge(data, _OVERRIDES)
    try:
        return validate(data, path)
    except ValueError as error:
        if _OVERRIDES:
            raise ValueError(f"{error} (with --set overrides)") from error
        raise


def load_text(text, path):
    """Parse and validate config text as if it were path; raise ValueError when it is invalid."""
    try:
        data = parse_text(text, path)
    except ValueError as error:
        raise ValueError(f"invalid claive config {path}: {error}") from error
    return validate(data, path)


def validate(data, path):
    """Validate a parsed config dict; raise ValueError when it is invalid."""
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError(f"invalid claive config {path}: must be a YAML or JSON object")
    unknown = sorted(set(data) - TOP_LEVEL_KEYS)
    if unknown:
        raise ValueError(f"invalid claive config {path}: unknown keys {', '.join(unknown)}")
    launch = data.get("launch", {})
    if not isinstance(launch, dict):
        raise ValueError(f"invalid claive config {path}: launch must be an object")
    for name, value in launch.items():
        if name not in {"start_timeout", "git_timeout", "worktree_setup_timeout", "stall_timeout", "idle_ttl"}:
            raise ValueError(f"invalid claive config {path}: launch: unknown key {name}")
        maximum = 2592000 if name == "idle_ttl" else 3600 if name in {"worktree_setup_timeout", "stall_timeout"} else 600
        minimum = 0 if name in {"stall_timeout", "idle_ttl"} else 1
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not minimum <= value <= maximum:
            raise ValueError(f"invalid claive config {path}: launch.{name} must be a number from {minimum} to {maximum}")
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
    if not isinstance(providers, dict):
        raise ValueError(f"invalid claive config {path}: providers must be an object")
    for name, entry in providers.items():
        _validate_provider_name(name, f"invalid claive config {path}: providers.{name}")
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
            owner = ENGINE_ONLY_KEYS.get(key)
            if owner is not None and name != owner:
                raise ValueError(f"{where}.{key} is only for {owner}")
            expected = ENGINE_KEYS.get(key)
            if expected is None:
                if key == "disabled_reasoning_efforts":
                    raise ValueError(f"{where}.disabled_reasoning_efforts is removed; disable an effort for "
                                     f"one model with {where}.disabled_model_efforts: {{<model>: [<effort>]}}")
                if key == "default_reasoning_effort":
                    raise ValueError(f"{where}.default_reasoning_effort is removed; set the effort for one "
                                     f"model with {where}.default_model_efforts: {{<model>: <effort>}}")
                raise ValueError(f"{where}: unknown key {key}")
            if not isinstance(value, expected):
                raise ValueError(f"{where}.{key} has the wrong type")
        if "providers" in policy:
            _validate_provider_policy(policy["providers"], f"{where}.providers")
        if "default_provider" in policy:
            _validate_provider_name(policy["default_provider"], f"{where}.default_provider")
            if _provider_disabled(policy.get("providers"), policy["default_provider"]):
                raise ValueError(f"{where}.default_provider {policy['default_provider']} is disabled "
                                 f"in {where}.providers")
        if "budget" in policy:
            _validate_claude_budget(policy["budget"], f"{where}.budget")
        extra = policy.get("extra_args", [])
        if not all(isinstance(item, str) and item for item in extra):
            raise ValueError(f"{where}.extra_args must hold nonempty strings")
        for item in extra:
            if item == "--" or item.split("=", 1)[0] in OWNED_FLAGS.get(name, set()):
                raise ValueError(f"{where}.extra_args: claive owns {item.split('=', 1)[0]}")
        if "binary" in policy:
            binary = policy["binary"]
            if not binary or not Path(binary).expanduser().is_absolute():
                raise ValueError(f"{where}.binary must be an absolute or ~ path")
        if "provider" in policy:
            provider = policy["provider"]
            if not provider or any(character.isspace() for character in provider):
                raise ValueError(f"{where}.provider must be a nonempty name without spaces")
            if _provider_disabled(policy.get("providers"), provider):
                raise ValueError(f"{where}.provider {provider} is disabled in {where}.providers")
        if "env" in policy:
            for variable, value in policy["env"].items():
                if not ENV_NAME.fullmatch(variable):
                    raise ValueError(f"{where}.env: {variable!r} is not an environment variable name")
                if not isinstance(value, str):
                    raise ValueError(f"{where}.env.{variable} must be a string")
        if "prompt_limit" in policy:
            limit = policy["prompt_limit"]
            if isinstance(limit, bool) or limit < 1:
                raise ValueError(f"{where}.prompt_limit must be a positive integer")
        if "fallback_models" in policy and not all(
                isinstance(item, str) and item for item in policy["fallback_models"]):
            raise ValueError(f"{where}.fallback_models must hold nonempty model names")
        listed = policy.get("disabled_models", [])
        reasons = listed.values() if isinstance(listed, dict) else []
        if not all(isinstance(item, str) and item for item in listed) or not all(
                isinstance(reason, str) for reason in reasons):
            raise ValueError(f"{where}.disabled_models must hold nonempty names (with string reasons)")
        if "default_model" in policy and (not policy["default_model"]
                                          or _disabled(listed, policy["default_model"]) is not None):
            raise ValueError(f"{where}.default_model must be a nonempty name that disabled_models does not disable")
        disabled_efforts = policy.get("disabled_model_efforts", {})
        for model, entry in disabled_efforts.items():
            names = entry if isinstance(entry, list) else list(entry) if isinstance(entry, dict) else None
            reasons = entry.values() if isinstance(entry, dict) else []
            if (not isinstance(model, str) or not model or names is None
                    or not all(isinstance(item, str) and item for item in names)
                    or not all(isinstance(reason, str) for reason in reasons)):
                raise ValueError(f"{where}.disabled_model_efforts.{model} must be a list of effort names "
                                 "or an object mapping each effort to a reason")
        for model, effort in policy.get("default_model_efforts", {}).items():
            if not isinstance(model, str) or not model or not isinstance(effort, str) or not effort:
                raise ValueError(f"{where}.default_model_efforts must map a nonempty model name "
                                 "to a nonempty effort")
            if _disabled(disabled_efforts.get(model, []), effort) is not None:
                raise ValueError(f"{where}.default_model_efforts.{model} {effort} is disabled in "
                                 f"{where}.disabled_model_efforts")
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
        extra_models = policy.get("extra_models")
        if extra_models is not None:
            for model, entry in extra_models.items():
                efforts = entry.get("efforts", []) if isinstance(entry, dict) else None
                if (not model or not isinstance(entry, dict) or set(entry) - {"efforts"}
                        or not isinstance(efforts, list)
                        or not all(isinstance(item, str) and item for item in efforts)):
                    raise ValueError(f"{where}.extra_models.{model} must be {{\"efforts\": [nonempty names]}}")
                if models is not None and model in models:
                    raise ValueError(f"{where}.extra_models.{model} is already in engines.{name}.models")
    if engine is not None and engines.get(engine, {}).get("enabled") is False:
        raise ValueError(f"invalid claive config {path}: default_engine {engine} is disabled in engines.{engine}")
    _validate_teams(data.get("teams", {}), path, set(BUILTIN_ROLES) | set(roles))
    return data


def launch_setting(name, default):
    return load().get("launch", {}).get(name, default)


def _claude_budget_values(model, budget):
    models = budget.get("models", {}) if isinstance(budget, dict) else {}
    values = dict(CLAUDE_BUDGET_DEFAULTS["default"])
    values.update(CLAUDE_BUDGET_DEFAULTS.get(model, {}))
    values.update(models.get("default", {}))
    values.update(models.get(model, {}))
    return values


def _validate_claude_budget(budget, where):
    if isinstance(budget, bool):
        return
    unknown = sorted(set(budget) - {"enabled", "models"})
    if unknown:
        raise ValueError(f"{where}: unknown keys {', '.join(unknown)}")
    if not isinstance(budget.get("enabled", True), bool):
        raise ValueError(f"{where}.enabled must be a boolean")
    models = budget.get("models", {})
    if not isinstance(models, dict):
        raise ValueError(f"{where}.models must be an object")
    for model, entry in models.items():
        entry_where = f"{where}.models.{model}"
        if not isinstance(model, str) or not model or not isinstance(entry, dict):
            raise ValueError(f"{entry_where} must be an object for a nonempty model name")
        unknown = sorted(set(entry) - {"warn", "stop", "max_handoffs"})
        if unknown:
            raise ValueError(f"{entry_where}: unknown keys {', '.join(unknown)}")
    for model in set(CLAUDE_BUDGET_DEFAULTS) | set(models):
        entry_where = f"{where}.models.{model}"
        values = _claude_budget_values(model, budget)
        for key in ("warn", "stop"):
            value = values[key]
            if type(value) is not int or value % 1000:
                raise ValueError(f"{entry_where}.{key} must be an integer and a multiple of 1000")
        if values["warn"] >= values["stop"]:
            raise ValueError(f"{entry_where}.warn must be less than stop")
        if not 47000 <= values["stop"] <= 942000:
            raise ValueError(f"{entry_where}.stop must be between 47000 and 942000")
        maximum = values["max_handoffs"]
        if maximum is not None and (type(maximum) is not int or maximum < 1):
            raise ValueError(f"{entry_where}.max_handoffs must be an integer >= 1 or null")


def claude_budget(model, data=None):
    """Resolve the launch budget once, merging each model over the built-in values."""
    if data is None:
        data = load()
    budget = data.get("engines", {}).get("claude", {}).get("budget", True)
    if budget is False or (isinstance(budget, dict) and budget.get("enabled") is False):
        return None, "config"
    values = _claude_budget_values(model, budget)
    from claivelib.engines.claude import budget_margin
    values["compact"] = values["stop"] + budget_margin(model)
    return values, None


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


PROVIDER_NAME = re.compile(r"[a-z][a-z0-9_.-]{0,63}")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _validate_provider_name(name, where):
    if not isinstance(name, str) or not PROVIDER_NAME.fullmatch(name):
        raise ValueError(f"{where}: provider name must be 1-64 lowercase letters, digits, "
                         "'_', '.' or '-', starting with a letter")
    return name


def _validate_provider_policy(providers, where):
    """Validate engines.<name>.providers: {name: {enabled: bool, reason: str}}."""
    if not isinstance(providers, dict):
        raise ValueError(f"{where} must be an object")
    for name, entry in providers.items():
        _validate_provider_name(name, f"{where}.{name}")
        if not isinstance(entry, dict):
            raise ValueError(f"{where}.{name} must be an object")
        unknown = sorted(set(entry) - {"enabled", "reason"})
        if unknown:
            raise ValueError(f"{where}.{name}: unknown key {unknown[0]}")
        if "enabled" in entry and not isinstance(entry["enabled"], bool):
            raise ValueError(f"{where}.{name}.enabled must be true or false")
        if "reason" in entry and not isinstance(entry["reason"], str):
            raise ValueError(f"{where}.{name}.reason must be a string")


def _provider_disabled(providers, name):
    """Whether a providers block disables this label."""
    return ((providers or {}).get(name) or {}).get("enabled") is False


def provider_policy(name, provider):
    """The user's policy for one provider label on one engine ({} when it has none)."""
    return (engine_policy(name).get("providers") or {}).get(provider, {})


def check_provider(name, provider):
    """Refuse a provider label the user disabled on this engine, with the configured reason."""
    policy = provider_policy(name, provider)
    if policy.get("enabled") is False:
        reason = policy.get("reason", "")
        raise ValueError(f"provider {provider} on worker engine {name} is explicitly disabled by the user "
                         f"in {config_path()}" + (f" (reason: {reason})" if reason else "")
                         + "; do not retry or work around this, choose an enabled provider or ask the user")


def pi_default_provider():
    """The provider a bare Pi model resolves to: policy, then Pi's settings, then opencode2api."""
    configured = engine_policy("pi").get("default_provider")
    if configured:
        return configured
    from claivelib import provider
    try:
        settings = json.loads((provider.agent_dir() / "settings.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return provider.PROVIDER
    remembered = settings.get("defaultProvider") if isinstance(settings, dict) else None
    return remembered if isinstance(remembered, str) and remembered else provider.PROVIDER


def engine_policy(name):
    """The user's policy for one engine ({} when it has none)."""
    return load().get("engines", {}).get(name, {})


def engine_setting(name, key, default=None):
    """The configured value for one engine key, else the engine's declared default."""
    value = engine_policy(name).get(key)
    return default if value is None else value


def engine_env(name):
    """Extra child environment from engines.<name>.env ({} when unset)."""
    return dict(engine_policy(name).get("env") or {})


def engine_effective(name, engine):
    """Every effective engine setting with its source: "config" or "default"."""
    policy = engine_policy(name)
    result = {}
    for key, default in (engine.defaults() if engine is not None else {}).items():
        result[key] = ({"value": policy[key], "source": "config"} if key in policy
                       else {"value": default, "source": "default"})
    for key, value in policy.items():
        if key not in result:
            result[key] = {"value": value, "source": "config"}
    return result


def model_disabled(name, model):
    """Whether the user disabled this model on the engine (or the engine itself), or left it off the allowlist."""
    policy = engine_policy(name)
    models = policy.get("models")
    return (policy.get("enabled") is False or _disabled(policy.get("disabled_models", []), model) is not None
            or (models is not None and model not in models))


def disabled_efforts(name, model):
    """The efforts the user disabled for one model: a list or {effort: reason} (empty when none)."""
    return (engine_policy(name).get("disabled_model_efforts") or {}).get(model, [])


def effort_disabled(name, model, effort):
    """The reason (possibly empty) when this effort is disabled for this model, else None."""
    return _disabled(disabled_efforts(name, model), effort)


def check_engine(name, model=None, reasoning_effort=None, allow=()):
    """Refuse an engine, model or reasoning effort the user disabled in the claive config."""
    policy = engine_policy(name)
    refused = None
    if policy.get("enabled") is False:
        refused = f"worker engine {name}", policy.get("reason", "")
    if refused is None and model:
        reason = _disabled(policy.get("disabled_models", []), model)
        if reason is not None:
            refused = f"model {model} on worker engine {name}", reason
    if refused is None and model and reasoning_effort:
        reason = effort_disabled(name, model, reasoning_effort)
        if reason is not None:
            refused = (f"reasoning effort {reasoning_effort} for model {model} on worker engine {name}", reason)
    if refused is None:
        _check_allowed(name, policy.get("models"), model, reasoning_effort, allow)
        return
    subject, reason = refused
    raise ValueError(f"{subject} is explicitly disabled by the user in {config_path()}"
                     + (f" (reason: {reason})" if reason else "")
                     + "; do not retry or work around this, choose an enabled alternative or ask the user")


def _check_allowed(name, models, model, reasoning_effort, allow=()):
    """Refuse a model outside the allowed tier, naming the flag and the config edit that allow it.

    The allowed tier is the models allowlist when the config has one, else the engine's
    declared catalog. Anything else is an extra model: it needs an explicit opt-in.
    """
    if not model or model in allow:
        return
    allowed = models if models is not None else engine_catalog(name)
    if allowed is not None and model not in allowed:
        known = ", ".join(allowed) or "none"
        if models is not None:
            raise ValueError(f"model {model} on worker engine {name} is not in the models allowlist in "
                             f"{config_path()} (allowed: {known}); pass --allow-model {model} to run it "
                             f"once, or add it to engines.{name}.models to allow it permanently")
        raise ValueError(f"model {model} is not in the {name} catalog in {config_path()} (known: {known}); "
                         f"pass --allow-model {model} to run it once, or add it to "
                         f"engines.{name}.extra_models to allow it permanently")
    efforts = (models or {}).get(model, {}).get("efforts") or []
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


def supported_models(name, engine):
    """The engine's declared catalog extended by engines.<name>.extra_models.

    None means the engine takes any model; extra_models cannot close an open catalog.
    """
    builtin = engine.supported_models()
    if builtin is None:
        return None
    merged = {model: list(efforts) for model, efforts in builtin.items()}
    for model, entry in (engine_policy(name).get("extra_models") or {}).items():
        merged[model] = list(entry.get("efforts") or [])
    return merged


def engine_catalog(name):
    """supported_models for one engine by name (None when the engine takes any model)."""
    from claivelib.engines import get_engine
    return supported_models(name, get_engine(name))


def model_tiers(name, engine):
    """The three model tiers: the default, the allowed set, and the extra set.

    allowed is the config allowlist when present, else the engine's declared catalog.
    extra is what the catalog knows but the policy does not allow. An open catalog has
    no extra tier, because there is nothing to exclude.
    """
    policy = engine_policy(name)
    catalog = supported_models(name, engine)
    allowlist = policy.get("models")
    allowed = list(allowlist) if allowlist is not None else (None if catalog is None else list(catalog))
    extra = [] if catalog is None or allowed is None else [model for model in catalog if model not in allowed]
    return {"default": policy.get("default_model") or getattr(engine, "default_model", None),
            "allowed": allowed, "extra": extra}


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
            from claivelib import worktrees
            source = worktrees.managed_source(real)
            if source:
                real = Path(source).resolve()
                matches = [(base, write) for base, write in entries if real == base or base in real.parents]
        if not matches:
            raise ValueError(f"workspace {real} is outside the claive config workspaces allowlist")
        _base, write = max(matches, key=lambda match: len(match[0].parts))
        if not write and not read_only:
            raise ValueError(f"workspace {real} is read-only in the claive config workspaces allowlist; "
                             "select enforced read-only or use a writable workspace entry")


def default_engine(builtin):
    """Engine for new workers that name neither --engine nor a role."""
    override = os.environ.get("CLAIVE_ENGINE")
    if override:
        return override
    return load().get("default_engine") or builtin


def role_overrides(data=None):
    return (data if data is not None else load()).get("roles", {})


def config_target():
    """Where `config init` and `config migrate` write: CLAIVE_CONFIG, else config.yaml."""
    explicit = os.environ.get("CLAIVE_CONFIG")
    return Path(explicit).expanduser() if explicit else config_dir() / "config.yaml"


TEMPLATE = """\
# claive configuration. Every key is listed; a commented key keeps its default.
# Validate with `claive config check`. YAML reads on, off, yes and no as booleans,
# so quote a value that must stay a string.

# Engine for new workers that name neither --engine nor a role.
# default_engine: pi

# Per-engine policy. Every engine accepts the same keys; four are engine-specific:
# codex user_config, claude budget, pi providers and pi default_provider.
engines:
  pi:
    enabled: true
    # binary: ~/.local/bin/pi
    # provider: mistral
    # default_provider: mistral
    # default_model: deepseek-v4-1-flash-1017
    # default_model_efforts: {deepseek-v4-1-flash-1017: max}
    # fallback_models: [opencode2api/deepseek-v4.1-flash-free, mistral/deepseek-v4-1-flash-1017]
    # env: {ANTHROPIC_LOG: debug}
    # prompt_limit: 200000
    # models:                      # allowlist: narrows the engine's catalog
    #   deepseek-v4-1-flash-1017: {efforts: [max]}
    # extra_models: {}             # extend the catalog without a code change
    # disabled_models: {space-bunny-free: too slow}
    # disabled_model_efforts: {deepseek-v4-1-flash-1017: [max]}
    # extra_args: []
    # providers:                   # enable or disable a provider label
    #   mistral: {enabled: true}
    #   opencode2api: {enabled: false, reason: gateway retired}
  claude:
    enabled: true
    # provider: anthropic
    # budget: {enabled: true, models: {default: {warn: 140000, stop: 150000}}}
  codex:
    enabled: true
    # user_config: false
  agy:
    enabled: true
    # prompt_limit: 100000
  muse:
    enabled: true

# Role overrides. A new role also needs engine, read_only and preamble or prompt_file.
# roles:
#   worker: {engine: pi, model: deepseek-v4-1-flash-1017, reasoning_effort: max}
#   oracle: {engine: pi, model: deepseek-v4-1-flash-1017, reasoning_effort: max}

# Redirect a provider Pi already knows to a loopback endpoint.
# providers:
#   opencode2api: {base_url: "http://127.0.0.1:8080/v1"}

# Workspace allowlist. Without the key, any workspace is allowed.
# workspaces:
#   - {path: ~/repo, write: true}
#   - {path: ~/notes}

# Launch teams.
# teams:
#   review:
#     members: {reader: oracle, writer: worker}

# Launch timeouts in seconds.
# launch: {start_timeout: 60, git_timeout: 30, worktree_setup_timeout: 120}
"""
