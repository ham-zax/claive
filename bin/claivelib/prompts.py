"""Prompt layer validation and first-turn composition."""
import hashlib
from pathlib import Path
import stat


MAX_LAYER_BYTES = 256 * 1024
MAX_CONTEXT_FILES = 10


def read_layer_file(path, label):
    """Read one bounded UTF-8 layer and return its resolved path, text and bytes."""
    source = Path(path)
    invalid = f"{label} must be an existing nonempty absolute file: {path}"
    if not source.is_absolute():
        raise ValueError(invalid)
    try:
        metadata = source.stat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size == 0:
            raise ValueError(invalid)
        if metadata.st_size > MAX_LAYER_BYTES:
            raise ValueError(f"{label} {path} is larger than 256 KiB")
        data = source.read_bytes()
    except OSError as error:
        raise ValueError(invalid) from error
    if not data:
        raise ValueError(invalid)
    if len(data) > MAX_LAYER_BYTES:
        raise ValueError(f"{label} {path} is larger than 256 KiB")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"{label} {path} is not UTF-8 text") from error
    return source.resolve(), text, data


def read_role_prompt(role, path):
    """Read a role prompt file with the single launch-time error required by the CLI contract."""
    try:
        _resolved, text, _data = read_layer_file(path, f"role {role}: prompt_file")
    except (OSError, ValueError) as error:
        raise ValueError(f"role {role}: prompt_file {path} is missing, empty, not UTF-8 or larger than 256 KiB") from error
    return text


def read_context_files(paths):
    """Read context layers in argument order and return their persisted fingerprints."""
    paths = list(paths or [])
    if len(paths) > MAX_CONTEXT_FILES:
        raise ValueError("at most 10 --context-file values")
    layers = []
    records = []
    seen = set()
    for path in paths:
        resolved, text, data = read_layer_file(path, "--context-file")
        if resolved in seen:
            raise ValueError(f"--context-file {path} is given twice")
        seen.add(resolved)
        layers.append(f"[context: {Path(path).name}]\n{text}")
        records.append(dict(path=str(resolved), sha256=hashlib.sha256(data).hexdigest(), bytes=len(data)))
    return layers, records


def compose_first_prompt(body=None, *, body_file=None, role_prompt=None, team_header=None,
                         collaboration=None, project=None, context_layers=(), report_contract=None):
    """Compose ordered first-turn layers, returning (text, has_non_body_layer)."""
    before_body = [role_prompt, team_header, collaboration, project, *context_layers]
    extras = [layer for layer in before_body if layer not in (None, "")]
    after_body = [report_contract] if report_contract not in (None, "") else []
    has_layers = bool(extras or after_body)
    if not has_layers:
        return None, False
    if body is None:
        if body_file is None:
            raise ValueError("a task body is required to compose the first prompt")
        body = Path(body_file).read_text(encoding="utf-8")
    layers = [*extras, body, *after_body]
    return "\n\n".join(layer.rstrip("\n") for layer in layers if layer != ""), True
