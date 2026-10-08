"""Durable missions: goals with linked workers and batches."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import time
import uuid

from claivelib import cli as cli_mod


def missions_dir():
    path = cli_mod.root() / "missions"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def mission_path(mission_id):
    if not re.fullmatch(r"[0-9a-f]{12}", mission_id or ""):
        raise ValueError(f"unknown mission: {mission_id}")
    path = missions_dir() / mission_id
    if not (path / "mission.json").is_file():
        raise ValueError(f"unknown mission: {mission_id}")
    return path


def load_mission(mission_id):
    return json.loads((mission_path(mission_id) / "mission.json").read_text())


def save_mission(mission_id, data):
    cli_mod.save(missions_dir() / mission_id / "mission.json", data)


@contextmanager
def updating(mission_id):
    """Serialise read-modify-write so parallel launches never drop each other's links."""
    path = mission_path(mission_id)
    with (path / ".lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = load_mission(mission_id)
        yield data
        save_mission(mission_id, data)


def require_open(mission_id):
    data = load_mission(mission_id)
    if data.get("status") != "open":
        raise ValueError(f"mission is closed: {mission_id}")
    return data


def resolve_mission(explicit=None):
    value = explicit or os.environ.get("CLAIVE_MISSION") or None
    return value or None


def link_auto(mission_id, kind, target_id):
    _add_link(mission_id, kind, target_id, require_open=True)


def _add_link(mission_id, kind, target_id, require_open=False):
    with updating(mission_id) as data:
        # Checked under the lock so a concurrent close cannot slip in between check and link.
        if require_open and data.get("status") != "open":
            raise ValueError(f"mission is closed: {mission_id}")
        if not any(link.get("kind") == kind and link.get("id") == target_id for link in data["links"]):
            data["links"].append(dict(kind=kind, id=target_id, at=time.time()))


def _worker_live(worker_id):
    try:
        state = cli_mod.load(cli_mod.job_path(worker_id))
    except (ValueError, OSError, KeyError):
        return dict(status="unknown", code=None)
    code = cli_mod.outcome_code(state)
    return dict(status=state.get("status"), code=code,
                needs_parent=state.get("needs_parent"),
                question=(state.get("needs_parent") or {}).get("question"))


def _batch_live(batch_id):
    from claivelib import batch as batch_mod
    try:
        state = batch_mod.load_state(batch_id)
    except (ValueError, OSError, KeyError):
        return dict(status="unknown", code=None)
    return dict(status=state.get("status"), code=state.get("code"))


def _worker_running(worker_id):
    try:
        path = cli_mod.job_path(worker_id)
        state = cli_mod.load(path)
    except (ValueError, OSError, KeyError):
        return False
    if state["status"] not in cli_mod.ACTIVE:
        return False
    if state["status"] == "idle" and not list((path / "requests").glob("*.json")):
        return False
    return True


def compute_next(data):
    from claivelib import batch as batch_mod
    for link in data.get("links", []):
        if link.get("kind") == "worker":
            try:
                state = cli_mod.load(cli_mod.job_path(link["id"]))
            except (ValueError, OSError, KeyError):
                continue
            needs = state.get("needs_parent")
            if needs:
                return f"answer {link['id']}: {needs.get('question', '')}"
        elif link.get("kind") == "batch":
            try:
                state = batch_mod.load_state(link["id"])
            except (ValueError, OSError, KeyError):
                continue
            for lane in state.get("lanes", []):
                for stage in lane.get("stages", []):
                    worker = stage.get("worker")
                    if not worker:
                        continue
                    try:
                        wstate = cli_mod.load(cli_mod.job_path(worker))
                    except (ValueError, OSError, KeyError):
                        continue
                    needs = wstate.get("needs_parent")
                    if needs:
                        return f"answer {worker}: {needs.get('question', '')}"
    for link in data.get("links", []):
        if link.get("kind") == "worker":
            live = _worker_live(link["id"])
            if live.get("status") in {"failed", "interrupted"}:
                return f"inspect failed {link['id']}"
        elif link.get("kind") == "batch":
            live = _batch_live(link["id"])
            if live.get("status") == "failed":
                return f"inspect failed {link['id']}"
    running = []
    for link in data.get("links", []):
        if link.get("kind") == "worker" and _worker_running(link["id"]):
            running.append(link["id"])
        elif link.get("kind") == "batch" and _batch_live(link["id"]).get("status") == "running":
            running.append(link["id"])
    if running:
        return f"wait for {' '.join(running)}"
    return "review results, then close the mission"


def _links_live(data):
    result = []
    for link in data.get("links", []):
        item = dict(link)
        if link.get("kind") == "worker":
            item.update(_worker_live(link["id"]))
        elif link.get("kind") == "batch":
            item.update(_batch_live(link["id"]))
        else:
            item["status"] = f"see claive-orch report {link.get('id')}"
        result.append(item)
    return result


def cmd_new(title, goal=None, goal_file=None):
    if goal_file is not None:
        path = Path(goal_file)
        if not path.is_absolute() or not path.is_file() or not path.stat().st_size:
            raise ValueError("--goal-file must be an existing nonempty absolute file")
        goal = path.read_text()
    if not goal:
        raise ValueError("a goal is required (--goal or --goal-file)")
    mission_id = uuid.uuid4().hex[:12]
    path = missions_dir() / mission_id
    path.mkdir(mode=0o700)
    cli_mod.save(path / "mission.json", dict(id=mission_id, title=title, goal=goal,
                                             status="open", created_at=time.time(),
                                             notes=[], links=[]))
    print(f"Mission {mission_id} | {title}")
    return 0


def cmd_note(mission_id, text):
    with updating(mission_id) as data:
        data["notes"].append(dict(at=time.time(), text=text))
    print(f"Mission {mission_id} noted")
    return 0


def cmd_link(mission_id, worker=None, batch=None, run=None):
    load_mission(mission_id)
    kinds = [bool(worker), bool(batch), bool(run)]
    if sum(kinds) != 1:
        raise ValueError("link needs exactly one of --worker, --batch, --run")
    if worker:
        kind, target = "worker", cli_mod.job_path(worker).name
    elif batch:
        from claivelib import batch as batch_mod
        batch_mod.batch_path(batch)
        kind, target = "batch", batch
    else:
        if not run:
            raise ValueError("--run must be non-empty")
        kind, target = "run", run
    _add_link(mission_id, kind, target)
    print(f"Mission {mission_id} linked to {kind} {target}")
    return 0


def cmd_show(mission_id, json_output=False):
    data = load_mission(mission_id)
    links = _links_live(data)
    nxt = compute_next(data)
    if json_output:
        print(json.dumps(dict(data, links=links, next=nxt), indent=2))
        return 0
    print(f"Mission {data['id']} | {data['title']} | {data['status']}")
    print(f"Goal: {data['goal']}")
    notes = data.get("notes", [])[-10:]
    if notes:
        print("Notes:")
        for note in notes:
            print(f"- {note.get('text', '')}")
    if links:
        print("Links:")
        for link in links:
            kind, target = link.get("kind"), link.get("id")
            if kind == "worker":
                badge = "ASK" if link.get("needs_parent") else str(link.get("status", "")).upper()
                print(f"worker {target} {badge} code={link.get('code')}")
                if link.get("needs_parent"):
                    print(f"  question: {link.get('question', '')}")
            elif kind == "batch":
                print(f"batch {target} {str(link.get('status', '')).upper()} code={link.get('code')}")
            else:
                print(f"run {target} see claive-orch report {target}")
    print(f"Next: {nxt}")
    return 0


def cmd_list(all_missions=False, json_output=False):
    items = []
    base = missions_dir()
    for child in base.iterdir() if base.exists() else []:
        state_file = child / "mission.json"
        if not state_file.is_file():
            continue
        try:
            items.append(json.loads(state_file.read_text()))
        except (OSError, ValueError):
            continue
    items.sort(key=lambda item: item.get("created_at", 0), reverse=True)
    if not all_missions:
        items = [item for item in items if item.get("status") == "open"]
    if json_output:
        print(json.dumps(items, indent=2))
    else:
        for item in items:
            print(f"Mission {item['id']} | {item['title']} | {item['status']}")
    return 0


def cmd_close(mission_id):
    with updating(mission_id) as data:
        data["status"] = "closed"
    print(f"Mission {mission_id} closed")
    return 0
