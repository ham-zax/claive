"""The opencode2api provider as Pi sees it: models.json, a loopback override and endpoint probes.

Pi reads the provider's baseUrl only from models.json in its agent directory. A host whose provider
runs locally (the ARM server reaches its own opencode2api through a public sslip.io URL) can set
providers.opencode2api.base_url in the claive config to a loopback URL. Pi turns launched by claive
then use an overlay agent directory: every entry of the real one linked in, plus a private
models.json that differs only in baseUrl. The real models.json is never edited. Doctor reports the
override, so it is never applied silently.
"""
import ipaddress
import json
import os
from pathlib import Path
import tempfile
import time
import urllib.parse
import urllib.request

from claivelib import config

PROVIDER = "opencode2api"
FALLBACK_MODEL = "muse-spark-1.3-contributor-free"


def agent_dir():
    return Path(os.environ.get("PI_CODING_AGENT_DIR", str(Path.home() / ".pi/agent"))).expanduser()


def models_file():
    return agent_dir() / "models.json"


def read_models():
    """Return (models.json data, provider entry); raise ValueError when either is unusable."""
    path = models_file()
    if not path.is_file():
        raise ValueError(f"missing {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise ValueError(f"invalid models.json: {error}") from error
    providers = data.get("providers") if isinstance(data, dict) else None
    entry = providers.get(PROVIDER) if isinstance(providers, dict) else None
    if not isinstance(entry, dict):
        raise ValueError(f"provider {PROVIDER} not configured in {path}")
    if not entry.get("baseUrl"):
        raise ValueError(f"provider {PROVIDER}.baseUrl not configured")
    return data, entry


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


def override_url():
    """The configured loopback base URL for opencode2api, or None."""
    return config.load().get("providers", {}).get(PROVIDER, {}).get("base_url")


def overlay_agent_dir(state_root):
    """Build the overlay agent directory when an override is configured; return its path or None."""
    url = override_url()
    if not url:
        return None
    data, entry = read_models()
    real = agent_dir().resolve()
    overlay = Path(state_root) / "pi-agent-overlay"
    overlay.mkdir(mode=0o700, parents=True, exist_ok=True)
    for child in real.iterdir():
        if child.name == "models.json":
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
    entry = dict(entry, baseUrl=url)
    data = dict(data, providers=dict(data["providers"], **{PROVIDER: entry}))
    text = json.dumps(data, indent=2) + "\n"
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
