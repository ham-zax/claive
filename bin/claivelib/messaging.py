"""Parent-to-worker messages with durable receipts and follow-up delivery."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import tempfile
import time
import uuid

from claivelib.acceptance import atomic


MAX_MESSAGE_BYTES = 128 * 1024
MESSAGE_ID = re.compile(r"[0-9a-f]{32}")


@contextmanager
def lock(path):
    """Serialize parent sends with the supervisor's next-turn note delivery."""
    fd = os.open(path / ".messages.lock", os.O_WRONLY | os.O_CREAT, 0o600)
    with os.fdopen(fd, "w") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def validate_message(message):
    if not isinstance(message, str) or not message.strip():
        raise ValueError("message must not be empty; pass nonblank text with --message or --message-file")
    if len(message.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise ValueError("message exceeds 128 KiB; shorten it before sending")


def validate_id(identifier):
    if not isinstance(identifier, str) or not MESSAGE_ID.fullmatch(identifier):
        raise ValueError("--id must be exactly 32 lowercase hexadecimal characters")


def _receipt_path(path, identifier):
    return path / "messages" / "receipts" / f"{identifier}.json"


def _existing_receipt(path, identifier):
    file = _receipt_path(path, identifier)
    if file.is_file():
        return json.loads(file.read_text())
    return None


def _store_receipt(path, record):
    atomic(_receipt_path(path, record["id"]), record)


def _write_text(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".message-")
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _worker_for_note_or_wake(path):
    from claivelib import cli

    state = cli.load(path)
    if state.get("phase") == "session closed":
        raise ValueError("worker is closed; reopen it with claive open and its retained --session-id")
    if (path / "close.request").exists():
        raise ValueError("worker is closing; use an open reusable worker or reopen its retained session")
    if state["status"] == "cancelling":
        raise ValueError("worker is cancelling; wait for it to stop, then reopen its retained session")
    if state["status"] not in cli.ACTIVE:
        raise ValueError(f"worker is {state['status']}; reopen it with claive open and its retained --session-id")
    if not state.get("reusable"):
        raise ValueError("worker is not reusable; start it with claive open to accept note or wake messages")
    return state


def _pending_record(path, identifier, message, mode, requested_mode, result):
    created_at = time.time()
    record = dict(id=identifier, mode=mode, requested_mode=requested_mode,
                  status="queued", result=result, created_at=created_at)
    message_record = dict(id=identifier, message=message, mode=mode,
                          requested_mode=requested_mode, created_at=created_at)
    # Persist the receipt before the delivery file so a retry cannot issue a second send.
    _store_receipt(path, record)
    pending = path / "messages" / "pending"
    try:
        pending.mkdir(parents=True, exist_ok=True)
        message_file = pending / f"{time.time_ns():020}-{identifier}.json"
        atomic(message_file, message_record)
    except Exception:
        _receipt_path(path, identifier).unlink(missing_ok=True)
        raise
    return record, message_file


def _queued_result(path, message_file, record, message):
    """Queue an idle wake when possible; otherwise leave the note for the next turn."""
    from claivelib import turns

    prompt_file = path / "messages" / "wake-prompts" / f"{record['id']}.md"
    body = ("Messages from the parent (context, not a change of assignment):\n\n"
            f"[Message ID: {record['id']}]\n{message}\n")
    try:
        _write_text(prompt_file, body)
        request_id, state = turns.queue(
            path, str(prompt_file), receipt=True, only_if_idle=True,
            message_id=record["id"],
        )
    except Exception:
        message_file.unlink(missing_ok=True)
        _receipt_path(path, record["id"]).unlink(missing_ok=True)
        prompt_file.unlink(missing_ok=True)
        raise
    if request_id is None:
        prompt_file.unlink(missing_ok=True)
        record["result"] = "queued for next turn"
        _store_receipt(path, record)
        return record
    record.update(result="delivered and started a turn", turn=state.get("turn", 1) + 1,
                  turn_request_id=request_id)
    _store_receipt(path, record)
    return record


def send(path, message, mode="note", identifier=None):
    """Send one message and return (receipt, duplicate)."""
    if mode not in {"note", "wake", "now"}:
        raise ValueError("--mode must be note, wake, or now")
    if identifier is None:
        identifier = uuid.uuid4().hex
    validate_id(identifier)

    with lock(path):
        existing = _existing_receipt(path, identifier)
        if existing is not None:
            return existing, True
        validate_message(message)

        if mode == "now":
            from claivelib import cli, steering
            from claivelib.engines import get_engine
            from claivelib.state import launch_config

            state = cli.load(path)
            capability = get_engine(state.get("engine", "muse")).steering_capability()
            fallback_reason = None
            if not capability.supported:
                fallback_reason = capability.reason
            elif not launch_config(state).get("steering"):
                fallback_reason = "worker was not launched with --enable-steering"
            elif (state["status"] != "running" or state.get("terminal")
                  or not state.get("worker_pid")):
                fallback_reason = "no turn is running"
            elif (path / "steering" / f"turn-{state.get('turn', 1):04}.closed").exists():
                fallback_reason = "current steering turn has ended"

            if fallback_reason is None:
                # An in-flight steering request is the only path that can message a
                # non-reusable worker. Persist the ID before submitting that request.
                record = dict(id=identifier, mode="now", requested_mode="now", status="pending",
                              result="submitting mid-turn message", created_at=time.time())
                _store_receipt(path, record)
                try:
                    steer_receipt = steering.submit(path, message)
                except ValueError as error:
                    reason = str(error)
                    if not any(text in reason for text in (
                            "requires an actively running turn", "current steering turn has ended")):
                        _receipt_path(path, identifier).unlink(missing_ok=True)
                        raise
                    _receipt_path(path, identifier).unlink(missing_ok=True)
                    fallback_reason = reason
                else:
                    record.update(result=f"delivered mid-turn (steering receipt {steer_receipt['id']})",
                                  turn=steer_receipt.get("turn"),
                                  steering_receipt_id=steer_receipt["id"])
                    _store_receipt(path, record)
                    return record, False

            # Replace any provisional now receipt with the wake result while
            # retaining requested_mode=now for the audit trail.
            _receipt_path(path, identifier).unlink(missing_ok=True)
            _worker_for_note_or_wake(path)
            record, message_file = _pending_record(
                path, identifier, message, "wake", "now", "queued for next turn",
            )
            record = _queued_result(path, message_file, record, message)
            if record.get("turn_request_id"):
                record["result"] = f"fallback to wake: {fallback_reason}; delivered and started a turn"
            else:
                record["result"] = f"fallback to wake: {fallback_reason}; queued for next turn"
            _store_receipt(path, record)
            return record, False

        _worker_for_note_or_wake(path)
        record, message_file = _pending_record(
            path, identifier, message, mode, mode, "queued for next turn",
        )
        if mode == "note":
            return record, False
        record = _queued_result(path, message_file, record, message)
        return record, False


def pending_messages(path):
    """Read queued notes in send order, cleaning up a prior completed move if needed."""
    pending = path / "messages" / "pending"
    if not pending.exists():
        return []
    delivered = path / "messages" / "delivered"
    messages = []
    for file in sorted(pending.glob("*.json")):
        record = json.loads(file.read_text())
        receipt_file = _receipt_path(path, record["id"])
        receipt = json.loads(receipt_file.read_text()) if receipt_file.is_file() else {}
        if receipt.get("status") == "delivered":
            delivered.mkdir(parents=True, exist_ok=True)
            os.replace(file, delivered / file.name)
            continue
        messages.append((file, record))
    return messages


def format_block(messages, already_in_prompt=()):
    already_in_prompt = set(already_in_prompt)
    visible = [record for _file, record in messages if record["id"] not in already_in_prompt]
    if not visible:
        return ""
    lines = ["Messages from the parent (context, not a change of assignment):"]
    for record in visible:
        lines.extend(("", f"[Message ID: {record['id']}]", record["message"]))
    return "\n".join(lines)


def mark_delivered(path, messages, turn):
    delivered = path / "messages" / "delivered"
    delivered.mkdir(parents=True, exist_ok=True)
    for file, message in messages:
        receipt_file = _receipt_path(path, message["id"])
        if receipt_file.is_file():
            receipt = json.loads(receipt_file.read_text())
            receipt.update(status="delivered", turn=turn, delivered_at=time.time())
            _store_receipt(path, receipt)
        os.replace(file, delivered / file.name)
