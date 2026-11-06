"""Pi's provider configuration as Claive sees it: models.json, discovery, and a safe writer.

Pi owns provider definitions, its built-in catalog, and credentials. Claive reads them for
policy and reporting, and writes models.json only through the narrow, additive helpers here.
A host whose provider runs locally can set providers.<name>.base_url in the claive config to a
loopback URL. Pi turns launched by claive then use an overlay agent directory: every entry of
the real one linked in, plus a private models.json that differs only in those baseUrls. The real
models.json is never edited by the override path. Doctor reports the override, so it is never
applied silently.
"""
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request

from claivelib import config

PROVIDER = "opencode2api"
FALLBACK_MODEL = "muse-spark-1.3-contributor-free"
SOURCE_MARKER = ".claive-source"  # in the overlay: the real agent dir it mirrors
NAME_PATTERN = re.compile(r"[a-z][a-z0-9_.-]{0,63}")
# Wire protocols Pi implements. A provider entry must name one of these.
APIS = ("openai-completions", "openai-responses", "anthropic-messages",
        "google-generative-ai", "mistral-conversations")


def agent_dir():
    return Path(os.environ.get("PI_CODING_AGENT_DIR", str(Path.home() / ".pi/agent"))).expanduser()


def models_file():
    return agent_dir() / "models.json"


def _load_models(path):
    if not path.is_file():
        raise ValueError(f"missing {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise ValueError(f"invalid models.json: {error}") from error
    providers = data.get("providers") if isinstance(data, dict) else None
    if not isinstance(providers, dict):
        raise ValueError(f"invalid models.json: providers must be an object in {path}")
    return data


def read_models(directory=None, name=PROVIDER):
    """Return (models.json data, provider entry); raise ValueError when either is unusable."""
    path = Path(directory) / "models.json" if directory else models_file()
    data = _load_models(path)
    entry = data["providers"].get(name)
    if not isinstance(entry, dict):
        raise ValueError(f"provider {name} not configured in {path}")
    if not entry.get("baseUrl"):
        raise ValueError(f"provider {name}.baseUrl not configured")
    return data, entry


def provider_names(directory=None):
    """Provider ids defined in models.json, sorted; empty when the file is absent."""
    path = Path(directory) / "models.json" if directory else models_file()
    if not path.is_file():
        return []
    return sorted(_load_models(path)["providers"])


def listed_models(entry):
    models = entry.get("models")
    ids = [item.get("id") if isinstance(item, dict) else item for item in models] if isinstance(models, list) else []
    return [item for item in ids if isinstance(item, str)]


def check_loopback(url):
    """Accept only http(s) URLs whose host is localhost or a loopback address."""
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ""
    if parsed.scheme not in {"http", "https"} or not host:
        raise ValueError("must be an http(s) URL with a host")
    if host != "localhost":
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            raise ValueError("host must be localhost or a loopback address")
    return url


def overrides():
    """Every configured loopback base URL, as {provider: url}."""
    providers = config.load().get("providers") or {}
    return {name: entry["base_url"] for name, entry in providers.items()
            if isinstance(entry, dict) and entry.get("base_url")}


def override_url(name=PROVIDER):
    """The configured loopback base URL for one provider, or None."""
    return overrides().get(name)


def overlay_agent_dir(state_root, names=None):
    """Build the overlay agent directory when an override applies; return its path or None."""
    wanted = overrides()
    if names is not None:
        wanted = {name: url for name, url in wanted.items() if name in names}
    if not wanted:
        return None
    overlay = Path(state_root) / "pi-agent-overlay"
    source = overlay / SOURCE_MARKER
    real = agent_dir().resolve()
    if overlay.exists() and real == overlay.resolve():
        # Run from inside a Pi worker, whose agent dir already is this overlay: relinking it
        # into itself would replace every link with a self-loop and then delete them all.
        try:
            real = Path(source.read_text().strip()).resolve()
        except OSError as error:
            raise ValueError(f"Pi agent dir is the claive overlay but {source} is unreadable") from error
        if real == overlay.resolve():
            raise ValueError(f"{source} points at the overlay itself")
    data = _load_models(real / "models.json")
    applied = {name: url for name, url in wanted.items() if isinstance(data["providers"].get(name), dict)}
    if not applied:
        return None
    overlay.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not source.is_file() or source.read_text().strip() != str(real):
        source.write_text(str(real) + "\n")
    for child in real.iterdir():
        if child.name in {"models.json", SOURCE_MARKER}:
            continue
        link = overlay / child.name
        if link.is_symlink() and os.readlink(link) == str(child):
            continue
        if link.is_symlink() or link.is_file():
            link.unlink()
        elif link.exists():
            continue  # a directory Pi created in the overlay; leave it alone
        link.symlink_to(child)
    for link in overlay.iterdir():
        if link.is_symlink() and not link.exists():
            link.unlink()
    providers = dict(data["providers"])
    for name, url in applied.items():
        providers[name] = dict(providers[name], baseUrl=url)
    text = json.dumps(dict(data, providers=providers), indent=2) + "\n"
    target = overlay / "models.json"
    if not target.is_file() or target.read_text() != text:
        fd, temporary = tempfile.mkstemp(prefix=".models-", dir=overlay)
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(text)
            os.chmod(temporary, 0o600)
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return str(overlay)


def probe(base_url, entry, timeout=3):
    """GET {base_url}/models. Return dict(ok, ms, ids, error); never includes the key."""
    headers = {}
    api_key = entry.get("apiKey")
    if isinstance(api_key, str) and api_key.startswith("$"):
        api_key = os.environ.get(api_key.strip("${}"))
    if isinstance(api_key, str) and api_key and not api_key.startswith("!"):
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(str(base_url).rstrip("/") + "/models", headers=headers)
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read())
    except ValueError:
        return dict(ok=False, ms=None, ids=[], error="GET /models did not return JSON")
    except Exception as error:
        return dict(ok=False, ms=None, ids=[], error=f"GET /models failed: {error}"[:200])
    ms = round((time.monotonic() - started) * 1000)
    items = payload if isinstance(payload, list) else (
        payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), list) else
        payload.get("models") if isinstance(payload, dict) and isinstance(payload.get("models"), list) else [])
    ids = [item if isinstance(item, str) else item.get("id") if isinstance(item, dict) else None for item in items]
    return dict(ok=True, ms=ms, ids=[item for item in ids if isinstance(item, str)], error=None)


