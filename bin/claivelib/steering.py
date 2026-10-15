"""Turn-bound delivery receipts for opt-in Pi, Codex and Claude duplex steering."""
import dataclasses
import fcntl
import json
import math
import os
import re
import time
import uuid

from claivelib.acceptance import atomic
from claivelib.engines import get_engine
from claivelib.state import launch_config


class ReceiptError(ValueError):
    """A local receipt is corrupt, rather than a malformed provider event."""


def _raw_receipt(path, identifier):
    if not re.fullmatch(r"[0-9a-f]{32}", identifier):
        raise ValueError("invalid steering receipt ID")
    filename = path / "steering" / "receipts" / f"{identifier}.json"
    try:
        record = json.loads(filename.read_text())
        if not isinstance(record, dict) or record.get("id") != identifier:
            raise ValueError("receipt identity does not match its file")
    except (ValueError, UnicodeError) as error:
        raise ReceiptError(f"invalid steering receipt {identifier} at {filename}: {error}") from error
    return record


def _metadata(record):
    return {key: value for key, value in record.items() if key != "payload"}


def receipt(path, identifier):
    return _metadata(_raw_receipt(path, identifier))


def _update_receipt(path, identifier, fields):
    if fields.keys() & {"id", "turn", "source", "created_at", "payload"}:
        raise ValueError("cannot update immutable steering receipt fields")
    try:
        record = _raw_receipt(path, identifier)
    except OSError as error:
        raise ReceiptError(f"cannot update steering receipt {identifier} at {path}: {error}") from error
    record.update(fields, updated_at=time.time())
    atomic(path / "steering" / "receipts" / f"{identifier}.json", record)


def submit(path, message, expires_at=None, source=None):
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
        if source is not None:
            record["source"] = source
        if source != "budget":
            sequences = []
            for file in (path / "steering" / "receipts").glob("*.json"):
                existing = json.loads(file.read_text())
                payload = existing.get("payload")
                if (existing.get("turn") == record["turn"] and existing.get("source") != "budget"
                        and isinstance(payload, dict)):
                    sequence = payload.get("sequence")
                    if type(sequence) is int and sequence > 0:
                        sequences.append(sequence)
            record["payload"] = dict(message=message, sequence=max(sequences, default=0) + 1,
                                     expires_at=expires_at)
        atomic(path / "steering" / "receipts" / f"{identifier}.json", record)
        request = dict(_metadata(record), message=message)
        if expires_at is not None:
            request["expires_at"] = expires_at
        atomic(path / "steering" / "requests" / f"{time.time_ns():020}-{identifier}.json", request)
        return _metadata(record)


