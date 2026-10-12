"""Turn-bound delivery receipts for opt-in Pi RPC steering."""
import dataclasses
import fcntl
import json
import os
from pathlib import Path
import re
import time
import uuid

from claivelib.acceptance import atomic
from claivelib.engines import get_engine
from claivelib.state import launch_config


def receipt(path, identifier):
    if not re.fullmatch(r"[0-9a-f]{32}", identifier):
        raise ValueError("invalid steering receipt ID")
    return json.loads((path / "steering" / "receipts" / f"{identifier}.json").read_text())


def submit(path, message):
    from claivelib import cli
    if not isinstance(message, str) or not message.strip() or len(message.encode()) > 1024 * 1024:
        raise ValueError("steering requires a nonblank message of at most 1 MiB")
    with (path / ".steering.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = cli.load(path)
        capability = get_engine(state.get("engine", "muse")).steering_capability()
        if not capability.supported:
            raise ValueError(capability.reason)
        if not launch_config(state).get("steering"):
            raise ValueError("worker was not launched with --enable-steering; follow-up remains a separate operation")
        if state["status"] != "running" or state.get("terminal") or not state.get("worker_pid"):
            raise ValueError("steering requires an actively running turn")
        if (path / "steering" / f"turn-{state.get('turn', 1):04}.closed").exists():
            raise ValueError("current steering turn has ended")
        identifier = uuid.uuid4().hex
        record = dict(id=identifier, turn=state.get("turn", 1), status="pending", created_at=time.time())
        atomic(path / "steering" / "receipts" / f"{identifier}.json", record)
        atomic(path / "steering" / "requests" / f"{time.time_ns():020}-{identifier}.json", dict(record, message=message))
        return record


def summary(path, state):
    # Steering is a new explicit launch flag; reporting it needs no legacy command decoding.
    launch = state.get("launch") if isinstance(state.get("launch"), dict) else {}
    capability = dataclasses.asdict(get_engine(state.get("engine", "muse")).steering_capability())
    records = []
    for file in (path / "steering" / "receipts").glob("*.json"):
        try:
            item = json.loads(file.read_text())
            if item.get("turn") == state.get("turn", 1):
                records.append(item)
        except (OSError, ValueError, AttributeError):
            continue
    return dict(capability=capability, enabled=bool(launch.get("steering")), receipts=records,
                pending=sum(item["status"] in {"pending", "sent"} for item in records),
                acknowledged=sum(item["status"] == "acknowledged" for item in records))


class Transport:
    """Supervisor-only nonblocking stdin owner; acknowledgements come from provider events."""
    def __init__(self, path, state, stream, initial_input):
        self.path, self.state, self.stream = path, state, stream
        self.pending = initial_input.encode()
        self.sending = None
        self.closed = False
        os.set_blocking(stream.fileno(), False)

    def update(self, identifier, **fields):
        record = receipt(self.path, identifier)
        record.update(fields, updated_at=time.time())
        atomic(self.path / "steering" / "receipts" / f"{identifier}.json", record)

    def pump(self):
        if self.closed:
            return
        if not self.pending:
            for file in sorted((self.path / "steering" / "requests").glob("*.json")):
                request = json.loads(file.read_text())
                identifier = request["id"]
                if request["turn"] != self.state.get("turn", 1):
                    self.update(identifier, status="not_delivered", reason="original turn ended")
                    file.unlink()
                    continue
                self.sending = identifier
                self.pending = (json.dumps(dict(type="steer", id=identifier, message=request["message"])) + "\n").encode()
                file.unlink()
                break
        if self.pending:
            try:
                count = os.write(self.stream.fileno(), self.pending)
                self.pending = self.pending[count:]
            except BlockingIOError:
                return
            except BrokenPipeError:
                self.close()
                return
            if not self.pending and self.sending:
                self.update(self.sending, status="sent")
                self.sending = None

    def observe(self, event):
        if event.get("type") != "control_response" or event.get("command") != "steer":
            return
        identifier = event.get("id")
        if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{32}", identifier):
            return
        try:
            record = receipt(self.path, identifier)
        except FileNotFoundError:
            return
        if record["turn"] != self.state.get("turn", 1) or record["status"] not in {"sent", "unconfirmed"}:
            return
        self.update(identifier, status="acknowledged" if event.get("success") is True else "rejected",
                    disposition=event.get("data"), reason=event.get("error"),
                    meaning="provider acknowledgement; model consumption is not established")

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.stream.close()
        finally:
            # Serialize with submit so requests racing terminal delivery cannot escape cleanup.
            with (self.path / ".steering.lock").open("w") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                atomic(self.path / "steering" / f"turn-{self.state.get('turn', 1):04}.closed", {"closed_at": time.time()})
                for file in (self.path / "steering" / "receipts").glob("*.json"):
                    record = json.loads(file.read_text())
                    if record.get("turn") != self.state.get("turn", 1):
                        continue
                    status = record["status"]
                    if status in {"pending", "sent"}:
                        status = "not_delivered" if status == "pending" else "unconfirmed"
                        self.update(record["id"], status=status, reason="turn ended before provider acknowledgement", turn_ended=True)
                    else:
                        self.update(record["id"], turn_ended=True)
                for file in (self.path / "steering" / "requests").glob("*.json"):
                    if json.loads(file.read_text()).get("turn") == self.state.get("turn", 1):
                        file.unlink()
