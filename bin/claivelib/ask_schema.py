"""Validation for the small JSON schema accepted by ``claive ask --schema``."""
import json
import math
import os
from pathlib import Path

from claivelib import messaging


_SCHEMA_KEYS = {"type", "required", "properties", "enum", "items"}
_TYPES = {"object", "array", "string", "number", "integer", "boolean", "null"}


def _reject_constant(value):
    raise ValueError(f"{value} is not a valid JSON value")


def _type_list(value, where):
    if isinstance(value, str):
        types = [value]
    elif isinstance(value, list) and value:
        types = value
    else:
        raise ValueError(f"{where}.type must be a JSON type name or a non-empty list of type names")
    if any(not isinstance(item, str) or item not in _TYPES for item in types):
        choices = ", ".join(sorted(_TYPES))
        raise ValueError(f"{where}.type entries must be unique names from: {choices}")
    if len(set(types)) != len(types):
        raise ValueError(f"{where}.type entries must be unique names")
    return types


def validate_schema(schema, where="$schema"):
    """Reject unsupported or malformed schema definitions before delivery."""
    if not isinstance(schema, dict):
        raise ValueError(f"{where} must be a JSON object")
    unknown = set(schema) - _SCHEMA_KEYS
    if unknown:
        raise ValueError(f"{where} has unsupported keyword(s): {', '.join(sorted(unknown))}")
    if "type" in schema:
        _type_list(schema["type"], where)
    if "required" in schema:
        required = schema["required"]
        if (not isinstance(required, list)
                or any(not isinstance(name, str) or not name for name in required)):
            raise ValueError(f"{where}.required must be a list of unique non-empty property names")
        if len(set(required)) != len(required):
            raise ValueError(f"{where}.required must be a list of unique non-empty property names")
    if "properties" in schema:
        properties = schema["properties"]
        if not isinstance(properties, dict) or any(not isinstance(name, str) for name in properties):
            raise ValueError(f"{where}.properties must be an object whose values are schemas")
        for name, child in properties.items():
            validate_schema(child, f"{where}.properties.{name}")
    if "enum" in schema:
        if not isinstance(schema["enum"], list) or not schema["enum"]:
            raise ValueError(f"{where}.enum must be a non-empty list")
    if "items" in schema:
        validate_schema(schema["items"], f"{where}.items")
    return schema


def load_schema(filename):
    """Read and validate an absolute schema path supplied by the parent."""
    path = Path(filename)
    if not path.is_absolute() or not path.is_file():
        raise ValueError("--schema must be an existing absolute file")
    try:
        schema = json.loads(path.read_text(), parse_constant=_reject_constant)
    except (json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"--schema is not valid JSON: {error}") from error
    return validate_schema(schema)


def schema_file(path, identifier):
    messaging.validate_id(identifier)
    return path / "messages" / "asks" / f"{identifier}.schema.json"


def store_schema(path, identifier, schema):
    """Persist the schema before ask delivery; return True when newly created."""
    validate_schema(schema)
    file = schema_file(path, identifier)
    file.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(schema, sort_keys=True, separators=(",", ":"))
    try:
        fd = os.open(file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        existing = load_stored_schema(file)
        if json.dumps(existing, sort_keys=True, separators=(",", ":")) != serialized:
            raise ValueError(f"question {identifier} already has a different reply schema")
        return False
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(serialized)
    except BaseException:
        file.unlink(missing_ok=True)
        raise
    return True


def load_stored_schema(file):
    try:
        schema = json.loads(file.read_text(), parse_constant=_reject_constant)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"stored reply schema is invalid: {error}") from error
    return validate_schema(schema)


def schema_for_reply(path, identifier):
    file = schema_file(path, identifier)
    return load_stored_schema(file) if file.is_file() else None


def _json_type(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "integer" if math.isfinite(value) and value.is_integer() else "number"
    return type(value).__name__


def _matches_type(value, type_name):
    actual = _json_type(value)
    if type_name == "number":
        return actual in {"integer", "number"}
    return actual == type_name


def _json_equal(left, right):
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, list):
        return len(left) == len(right) and all(_json_equal(a, b) for a, b in zip(left, right))
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_json_equal(left[key], right[key]) for key in left)
    return left == right


def _validate_value(value, schema, where="$"):
    if "type" in schema:
        allowed = _type_list(schema["type"], "$schema")
        if not any(_matches_type(value, expected) for expected in allowed):
            expected = " or ".join(allowed)
            raise ValueError(f"{where}: expected {expected}, got {_json_type(value)}")
    if "enum" in schema and not any(_json_equal(value, item) for item in schema["enum"]):
        raise ValueError(f"{where}: value is not one of the allowed enum values")
    if isinstance(value, dict):
        for name in schema.get("required", []):
            if name not in value:
                raise ValueError(f"{where}.{name}: required property is missing")
        for name, child in schema.get("properties", {}).items():
            if name in value:
                _validate_value(value[name], child, f"{where}.{name}")
    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            _validate_value(item, schema["items"], f"{where}[{index}]")


def validate_reply(text, schema):
    """Require text to be JSON matching the supported schema subset."""
    try:
        value = json.loads(text, parse_constant=_reject_constant)
    except (json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"reply must be valid JSON: {error}") from error
    _validate_value(value, schema)
    return value
