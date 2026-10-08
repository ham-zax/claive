"""Persistent names for workers; bindings never launch or resume a session."""
import fcntl
import json
import os
import re
import sys


def validate_name(name):
    if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", name or ""):
        raise ValueError("alias name must be 1-64 lowercase letters, digits, _, . or -, starting with a letter")
    return name


def _valid(name, worker):
    try:
        validate_name(name)
    except ValueError:
        return False
    return isinstance(worker, str) and re.fullmatch(r"[0-9a-f]{12}", worker) is not None


def _read(path):
    """Return (valid entries, invalid names); one bad entry never hides the rest."""
    if not path.exists():
        return {}, []
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError("invalid worker aliases registry: expected a JSON object")
    good = {name: worker for name, worker in data.items() if _valid(name, worker)}
    return good, sorted(set(data) - set(good))


def resolve(reference):
    from claivelib import cli
    name = validate_name(reference[1:])
    worker = _read(cli.root() / "aliases.json")[0].get(name)
    if worker is None:
        raise ValueError(f"unknown worker alias: @{name}")
    return worker


def update(name, worker=None, replace=False):
    from claivelib import cli
    validate_name(name)
    # Resolve before taking the lock, and store only a canonical worker ID.
    target = cli.job_path(worker).name if worker is not None else None
    path = cli.root() / "aliases.json"
    fd = os.open(path.with_suffix(".lock"), os.O_WRONLY | os.O_CREAT, 0o600)
    with os.fdopen(fd, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data, dropped = _read(path)
        if dropped:
            print(f"warning: dropped invalid alias entries: {', '.join(dropped)}", file=sys.stderr)
        if target is None:
            if name not in data:
                raise ValueError(f"unknown worker alias: @{name}")
            del data[name]
        else:
            existing = data.get(name)
            if existing and existing != target and not replace:
                raise ValueError(f"@{name} already names worker {existing}; use --replace to rebind it")
            data[name] = target
        cli.save(path, data)
    print(f"@{name} -> {target}" if target else f"Removed worker alias @{name}")
    return 0


def listing(json_output=False):
    from claivelib import cli
    records = []
    valid, dropped = _read(cli.root() / "aliases.json")
    if dropped:
        print(f"warning: ignoring invalid alias entries: {', '.join(dropped)}; "
              "any alias set or remove rewrites the registry without them", file=sys.stderr)
    for name, worker in sorted(valid.items()):
        item = dict(name=name, reference=f"@{name}", id=worker, status="missing")
        try:
            state = cli.load(cli.job_path(worker))
        except (ValueError, OSError, KeyError):
            pass
        else:
            item.update(status=state["status"], workspace=state["workspace"],
                        session_id=cli.worker_session_id(state), label=state.get("label", ""))
        records.append(item)
    if json_output:
        print(json.dumps(records, indent=2))
    elif records:
        for item in records:
            print(f"{item['reference']} -> {item['id']} | {item['status']} | "
                  f"{cli.clean(item.get('workspace', ''))}")
    else:
        print("No worker aliases.")
    return 0
