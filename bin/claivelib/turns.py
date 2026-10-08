"""Queue follow-ups and retain immutable, request-specific delivery receipts."""
import fcntl
import json
import math
import os
from pathlib import Path
import re
import time
import uuid

from claivelib import cli
from claivelib.engines import DEFAULT_ENGINE, get_engine


def queue(path, prompt_file, label=None, reasoning_effort=None, max_model_steps=None,
          expires_at=None, requires_no_parent=False, stop_file=None, receipt=False):
    fd = os.open(path / ".requests.lock", os.O_WRONLY | os.O_CREAT, 0o600)
    with os.fdopen(fd, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = cli.load(path)
        if not state.get("reusable") or state["status"] not in cli.ACTIVE or (path / "close.request").exists():
            raise ValueError("worker is not available for follow-up; reopen its retained --session-id if appropriate")
        if requires_no_parent and state.get("needs_parent"):
            raise ValueError("worker is waiting for a parent decision")
        prompt = Path(prompt_file)
        if not prompt.is_absolute() or not prompt.is_file() or not prompt.stat().st_size:
            raise ValueError("--prompt-file must be an existing nonempty absolute file")
        if max_model_steps is not None and max_model_steps < 1:
            raise ValueError("--max-model-steps must be positive")
        policy = json.loads((path / "policy.json").read_text())
        get_engine(state.get("engine", DEFAULT_ENGINE)).validate_turn(cli.turn_request(
            state, prompt_file=str(prompt),
            reasoning_effort=reasoning_effort or policy["reasoning_effort"],
            max_model_steps=max_model_steps if max_model_steps is not None else policy["max_model_steps"],
        ))
        request_id = uuid.uuid4().hex
        request = dict(prompt_file=str(prompt), label=label,
                       reasoning_effort=reasoning_effort, max_model_steps=max_model_steps)
        if receipt:
            request["request_id"] = request_id
        if expires_at is not None:
            request["expires_at"] = expires_at
        if requires_no_parent:
            request["requires_no_parent"] = True
        if stop_file is not None:
            request["stop_file"] = str(stop_file)
        validate(request)
        cli.save(path / "requests" / f"{time.time_ns():020}-{request_id}.json", request)
    return request_id if receipt else None, state


def validate(request):
    if not isinstance(request, dict):
        raise ValueError("follow-up request must be a JSON object")
    prompt = request.get("prompt_file")
    if not isinstance(prompt, str) or not Path(prompt).is_absolute():
        raise ValueError("queued prompt_file must be an absolute path")
    if "request_id" in request:
        identifier = request["request_id"]
        if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{32}", identifier):
            raise ValueError("invalid follow-up request ID")
    if "expires_at" in request:
        deadline = request["expires_at"]
        try:
            finite = not isinstance(deadline, bool) and isinstance(deadline, (int, float)) and math.isfinite(deadline)
        except OverflowError:
            finite = False
        if not finite:
            raise ValueError("queued expires_at must be a finite timestamp")
    if "stop_file" in request:
        marker = request["stop_file"]
        if not isinstance(marker, str) or not Path(marker).is_absolute():
            raise ValueError("queued stop_file must be an absolute path")
    if "requires_no_parent" in request and not isinstance(request["requires_no_parent"], bool):
        raise ValueError("queued requires_no_parent must be a boolean")
    for key in ("reasoning_effort", "label"):
        if request.get(key) is not None and not isinstance(request[key], str):
            raise ValueError(f"queued {key} must be a string")
    steps = request.get("max_model_steps")
    if steps is not None and (isinstance(steps, bool) or not isinstance(steps, int) or steps < 1):
        raise ValueError("queued max_model_steps must be a positive integer")


def complete(path, state, request_id=None, error=None, rejection_code=1):
    request_id = request_id if error else request_id or state.get("request_id")
    if not request_id:
        return
    if not isinstance(request_id, str) or not re.fullmatch(r"[0-9a-f]{32}", request_id):
        return  # Foreign queue metadata must not terminate a durable supervisor.
    responses = path / "responses"
    response_file = responses / f"{request_id}.json"
    if response_file.exists():
        return
    responses.mkdir(mode=0o700, exist_ok=True)
    response = dict(request_id=request_id, worker=state["id"], turn=state.get("turn"),
                    session_id=cli.worker_session_id(state), engine=state.get("engine"),
                    model=state.get("model"), ended_at=time.time())
    if error:
        response.update(code=rejection_code, status="rejected", error=error, answer="", turn=None,
                        needs_parent=state.get("needs_parent") if rejection_code == 3 else None)
    else:
        response.update(code=cli.outcome_code(state), status=state["status"],
                        answer=(path / "result.txt").read_text() if (path / "result.txt").exists() else "",
                        report_state=state.get("report_state"), report=state.get("report"),
                        report_contract=bool(state.get("report_contract")),
                        needs_parent=state.get("needs_parent"), failure_kind=state.get("failure_kind"),
                        error=state.get("error"))
    cli.save(response_file, response)