def withdraw(path, receipt_id):
    """Claim an unconsumed request so it can no longer reach the provider."""
    if not re.fullmatch(r"[0-9a-f]{32}", receipt_id):
        raise ValueError("invalid steering receipt ID")
    with (path / ".steering.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for file in (path / "steering" / "requests").glob(f"*-{receipt_id}.json"):
            try:
                os.rename(file, file.with_name(file.name + ".withdrawn"))
            except FileNotFoundError:
                continue
            record = _raw_receipt(path, receipt_id)
            record.update(status="withdrawn", updated_at=time.time())
            atomic(path / "steering" / "receipts" / f"{receipt_id}.json", record)
            return True
    return False


def summary(path, state):
    # Steering is a new explicit launch flag; reporting it needs no legacy command decoding.
    launch = state.get("launch") if isinstance(state.get("launch"), dict) else {}
    capability = dataclasses.asdict(get_engine(state.get("engine", "muse")).steering_capability())
    records = []
    for file in (path / "steering" / "receipts").glob("*.json"):
        try:
            item = json.loads(file.read_text())
            if item.get("turn") == state.get("turn", 1):
                records.append(_metadata(item))
        except (OSError, ValueError, AttributeError):
            continue
    parent_records = [item for item in records if item.get("source") != "budget"]
    return dict(capability=capability, enabled=bool(launch.get("steering")), receipts=records,
                pending=sum(item["status"] in {"pending", "sent"} for item in parent_records),
                acknowledged=sum(item["status"] == "acknowledged" for item in parent_records),
                budget=sum(item.get("source") == "budget" for item in records))


def continuation_records(path, turn, carried_ids=(), entered_ids=(), now=None):
    """Read eligible durable external inputs, including explicit historical carry."""
    now = time.time() if now is None else now
    entered_ids = set(entered_ids)
    with (path / ".steering.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        records = {}
        for file in (path / "steering" / "receipts").glob("*.json"):
            record = _raw_receipt(path, file.stem)
            if record.get("turn") == turn:
                records[record["id"]] = record
        for identifier in set(carried_ids):
            try:
                records[identifier] = _raw_receipt(path, identifier)
            except (OSError, ValueError) as exc:
                raise ValueError(f"invalid steering receipt {identifier} at {path}: {exc}") from exc
        eligible = []
        for identifier, record in records.items():
            if record.get("source") == "budget" or record.get("status") in {"rejected", "withdrawn"}:
                continue
            status = record.get("status")
            if status not in {"acknowledged", "sent", "unconfirmed", "pending", "not_delivered"}:
                raise ValueError(f"unknown steering status for {identifier} at {path}")
            payload = record.get("payload")
            expiry = payload.get("expires_at") if isinstance(payload, dict) else None
            valid_expiry = (expiry is None or type(expiry) is int or
                            (type(expiry) is float and math.isfinite(expiry)))
            if (status in {"pending", "not_delivered"} and identifier not in entered_ids
                    and valid_expiry and expiry is not None and now >= expiry):
                continue
            if (not isinstance(payload, dict) or not isinstance(payload.get("message"), str)
                    or not payload["message"].strip() or type(payload.get("sequence")) is not int
                    or payload["sequence"] <= 0 or "expires_at" not in payload or not valid_expiry):
                raise ValueError(f"invalid steering payload for {identifier} at {path}")
            eligible.append(record)
        return sorted(eligible, key=lambda record: (record["turn"], record["payload"]["sequence"], record["id"]))


def stage_continuation(path, identifiers, *, turn, session_id, prompt_file, input_id):
    with (path / ".steering.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        records = []
        for identifier in dict.fromkeys(identifiers):
            try:
                records.append(_raw_receipt(path, identifier))
            except (OSError, ValueError) as exc:
                raise ValueError(f"invalid steering receipt {identifier} at {path}: {exc}") from exc
        for record in records:
            record["continuation"] = dict(turn=turn, session_id=session_id, prompt_file=str(prompt_file),
                                          input_id=input_id, phase="staged")
            atomic(path / "steering" / "receipts" / f"{record['id']}.json", record)


def enter_continuation(path, input_id):
    identifiers = []
    with (path / ".steering.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for file in sorted((path / "steering" / "receipts").glob("*.json")):
            record = _raw_receipt(path, file.stem)
            continuation = record.get("continuation", {})
            if continuation.get("input_id") == input_id and continuation.get("phase") == "staged":
                continuation["phase"] = "entered"
                atomic(file, record)
                identifiers.append(record["id"])
    return identifiers


class Transport:
    """Supervisor-only nonblocking stdin owner; acknowledgements come from provider events."""
    def __init__(self, path, state, stream, initial_input):
        self.path, self.state, self.stream = path, state, stream
        self.frame = get_engine(state.get("engine", "muse")).steering_frame
        self.pending = initial_input.encode()
        self.sending = None
        self.closed = False
        os.set_blocking(stream.fileno(), False)

    def update(self, identifier, **fields):
        with (self.path / ".steering.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            _update_receipt(self.path, identifier, fields)

    def pump(self):
        if self.closed:
            return
        if not self.pending:
            for file in sorted((self.path / "steering" / "requests").glob("*.json")):
                claimed = file.with_suffix(".claimed")
                try:
                    os.rename(file, claimed)
                except FileNotFoundError:
                    continue
                try:
                    request = json.loads(claimed.read_text())
                finally:
                    claimed.unlink(missing_ok=True)
                identifier = request["id"]
                if request.get("expires_at") is not None and time.time() >= request["expires_at"]:
                    self.update(identifier, status="withdrawn", reason="expired before delivery")
                    continue
                if request["turn"] != self.state.get("turn", 1):
                    self.update(identifier, status="not_delivered", reason="original turn ended")
                    continue
                self.sending = identifier
                self.pending = self.frame(identifier, request["message"]).encode()
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
                    record = _raw_receipt(self.path, file.stem)
                    if record.get("turn") != self.state.get("turn", 1):
                        continue
                    status = record["status"]
                    if status in {"pending", "sent"}:
                        status = "not_delivered" if status == "pending" else "unconfirmed"
                        _update_receipt(self.path, record["id"], dict(status=status,
                                        reason="turn ended before provider acknowledgement", turn_ended=True))
                    else:
                        _update_receipt(self.path, record["id"], dict(turn_ended=True))
                requests = self.path / "steering" / "requests"
                for file in (list(requests.glob("*.json")) + list(requests.glob("*.claimed"))
                             + list(requests.glob("*.withdrawn"))):
                    if json.loads(file.read_text()).get("turn") == self.state.get("turn", 1):
                        file.unlink()
