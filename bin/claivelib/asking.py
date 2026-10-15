"""Deadline-bound questions using the ordinary message delivery path."""
import json
import os
import tempfile
import time
import uuid

from claivelib import ask_schema, messaging, steering
from claivelib.acceptance import atomic


def validate_timeout(timeout):
    if not 0 < timeout <= 3600:
        raise ValueError("--timeout must be greater than 0 and at most 3600 seconds")


def _ask_path(path, identifier):
    messaging.validate_id(identifier)
    return path / "messages" / "asks" / f"{identifier}.json"


def _delivery_turn(path, receipt):
    if receipt.get("steering_receipt_id"):
        steer_receipt = steering.receipt(path, receipt["steering_receipt_id"])
        return steer_receipt.get("turn") if steer_receipt.get("status") == "acknowledged" else None
    if receipt.get("status") == "delivered":
        return receipt.get("turn")
    if receipt.get("turn_request_id"):
        response = path / "responses" / f"{receipt['turn_request_id']}.json"
        if response.is_file():
            return json.loads(response.read_text()).get("turn")
    return None


def _turn_outcome(worker_id, turn):
    from claivelib import cli

    events = cli.root() / "inbox.jsonl"
    outcome = {}
    try:
        with events.open() as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                if (event.get("type") == "turn" and event.get("id") == worker_id
                        and event.get("turn") == turn):
                    outcome = event
    except OSError:
        pass
    return outcome


def _finish(file, record, status, answer=None, source=None):
    record.update(status=status, answer=answer, answer_source=source, ended_at=time.time())
    atomic(file, record)
    return record


def _answer_schema_error(answer, schema):
    if schema is None:
        return None
    try:
        ask_schema.validate_reply(answer, schema)
    except ValueError as error:
        return str(error)
    return None


def _timeout(path, file, record, receipt):
    """Withdraw only requests not claimed for delivery; caller holds the message lock."""
    delivery = record["delivery"]
    withdrawn = record["status"] == "withdrawn"
    if delivery["mode"] == "note":
        withdrawn = messaging.withdraw_pending(path, record["id"])
    elif delivery["mode"] == "wake":
        stop_file = file.with_suffix(".withdrawn")
        messaging._write_text(stop_file, "question deadline passed\n")
        if receipt.get("status") != "delivered" and _delivery_turn(path, receipt) is None:
            withdrawn = messaging.withdraw_pending(path, record["id"])
        request_id = delivery.get("turn_request_id")
        request_remains = (request_id and any((path / "requests").glob(f"*-{request_id}.json")))
        if not request_remains:
            stop_file.unlink(missing_ok=True)
    else:
        steer_receipt = steering.receipt(path, delivery["steering_receipt_id"])
        withdrew_request = steering.withdraw(path, delivery["steering_receipt_id"])
        withdrawn = withdrew_request and steer_receipt.get("status") != "acknowledged"
        if withdrawn:
            receipt["status"] = "withdrawn"
            messaging._store_receipt(path, receipt)
    turn = record.get("delivered_turn") or _delivery_turn(path, receipt)
    record["delivered_turn"] = turn
    status = "withdrawn" if withdrawn else "expired"
    _finish(file, record, status)
    notice = ("withdrawn: question never reached the worker" if withdrawn else
              f"expired: question delivered at turn {turn}, no answer before the deadline")
    return record, 124, notice


