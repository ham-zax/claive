"""Parent-to-worker messages with durable receipts and follow-up delivery."""
from contextlib import contextmanager
import datetime
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


# Parent-message envelopes and extraction are adapted from lop
# (local_operator/harness/comms.py: TO_CHILD_INSTRUCTIONS, extract_parent_message,
# SubagentComms._format_to_child).
# Copyright (c) 2024 Radient Inc. Licensed under the MIT License:
# Permission is hereby granted, free of charge, to any person obtaining a copy of this
# software and associated documentation files (the "Software"), to deal in the Software
# without restriction, including without limitation the rights to use, copy, modify,
# merge, publish, distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to the following
# conditions: The above copyright notice and this permission notice shall be included
# in all copies or substantial portions of the Software. THE SOFTWARE IS PROVIDED "AS
# IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED.
PARENT_MESSAGE_TAG = "<parent-message>"
PARENT_MESSAGE_CLOSE_TAG = "</parent-message>"
TO_WORKER_INSTRUCTIONS = {
    "note": "This is a note from the parent, not a question and not a change of assignment. No reply is needed.",
    "ask": ('Answer it now with `claive reply {id} --message "..."` (a short, direct reply), then carry on '
            'with what you were doing. Do not restructure your work around the question. If you cannot run '
            'that command, give the answer in your final message. If you read this after {deadline}, ignore it.'),
}


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


def sender_identity():
    from claivelib import cli

    worker_id = os.environ.get("CLAIVE_WORKER_ID")
    if worker_id:
        state = cli.load(cli.job_path(worker_id), check_alive=False)
        return f"worker {state['id']} ({state.get('engine')}/{state.get('model')})"
    parent = os.environ.get("CLAIVE_PARENT", "").strip() or "unknown harness"
    return f"parent ({parent})"


def delivered_text(record):
    header = f"[Message ID: {record['id']} | from: {record.get('sender', 'parent (unknown harness)')}]"
    instruction = TO_WORKER_INSTRUCTIONS["note"]
    if record.get("kind") == "ask":
        deadline = datetime.datetime.fromtimestamp(record["expires_at"], datetime.timezone.utc)
        deadline_text = deadline.isoformat(timespec="seconds").replace("+00:00", "Z")
        instruction = TO_WORKER_INSTRUCTIONS["ask"].format(id=record["id"], deadline=deadline_text)
    return (f"{PARENT_MESSAGE_TAG}\n{instruction}\n\n{header}\n"
            f"{record['message']}\n{PARENT_MESSAGE_CLOSE_TAG}")


