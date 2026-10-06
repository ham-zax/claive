"""Batches: parallel lanes of ordered single-turn stage workers."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

from claivelib import cli as cli_mod
from claivelib.engines import DEFAULT_ENGINE, get_engine
from claivelib.roles import ROLES

STAGE_OPTIONS = {"prompt_file", "workspace", "label", "role", "engine", "model",
                 "provider", "reasoning_effort", "max_model_steps", "read_only",
                 "report", "context"}
TOP_KEYS = {"label", "workspace", "mission", "defaults", "lanes"}
KEY_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
TERMINAL = {"done", "failed", "needs_parent", "cancelled", "skipped"}


def batches_dir():
    path = cli_mod.root() / "batches"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def batch_path(batch_id):
    if not re.fullmatch(r"[0-9a-f]{12}", batch_id or ""):
        raise ValueError(f"unknown batch: {batch_id}")
    path = batches_dir() / batch_id
    if not (path / "state.json").is_file():
        raise ValueError(f"unknown batch: {batch_id}")
    return path


def load_state(batch_id):
    return json.loads((batch_path(batch_id) / "state.json").read_text())


def save_state(batch_id, state):
    cli_mod.save(batch_path(batch_id) / "state.json", state)


def _check_option_types(where, options):
    for key, value in options.items():
        if key in {"prompt_file", "workspace", "label", "role", "engine", "model",
                   "provider", "reasoning_effort"} and not isinstance(value, str):
            raise ValueError(f"{where}: {key} must be a string")
        if key == "max_model_steps" and (not isinstance(value, int) or isinstance(value, bool)):
            raise ValueError(f"{where}: max_model_steps must be an integer")
        if key in {"read_only", "report"} and not isinstance(value, bool):
            raise ValueError(f"{where}: {key} must be a boolean")
        if key == "context" and value != "previous":
            raise ValueError(f"{where}: context must be \"previous\"")


def _effective_stage(plan, lane_index, stage_index):
    lane = plan["lanes"][lane_index]
    stage = lane["stages"][stage_index]
    defaults = plan.get("defaults") or {}
    resolved = {}
    for opt in STAGE_OPTIONS:
        if opt in stage:
            resolved[opt] = stage[opt]
        elif opt in defaults:
            resolved[opt] = defaults[opt]
    if resolved.get("workspace") is None and plan.get("workspace") is not None:
        resolved["workspace"] = plan["workspace"]
    return resolved


def validate_plan(plan_path):
    raw_path = Path(plan_path)
    if not raw_path.is_absolute():
        raise ValueError("plan must be an absolute path")
    try:
        plan = json.loads(raw_path.read_text())
    except (OSError, ValueError) as error:
        raise ValueError(f"invalid plan JSON: {error}") from error
    if not isinstance(plan, dict):
        raise ValueError("plan must be a JSON object")
    for key in plan:
        if key not in TOP_KEYS:
            raise ValueError(f"unknown plan key: {key}")
    if "label" in plan and not isinstance(plan["label"], str):
        raise ValueError("plan label must be a string")
    if "workspace" in plan:
        workspace = plan["workspace"]
        if not isinstance(workspace, str) or not Path(workspace).is_absolute() or not Path(workspace).is_dir():
            raise ValueError("workspace must be an existing absolute directory")
    if "mission" in plan and (not isinstance(plan["mission"], str) or not plan["mission"]):
        raise ValueError("mission must be a non-empty string")
    defaults = plan.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise ValueError("defaults must be an object")
    for key in defaults:
        if key not in STAGE_OPTIONS:
            raise ValueError(f"unknown option: {key}")
    _check_option_types("defaults", defaults)
    lanes = plan.get("lanes")
    if not isinstance(lanes, list) or not 1 <= len(lanes) <= 16:
        raise ValueError("plan must have 1..16 lanes")
    seen_lanes = set()
    total = 0
    for lane in lanes:
        if not isinstance(lane, dict) or set(lane) - {"key", "stages"}:
            raise ValueError("each lane needs only key and stages")
        key = lane.get("key")
        if not isinstance(key, str) or not KEY_RE.fullmatch(key):
            raise ValueError(f"invalid lane key: {key}")
        if key in seen_lanes:
            raise ValueError(f"duplicate lane key: {key}")
        seen_lanes.add(key)
        stages = lane.get("stages")
        if not isinstance(stages, list) or not 1 <= len(stages) <= 8:
            raise ValueError(f"lane {key} must have 1..8 stages")
        seen_stages = set()
        for position, stage in enumerate(stages):
            if not isinstance(stage, dict):
                raise ValueError(f"lane {key} stage {position} must be an object")
            for opt in stage:
                if opt not in STAGE_OPTIONS | {"key"}:
                    raise ValueError(f"unknown option: {opt}")
            stage_key = stage.get("key")
            if not isinstance(stage_key, str) or not KEY_RE.fullmatch(stage_key):
                raise ValueError(f"invalid stage key: {stage_key}")
            if stage_key in seen_stages:
                raise ValueError(f"duplicate stage key: {key}.{stage_key}")
            seen_stages.add(stage_key)
            total += 1
    for lane_index, lane in enumerate(lanes):
        for stage_index, stage in enumerate(lane["stages"]):
            name = f"{lane['key']}.{stage['key']}"
            _check_option_types(f"stage {name}", {k: v for k, v in stage.items() if k != "key"})
            resolved = _effective_stage(plan, lane_index, stage_index)
            prompt = resolved.get("prompt_file")
            if prompt is None:
                raise ValueError(f"stage {name} needs prompt_file")
            if not isinstance(prompt, str) or not Path(prompt).is_absolute():
                raise ValueError(f"stage {name}: prompt_file must be an absolute path")
            prompt_path = Path(prompt)
            if not prompt_path.is_file() or not prompt_path.stat().st_size:
                raise ValueError(f"stage {name}: prompt_file must be an existing nonempty file")
            workspace = resolved.get("workspace")
            if workspace is None:
                raise ValueError(f"stage {name} needs workspace")
            if not isinstance(workspace, str) or not Path(workspace).is_absolute() or not Path(workspace).is_dir():
                raise ValueError(f"stage {name}: workspace must be an existing absolute directory")
            role = resolved.get("role")
            if role is not None and role not in ROLES:
                raise ValueError(f"stage {name}: unknown role: {role}")
            model = resolved.get("model")
            if model is None and role:
                model = ROLES[role].get("model")
            if model is not None:
                cli_mod.check_model_policy(model)
            engine_name = resolved.get("engine") or (ROLES[role]["engine"] if role else None) or DEFAULT_ENGINE
            get_engine(engine_name)
            steps = resolved.get("max_model_steps")
            if steps is not None and steps < 1:
                raise ValueError(f"stage {name}: max_model_steps must be positive")
            if resolved.get("context") is not None and stage_index == 0:
                raise ValueError(f"stage {name}: context \"previous\" not allowed on first stage")
    if plan.get("mission"):
        from claivelib import mission as mission_mod
        mission_mod.require_open(plan["mission"])
    return plan, len(lanes), total


def _stage_args(resolved, workspace, prompt_file):
    return argparse.Namespace(
        action="start", workspace=workspace, prompt_file=prompt_file,
        label=resolved.get("label"), engine=resolved.get("engine"),
        role=resolved.get("role"), report=bool(resolved.get("report")),
        reasoning_effort=resolved.get("reasoning_effort"),
        max_model_steps=resolved.get("max_model_steps"),
        read_only=bool(resolved.get("read_only")), worktree=False,
        worktree_existing=None, worktree_base=None, web=False,
        model=resolved.get("model"), session_id=None, output_schema=None,
        no_session_log=False, provider=resolved.get("provider"), mission=None)


def _launch_stage(batch_id, lane_key, stage_key, resolved, workspace, prompt_file):
    saved = os.environ.pop("CLAIVE_MISSION", None)
    try:
        path, state = cli_mod.create_job(_stage_args(resolved, workspace, prompt_file))
    finally:
        if saved is not None:
            os.environ["CLAIVE_MISSION"] = saved
    batch = load_state(batch_id)
    state["batch"] = batch_id
    state["batch_stage"] = f"{lane_key}.{stage_key}"
    if batch.get("mission"):
        state["mission"] = batch["mission"]
    cli_mod.save(path / "state.json", state)
    with (path / "supervisor.log").open("w") as log:
        subprocess.Popen([sys.executable, cli_mod.launcher_path(), "_supervise", state["id"]],
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         start_new_session=True)
    return state["id"]


def _settle_worker(worker_id):
    try:
        path = cli_mod.job_path(worker_id)
    except ValueError:
        return None
    try:
        state = cli_mod.load(path)
    except (OSError, ValueError, KeyError):
        return None
    if state["status"] in cli_mod.ACTIVE:
        return None
    return state


def _stage_outcome(state):
    code = cli_mod.outcome_code(state)
    if code == 0:
        return "done", code
    if code == 3:
        return "needs_parent", code
    if code == 130:
        return "cancelled", code
    return "failed", code


def _start_runner(batch_id):
    path = batch_path(batch_id)
    env = {key: value for key, value in os.environ.items() if key != "CLAIVE_WORKER_ID"}
    with (path / "runner.log").open("w") as log:
        subprocess.Popen([sys.executable, cli_mod.launcher_path(), "_batch", batch_id],
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         start_new_session=True, env=env)


def cmd_validate(plan_path):
    _plan, lanes, stages = validate_plan(plan_path)
    print(f"Plan ok: {lanes} lanes, {stages} stages")
    return 0


def cmd_start(plan_path, json_output=False):
    cli_mod.check_nested()
    plan, _lanes, _stages = validate_plan(plan_path)
    from claivelib import mission as mission_mod
    mission_id = plan.get("mission") or mission_mod.resolve_mission()
    if mission_id:
        mission_mod.require_open(mission_id)
    batch_id = uuid.uuid4().hex[:12]
    path = batches_dir() / batch_id
    path.mkdir(mode=0o700)
    (path / "prompts").mkdir(mode=0o700)
    label = plan.get("label") or Path(plan_path).stem
    (path / "plan.json").write_text(json.dumps(plan, indent=2) + "\n")
    state = dict(id=batch_id, label=label, workspace=plan.get("workspace"),
                 mission=mission_id, status="running", code=None,
                 started_at=time.time(), ended_at=None,
                 lanes=[dict(key=lane["key"], stages=[dict(key=stage["key"], status="pending",
                                                            worker=None, code=None)
                                                       for stage in lane["stages"]])
                        for lane in plan["lanes"]])
    cli_mod.save(path / "state.json", state)
    if mission_id:
        mission_mod.link_auto(mission_id, "batch", batch_id)
    _start_runner(batch_id)
    if json_output:
        print(json.dumps({"id": batch_id, "path": str(path)}))
    else:
        print(f"Batch {batch_id} | {label} | {path}")
    return 0


def _render(state):
    lines = [f"Batch {state['id']} | {state['label']} | {state['status']}"]
    for lane in state.get("lanes", []):
        for stage in lane.get("stages", []):
            worker = stage.get("worker") or "-"
            line = f"{lane['key']}.{stage['key']} {str(stage['status']).upper()} {worker}"
            if stage.get("code") is not None:
                line += f" code={stage['code']}"
            if stage.get("error"):
                line += f" ({stage['error']})"
            lines.append(line)
    return "\n".join(lines)


def cmd_status(batch_id, json_output=False):
    state = load_state(batch_id)
    print(json.dumps(state, indent=2) if json_output else _render(state))
    return 0


def cmd_wait(batch_id, timeout=None):
    if timeout is not None and timeout <= 0:
        raise ValueError("--timeout must be > 0")
    start = time.monotonic()
    while True:
        state = load_state(batch_id)
        if state.get("status") != "running":
            print(_render(state))
            code = state.get("code")
            return code if isinstance(code, int) else 1
        if timeout is not None and time.monotonic() - start >= timeout:
            print(f"Timed out after {'%g' % timeout}s; batch {batch_id} still running")
            return 124
        time.sleep(0.25)


def cmd_cancel(batch_id):
    state = load_state(batch_id)
    if state.get("status") != "running":
        print(f"Batch is already {state.get('status')}.")
        return 0
    cli_mod.save(batch_path(batch_id) / "cancel.request", {"requested_at": time.time()})
    for lane in state.get("lanes", []):
        for stage in lane.get("stages", []):
            if stage.get("status") == "running" and stage.get("worker"):
                try:
                    subprocess.run([sys.executable, cli_mod.launcher_path(), "cancel", stage["worker"]],
                                   capture_output=True, timeout=15)
                except Exception:
                    pass
    print(f"Batch {batch_id} cancelled")
    return 0


def cmd_retry(batch_id):
    state = load_state(batch_id)
    if state.get("status") == "running":
        raise ValueError("batch is still running")
    count = 0
    for lane in state.get("lanes", []):
        for stage in lane.get("stages", []):
            if stage.get("status") != "done":
                stage.update(status="pending", worker=None, code=None, error=None)
                count += 1
    state.update(status="running", code=None, ended_at=None)
    (batch_path(batch_id) / "cancel.request").unlink(missing_ok=True)
    save_state(batch_id, state)
    _start_runner(batch_id)
    print(f"Batch {batch_id} retrying {count} stages")
    return 0


def cmd_list(json_output=False):
    items = []
    base = batches_dir()
    for child in base.iterdir() if base.exists() else []:
        state_file = child / "state.json"
        if not state_file.is_file():
            continue
        try:
            items.append(json.loads(state_file.read_text()))
        except (OSError, ValueError):
            continue
    items.sort(key=lambda item: item.get("started_at", 0), reverse=True)
    if json_output:
        print(json.dumps(items, indent=2))
    else:
        for item in items:
            print(f"Batch {item['id']} | {item['label']} | {item['status']}")
    return 0


def _finalize(batch_id, state, cancelled):
    if cancelled or any(stage.get("status") == "cancelled" for lane in state["lanes"] for stage in lane["stages"]):
        state.update(status="cancelled", code=130)
    elif any(stage.get("status") == "failed" for lane in state["lanes"] for stage in lane["stages"]):
        state.update(status="failed", code=1)
    elif any(stage.get("status") == "needs_parent" for lane in state["lanes"] for stage in lane["stages"]):
        state.update(status="needs_parent", code=3)
    else:
        state.update(status="completed", code=0)
    state["ended_at"] = time.time()
    save_state(batch_id, state)
    from claivelib import inbox as inbox_mod
    try:
        inbox_mod.append(inbox_mod.batch_event(batch_id, state["label"], state["status"],
                                               state["code"], state.get("mission")))
    except Exception:
        pass
    return state["code"]


def runner(batch_id):
    try:
        return _run(batch_id)
    except Exception:
        try:
            state = load_state(batch_id)
            if state.get("status") == "running":
                state.update(status="failed", code=1, ended_at=time.time())
                save_state(batch_id, state)
                from claivelib import inbox as inbox_mod
                try:
                    inbox_mod.append(inbox_mod.batch_event(batch_id, state["label"], "failed", 1,
                                                           state.get("mission")))
                except Exception:
                    pass
        except Exception:
            pass
        return 1


def _run(batch_id):
    path = batch_path(batch_id)
    plan = json.loads((path / "plan.json").read_text())
    while True:
        state = load_state(batch_id)
        cancelled = (path / "cancel.request").exists()
        if cancelled:
            changed = False
            for lane in state["lanes"]:
                for stage in lane["stages"]:
                    if stage.get("status") == "pending":
                        stage.update(status="skipped", worker=None, code=None)
                        changed = True
            if changed:
                save_state(batch_id, state)
            for lane in state["lanes"]:
                for stage in lane["stages"]:
                    if stage.get("status") != "running":
                        continue
                    worker_state = _settle_worker(stage.get("worker")) if stage.get("worker") else None
                    if worker_state is None:
                        continue
                    status, code = _stage_outcome(worker_state)
                    stage.update(status=status, code=code)
                    save_state(batch_id, state)
            state = load_state(batch_id)
            if not any(stage.get("status") == "running" for lane in state["lanes"] for stage in lane["stages"]):
                return _finalize(batch_id, state, True)
            time.sleep(0.25)
            continue
        for lane_index, lane in enumerate(state["lanes"]):
            if any(stage.get("status") == "running" for stage in lane["stages"]):
                continue
            pending_index = next((i for i, s in enumerate(lane["stages"]) if s.get("status") == "pending"), None)
            if pending_index is None:
                continue
            if pending_index > 0 and lane["stages"][pending_index - 1].get("status") != "done":
                for stage in lane["stages"][pending_index:]:
                    if stage.get("status") == "pending":
                        stage.update(status="skipped", worker=None, code=None)
                save_state(batch_id, state)
                continue
            lane_key = lane["key"]
            stage_key = lane["stages"][pending_index]["key"]
            resolved = _effective_stage(plan, lane_index, pending_index)
            try:
                workspace = resolved["workspace"]
                if resolved.get("context") == "previous":
                    prev = lane["stages"][pending_index - 1]
                    prev_text = ""
                    if prev.get("worker"):
                        try:
                            prev_text = (cli_mod.job_path(prev["worker"]) / "result.txt").read_text()
                        except OSError:
                            prev_text = ""
                    prompt_text = Path(resolved["prompt_file"]).read_text()
                    combined = (f"Context from previous stage {lane_key}.{prev['key']}:\n"
                                f"{prev_text}\n\n{prompt_text}")
                    target = path / "prompts" / f"{lane_key}.{stage_key}.md"
                    target.write_text(combined)
                    prompt_file = str(target)
                else:
                    prompt_file = resolved["prompt_file"]
                worker_id = _launch_stage(batch_id, lane_key, stage_key, resolved, workspace, prompt_file)
            except (ValueError, OSError) as error:
                state = load_state(batch_id)
                for lane_item in state["lanes"]:
                    if lane_item["key"] != lane_key:
                        continue
                    index = next(i for i, s in enumerate(lane_item["stages"]) if s["key"] == stage_key)
                    if lane_item["stages"][index].get("status") == "pending":
                        lane_item["stages"][index].update(status="failed", worker=None, code=1,
                                                          error=f"launch failed: {error}")
                    for stage in lane_item["stages"][index + 1:]:
                        if stage.get("status") == "pending":
                            stage.update(status="skipped", worker=None, code=None)
                save_state(batch_id, state)
                continue
            state = load_state(batch_id)
            for lane_item in state["lanes"]:
                if lane_item["key"] != lane_key:
                    continue
                for stage in lane_item["stages"]:
                    if stage["key"] == stage_key and stage.get("status") == "pending":
                        stage.update(status="running", worker=worker_id, code=None)
            save_state(batch_id, state)
        state = load_state(batch_id)
        for lane in state["lanes"]:
            for index, stage in enumerate(lane["stages"]):
                if stage.get("status") != "running" or not stage.get("worker"):
                    continue
                worker_state = _settle_worker(stage["worker"])
                if worker_state is None:
                    continue
                status, code = _stage_outcome(worker_state)
                stage.update(status=status, code=code)
                if status != "done":
                    for later in lane["stages"][index + 1:]:
                        if later.get("status") == "pending":
                            later.update(status="skipped", worker=None, code=None)
                save_state(batch_id, state)
        state = load_state(batch_id)
        if not any(stage.get("status") in {"pending", "running"}
                   for lane in state["lanes"] for stage in lane["stages"]):
            return _finalize(batch_id, state, False)
        time.sleep(0.25)
