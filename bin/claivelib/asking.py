"""Deadline-bound questions using the ordinary message delivery path."""
import json
import os
import time

from claivelib import messaging, steering
from claivelib.acceptance import atomic


def validate_timeout(timeout):
    if not 0 < timeout <= 3600:
        raise ValueError("--timeout must be greater than 0 and at most 3600 seconds")


def _ask_path(path, identifier):
    messaging.validate_id(identifier)
    return path / "messages" / "asks" / f"{identifier}.json"


def _delivery_turn(path, receipt):
    if receipt.get("steering_receipt_id"):
        return steering.receipt(path, receipt["steering_receipt_id"]).get("turn")
    if receipt.get("status") == "delivered":
        return receipt.get("turn")
    if receipt.get("turn_request_id"):
        response = path / "responses" / f"{receipt['turn_request_id']}.json"
        if response.is_file():
            return json.loads(response.read_text()).get("turn")
    return None


def _finish(file, record, status, answer=None, source=None):
    record.update(status=status, answer=answer, answer_source=source, ended_at=time.time())
    atomic(file, record)
    return record


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
    else:
        withdrawn = steering.withdraw(path, delivery["steering_receipt_id"])
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


def ask(path, question, timeout=120, identifier=None):
    from claivelib import cli

    validate_timeout(timeout)
    receipt, _duplicate = messaging.send(path, question, "now", identifier,
                                         kind="ask", expires_at=time.time() + timeout)
    file = _ask_path(path, receipt["id"])
    if not file.is_file():
        raise ValueError(f"no question {receipt['id']} for this worker")
    while True:
        with messaging.lock(path):
            record = json.loads(file.read_text())
            receipt = messaging._existing_receipt(path, record["id"])
            answer_file = file.with_name(f"{record['id']}.answer.json")
            if record["status"] == "answered":
                return record, 0, f"Answer from {record['target']} ({record['answer_source']}):\n{record['answer']}"
            if record["status"] in {"withdrawn", "expired"}:
                notice = ("withdrawn: question never reached the worker" if record["status"] == "withdrawn" else
                          f"expired: question delivered at turn {record['delivered_turn']}, no answer before the deadline")
                return record, 124, notice
            if answer_file.is_file():
                answer = json.loads(answer_file.read_text())
                record["delivered_turn"] = answer["turn"]
                _finish(file, record, "answered", answer["answer"], "reply")
                return record, 0, f"Answer from {record['target']} (reply):\n{record['answer']}"

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
                    outcome = response or (state if state["turn"] == turn else {})
                    needs_parent = outcome.get("needs_parent")
                    result = path / "results" / f"turn-{turn:04}.txt"
                    text = result.read_text() if result.is_file() else None
                    if needs_parent:
                        question = needs_parent.get("question", "")
                        _finish(file, record, "answered", text or question, "final_text")
                        return record, 3, question
                    status = outcome.get("last_turn_status") if outcome.get("status") == "idle" else outcome.get("status")
                    if status in {"failed", "cancelled", "interrupted"} and not text:
                        _finish(file, record, "expired")
                        return record, 1, f"turn {turn} ended {status} with no answer"
                    if text is not None:
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
        answer_file = file.with_name(f"{identifier}.answer.json")
        try:
            fd = os.open(answer_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise ValueError(f"question {identifier} already has an answer")
        with os.fdopen(fd, "w") as stream:
            json.dump(dict(answer=message, replied_at=time.time(), turn=cli.load(path)["turn"]), stream)
