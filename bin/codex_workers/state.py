"""Worker state schema helpers."""
from copy import deepcopy

SCHEMA_VERSION = 2


def session_id(record):
    return record.get("session_id") or record.get("muse_session_id")


def launch_config(record):
    if record.get("schema_version") == SCHEMA_VERSION and isinstance(record.get("launch"), dict):
        return deepcopy(record["launch"])
    from .compat.v1_muse import decode_launch
    return decode_launch(record)


def normalized(record):
    result = deepcopy(record)
    if result.get("schema_version") == SCHEMA_VERSION:
        return result
    from .compat.v1_muse import decode_session_id
    result["engine"] = result.get("engine") or "muse"
    result["session_id"] = decode_session_id(result)
    result["launch"] = launch_config(result)
    result["source_schema_version"] = result.get("schema_version", 1)
    return result