def binary():
    """The Pi executable Claive would launch, for discovery commands."""
    return os.environ.get("PI_WORKER_BINARY", str(Path.home() / ".local/bin/pi"))


def catalog_models(binary_path=None, timeout=30):
    """Parse `pi --list-models` into {provider: [model ids]}.

    Pi has no JSON form for this table, so the parse is defensive: an unrecognized shape
    returns {} and reporting degrades. It never raises and never blocks a turn.
    """
    executable = binary_path or binary()
    try:
        completed = subprocess.run([executable, "--list-models"], capture_output=True, text=True,
                                   timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return {}
    if completed.returncode:
        return {}
    lines = completed.stdout.splitlines()
    if not lines or not lines[0].split()[:1] == ["provider"]:
        return {}
    catalog = {}
    for line in lines[1:]:
        fields = line.split()
        if len(fields) < 2:
            continue
        provider, model = fields[0], fields[1]
        if not NAME_PATTERN.fullmatch(provider):
            continue
        catalog.setdefault(provider, []).append(model)
    return catalog


def auth_check(name, binary_path=None, timeout=30):
    """`pi auth check --provider NAME --json` as a dict, or None when Pi cannot answer."""
    executable = binary_path or binary()
    try:
        completed = subprocess.run([executable, "auth", "check", "--provider", name, "--json"],
                                   capture_output=True, text=True, timeout=timeout,
                                   stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        payload = json.loads(completed.stdout)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def list_providers(binary_path=None):
    """Every provider Pi knows, with its source, models and readiness.

    models.json is authoritative for providers it defines. The built-in catalog fills in
    the rest. Readiness comes from `pi auth check`; None means Pi could not answer.
    """
    defined = {}
    try:
        data = _load_models(models_file())
        defined = {name: entry for name, entry in data["providers"].items() if isinstance(entry, dict)}
    except ValueError:
        defined = {}
    catalog = catalog_models(binary_path)
    rows = []
    for name in sorted(set(defined) | set(catalog)):
        entry = defined.get(name) or {}
        models = listed_models(entry) or catalog.get(name, [])
        auth = auth_check(name, binary_path)
        rows.append(dict(
            provider=name,
            source="models.json" if name in defined else "built-in",
            base_url=entry.get("baseUrl"),
            api=entry.get("api"),
            models=models,
            ready=None if auth is None else auth.get("status") == "ready",
            auth_type=None if auth is None else auth.get("authType"),
        ))
    return rows


def _validate_name(name):
    if not isinstance(name, str) or not NAME_PATTERN.fullmatch(name):
        raise ValueError("provider name must be 1-64 lowercase letters, digits, '_', '.' or '-', starting with a letter")
    return name


def write_models(data, path=None):
    """Replace models.json atomically, mode 0600, after keeping one timestamped backup."""
    target = Path(path) if path else models_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2) + "\n"
    if target.is_file():
        stamp, number = int(time.time()), 0
        backup = target.with_name(f"{target.name}.bak-{stamp}")
        while backup.exists():
            number += 1
            backup = target.with_name(f"{target.name}.bak-{stamp}-{number}")
        shutil.copy2(target, backup)
        os.chmod(backup, 0o600)
    fd, temporary = tempfile.mkstemp(prefix=".models-", dir=target.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return target


def add_provider(name, base_url, api, models, api_key_env=None, display_name=None,
                 replace=False, path=None):
    """Add or replace one provider entry in models.json. Never touches credentials."""
    _validate_name(name)
    if api not in APIS:
        raise ValueError(f"unsupported provider api: {api}; choose {', '.join(APIS)}")
    parsed = urllib.parse.urlsplit(base_url or "")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("--base-url must be an http(s) URL with a host")
    ids = [item for item in (models or []) if isinstance(item, str) and item.strip()]
    if not ids:
        raise ValueError("a provider needs at least one --model")
    target = Path(path) if path else models_file()
    data = _load_models(target) if target.is_file() else {"providers": {}}
    providers = data["providers"]
    if name in providers and not replace:
        raise ValueError(f"provider {name} already exists in {target}; use --replace to overwrite it")
    entry = {"baseUrl": base_url, "api": api}
    if display_name:
        entry["name"] = display_name
    if api_key_env:
        entry["apiKey"] = f"${api_key_env}"
    entry["models"] = [{"id": model} for model in ids]
    providers[name] = entry
    write_models(data, target)
    return entry


def remove_provider(name, path=None):
    """Remove one provider entry from models.json.

    Returns True when Pi still serves the provider from its built-in catalog, so the caller
    can say so instead of implying the provider is gone.
    """
    _validate_name(name)
    target = Path(path) if path else models_file()
    data = _load_models(target)
    if name not in data["providers"]:
        raise ValueError(f"provider {name} is not defined in {target}")
    del data["providers"][name]
    write_models(data, target)
    return name in catalog_models()