def extract_parent_message(text: str) -> tuple[str, str, str] | None:
    """Extract only one exact envelope with a recognized instruction line."""
    if (not text.startswith(PARENT_MESSAGE_TAG + "\n")
            or not text.endswith("\n" + PARENT_MESSAGE_CLOSE_TAG)
            or text.count(PARENT_MESSAGE_TAG) != 1
            or text.count(PARENT_MESSAGE_CLOSE_TAG) != 1):
        return None
    inner = text[len(PARENT_MESSAGE_TAG) + 1:-len(PARENT_MESSAGE_CLOSE_TAG) - 1]
    instruction, separator, content = inner.partition("\n\n")
    if not separator:
        return None
    header, separator, body = content.partition("\n")
    match = re.fullmatch(r"\[Message ID: ([0-9a-f]{32}) \| from: [^\n]+\]", header)
    if not separator or match is None:
        return None
    identifier = match[1]
    if instruction == TO_WORKER_INSTRUCTIONS["note"]:
        return "note", identifier, body
    pattern = re.escape(TO_WORKER_INSTRUCTIONS["ask"])
    pattern = pattern.replace(re.escape("{id}"), re.escape(identifier))
    pattern = pattern.replace(re.escape("{deadline}"), r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
    if re.fullmatch(pattern, instruction):
        return "ask", identifier, body
    return None


def _pending_record(path, identifier, message, mode, requested_mode, result, metadata,
                    kind="send", expires_at=None):
    created_at = time.time()
    record = dict(id=identifier, mode=mode, requested_mode=requested_mode,
                  status="queued", result=result, created_at=created_at, **metadata)
    message_record = dict(id=identifier, message=message, mode=mode,
                          requested_mode=requested_mode, created_at=created_at,
                          sender=metadata["sender"], kind=kind)
    if expires_at is not None:
        message_record["expires_at"] = expires_at
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


def _queued_result(path, message_file, record):
    """Queue an idle wake when possible; otherwise leave the note for the next turn."""
    from claivelib import turns

    prompt_file = path / "messages" / "wake-prompts" / f"{record['id']}.md"
    message = json.loads(message_file.read_text())
    text = delivered_text(message)
    body = text + "\n"
    try:
        _write_text(prompt_file, body)
        request_id, state = turns.queue(
            path, str(prompt_file), receipt=True, only_if_idle=True,
            message_id=record["id"],
            expires_at=message.get("expires_at"),
            stop_file=(path / "messages" / "asks" / f"{record['id']}.withdrawn"
                       if message.get("kind") == "ask" else None),
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


def send(path, message, mode="note", identifier=None, *, kind="send", expires_at=None):
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
        from claivelib import cli
        state = cli.load(path)
        metadata = dict(sender=sender_identity(), target=state.get("id", path.name),
                        engine=state.get("engine"), model=state.get("model"))

        def prepare_ask():
            if kind == "ask":
                atomic(path / "messages" / "asks" / f"{identifier}.json",
                       dict(id=identifier, target=metadata["target"], sender=metadata["sender"],
                            question=message, created_at=time.time(), deadline=expires_at,
                            delivery={}, delivered_turn=None, status="open", answer=None,
                            answer_source=None, ended_at=None))

        def finish(record):
            if kind == "ask":
                file = path / "messages" / "asks" / f"{identifier}.json"
                ask = json.loads(file.read_text())
                delivery_mode = ("now" if record.get("steering_receipt_id") else
                                 "wake" if record.get("turn_request_id") else "note")
                ask["delivery"] = dict(mode=delivery_mode)
                for key in ("steering_receipt_id", "turn_request_id"):
                    if record.get(key):
                        ask["delivery"][key] = record[key]
                if delivery_mode == "note":
                    ask["delivery"]["note"] = identifier
                if delivery_mode == "now":
                    ask["delivered_turn"] = record.get("turn")
                atomic(file, ask)
            return record, False

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
                              result="submitting mid-turn message", created_at=time.time(), **metadata)
                prepare_ask()
                _store_receipt(path, record)
                try:
                    content = delivered_text(dict(id=identifier, message=message, kind=kind,
                                                  expires_at=expires_at, sender=metadata["sender"]))
                    steer_receipt = steering.submit(path, content)
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
                    return finish(record)

            # Replace any provisional now receipt with the wake result while
            # retaining requested_mode=now for the audit trail.
            _receipt_path(path, identifier).unlink(missing_ok=True)
            state = _worker_for_note_or_wake(path)
            _check_parent(state)
            prepare_ask()
            record, message_file = _pending_record(
                path, identifier, message, "wake", "now", "queued for next turn",
                metadata, kind, expires_at,
            )
            record = _queued_result(path, message_file, record)
            if record.get("turn_request_id"):
                record["result"] = f"fallback to wake: {fallback_reason}; delivered and started a turn"
            else:
                record["result"] = f"fallback to wake: {fallback_reason}; queued for next turn"
            _store_receipt(path, record)
            return finish(record)

        state = _worker_for_note_or_wake(path)
        if mode == "wake":
            _check_parent(state)
        prepare_ask()
        record, message_file = _pending_record(
            path, identifier, message, mode, mode, "queued for next turn",
            metadata, kind, expires_at,
        )
        if mode == "note":
            return finish(record)
        record = _queued_result(path, message_file, record)
        return finish(record)


def _check_parent(state):
    if state.get("needs_parent"):
        raise ValueError("worker is waiting for a parent decision; answer first with claive answer "
                         f"{state['id']} --message ..., or use --mode note")


def withdraw_pending(path, identifier):
    """Called with the message lock held, including by durable expiry cleanup."""
    receipt = _existing_receipt(path, identifier)
    if receipt and receipt.get("status") == "delivered":
        return False
    withdrawn = path / "messages" / "withdrawn"
    for file in (path / "messages" / "pending").glob(f"*-{identifier}.json"):
        withdrawn.mkdir(parents=True, exist_ok=True)
        os.replace(file, withdrawn / file.name)
    if receipt:
        receipt.update(status="withdrawn")
        _store_receipt(path, receipt)
    ask_file = path / "messages" / "asks" / f"{identifier}.json"
    if ask_file.is_file():
        ask = json.loads(ask_file.read_text())
        if ask["status"] == "open":
            ask.update(status="withdrawn", ended_at=time.time())
            atomic(ask_file, ask)
    return True


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
        if record.get("expires_at") is not None and time.time() >= record["expires_at"]:
            withdraw_pending(path, record["id"])
            continue
        messages.append((file, record))
    return messages


def format_block(messages, already_in_prompt=()):
    already_in_prompt = set(already_in_prompt)
    visible = [record for _file, record in messages if record["id"] not in already_in_prompt]
    if not visible:
        return ""
    return "\n\n".join(delivered_text(record) for record in visible)


def mark_delivered(path, messages, turn):
    delivered = path / "messages" / "delivered"
    delivered.mkdir(parents=True, exist_ok=True)
    for file, message in messages:
        receipt_file = _receipt_path(path, message["id"])
        if receipt_file.is_file():
            receipt = json.loads(receipt_file.read_text())
            receipt.update(status="delivered", turn=turn, delivered_at=time.time())
            _store_receipt(path, receipt)
        ask_file = path / "messages" / "asks" / f"{message['id']}.json"
        if ask_file.is_file():
            ask = json.loads(ask_file.read_text())
            ask["delivered_turn"] = turn
            atomic(ask_file, ask)
        os.replace(file, delivered / file.name)