def ask(path, question, timeout=120, identifier=None, *, schema=None):
    from claivelib import cli

    validate_timeout(timeout)
    schema_created = False
    if schema is not None:
        if identifier is None:
            identifier = uuid.uuid4().hex
        messaging.validate_id(identifier)
        ask_file = _ask_path(path, identifier)
        stored_schema = ask_schema.schema_file(path, identifier)
        if ask_file.is_file() and not stored_schema.is_file():
            raise ValueError(f"question {identifier} already exists without a reply schema")
        schema_created = ask_schema.store_schema(path, identifier, schema)
    try:
        receipt, _duplicate = messaging.send(path, question, "now", identifier,
                                             kind="ask", expires_at=time.time() + timeout)
    except BaseException:
        if schema_created and not _ask_path(path, identifier).exists():
            ask_schema.schema_file(path, identifier).unlink(missing_ok=True)
        raise
    file = _ask_path(path, receipt["id"])
    if not file.is_file():
        if schema_created:
            ask_schema.schema_file(path, receipt["id"]).unlink(missing_ok=True)
        raise ValueError(f"no question {receipt['id']} for this worker")
    if schema is None:
        schema = ask_schema.schema_for_reply(path, receipt["id"])
    while True:
        with messaging.lock(path):
            record = json.loads(file.read_text())
            receipt = messaging._existing_receipt(path, record["id"])
            answer_file = file.with_name(f"{record['id']}.answer.json")
            if record["status"] == "answered":
                error = _answer_schema_error(record["answer"], schema)
                if error:
                    return record, 1, error
                return record, 0, f"Answer from {record['target']} ({record['answer_source']}):\n{record['answer']}"
            if record["status"] in {"withdrawn", "expired"}:
                notice = ("withdrawn: question never reached the worker" if record["status"] == "withdrawn" else
                          f"expired: question delivered at turn {record['delivered_turn']}, no answer before the deadline")
                return record, 124, notice
            if answer_file.is_file():
                answer = json.loads(answer_file.read_text())
                error = _answer_schema_error(answer["answer"], schema)
                if error:
                    return record, 1, error
                record["delivered_turn"] = answer["turn"]
                _finish(file, record, "answered", answer["answer"], "reply")
                return record, 0, f"Answer from {record['target']} (reply):\n{record['answer']}"

            steering_receipt_id = receipt.get("steering_receipt_id")
            if steering_receipt_id:
                steer_status = steering.receipt(path, steering_receipt_id).get("status")
                if steer_status in {"rejected", "not_delivered", "unconfirmed", "withdrawn"}:
                    record["delivered_turn"] = None
                    _finish(file, record, "withdrawn")
                    return record, 124, "withdrawn: question never reached the worker"

            turn = _delivery_turn(path, receipt)
            state = cli.load(path)
            if turn is not None:
                record["delivered_turn"] = turn
                atomic(file, record)
                ended = state["turn"] > turn or (state["turn"] == turn and state["status"] != "running")
                if ended:
                    response = None
                    request_id = receipt.get("turn_request_id")
                    if request_id:
                        response_file = path / "responses" / f"{request_id}.json"
                        if response_file.is_file():
                            response = json.loads(response_file.read_text())
                    outcome = response or _turn_outcome(state.get("id"), turn)
                    if not outcome:
                        outcome = (state if state["turn"] == turn or
                                   (state["turn"] == turn + 1 and state.get("status") == "running") else {})
                    needs_parent = outcome.get("needs_parent")
                    result = path / "results" / f"turn-{turn:04}.txt"
                    text = result.read_text() if result.is_file() else None
                    status = (outcome.get("last_turn_status", "unknown")
                              if outcome.get("status") in {"idle", "running"} else
                              outcome.get("status", "unknown"))
                    if status == "unknown" and text and text.strip():
                        status = "completed"
                    if needs_parent:
                        question = needs_parent.get("question", "")
                        answer_text = text or question
                        _finish(file, record, "answered", answer_text, "final_text")
                        return record, 3, question
                    if status != "completed":
                        _finish(file, record, "expired")
                        return record, 1, f"turn {turn} ended {status} with no answer"
                    if not text or not text.strip():
                        _finish(file, record, "expired")
                        return record, 1, f"turn {turn} ended {status} with no answer"
                    error = _answer_schema_error(text, schema)
                    if error:
                        _finish(file, record, "expired", text, "final_text")
                        return record, 1, error
                    _finish(file, record, "answered", text, "final_text")
                    return record, 0, f"Answer from {record['target']} (final_text):\n{text}"
            elif receipt.get("turn_request_id"):
                response_file = path / "responses" / f"{receipt['turn_request_id']}.json"
                if response_file.is_file():
                    response = json.loads(response_file.read_text())
                    if response.get("status") == "rejected" and response.get("code") not in {124}:
                        # A stop marker is also a deadline withdrawal, rather than a failed turn.
                        if response.get("error") != "conversation stopped before delivery":
                            messaging.withdraw_pending(path, record["id"])
                            record = json.loads(file.read_text())
                            notice = ((response.get("needs_parent") or {}).get("question", "")
                                      if response.get("code") == 3 else response.get("error", "delivery refused"))
                            return record, response.get("code", 1), notice
            if time.time() >= record["deadline"]:
                return _timeout(path, file, record, receipt)
        time.sleep(0.2)


def reply(path, identifier, message):
    from claivelib import cli

    file = _ask_path(path, identifier)
    with messaging.lock(path):
        if not file.is_file():
            raise ValueError(f"no question {identifier} for this worker")
        record = json.loads(file.read_text())
        status = record["status"]
        if status != "open" or time.time() >= record["deadline"]:
            raise ValueError(f"question {identifier} is {status}; the parent no longer waits")
        messaging.validate_message(message)
        schema = ask_schema.schema_for_reply(path, identifier)
        if schema is not None:
            ask_schema.validate_reply(message, schema)
        answer_file = file.with_name(f"{identifier}.answer.json")
        payload = json.dumps(dict(answer=message, replied_at=time.time(), turn=cli.load(path)["turn"]))
        fd, temporary = tempfile.mkstemp(dir=answer_file.parent, prefix=f".{identifier}.answer-")
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(payload)
            try:
                os.link(temporary, answer_file)
            except FileExistsError:
                raise ValueError(f"question {identifier} already has an answer")
            except OSError as error:
                raise ValueError(f"cannot record the answer: {error}") from error
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
