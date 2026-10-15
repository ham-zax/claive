"""Mask common credential shapes before peek clips or prints provider text."""
import re


REDACTED = "[redacted]"

_SECRET_NAME = r"(?:secret|token|password|passwd|api_key|apikey)"
_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN ([A-Z0-9 ]*PRIVATE KEY)-----[\s\S]*?-----END \1-----", re.IGNORECASE)
_JSON_NAMED_VALUE = re.compile(
    rf'(?P<prefix>"[^"\\]*(?:{_SECRET_NAME})[^"\\]*"\s*:\s*")'
    r'(?P<value>(?:\\.|[^"\\])*)(?P<suffix>")', re.IGNORECASE)
_ASSIGNED_NAMED_VALUE = re.compile(
    rf"(?P<prefix>(?<![\w.-])[\w.-]*(?:{_SECRET_NAME})[\w.-]*\s*=\s*)"
    r'(?P<value>"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[^\s,;&]+)',
    re.IGNORECASE)
_BEARER_TOKEN = re.compile(
    r"\b(Bearer[ \t]+)[A-Za-z0-9_~+/-]+(?:\.[A-Za-z0-9_~+/-]+)*=*", re.IGNORECASE)
_TOKEN_SHAPES = re.compile(
    r"(?<![A-Za-z0-9_])sk-[A-Za-z0-9_-]{16,}(?![A-Za-z0-9_-])"
    r"|(?<![A-Za-z0-9_])(?:ghp_|gho_|ghs_|github_pat_)[A-Za-z0-9_]{16,}(?![A-Za-z0-9_])"
    r"|(?<![A-Za-z0-9])xox[abpr]-[A-Za-z0-9-]{16,}(?![A-Za-z0-9-])"
    r"|(?<![A-Za-z0-9])AKIA[0-9A-Z]{16}(?![0-9A-Z])")


def _should_mask_named_value(value):
    return len(value) >= 8 and not value.isdigit()


def _mask_json_named_value(match):
    value = match.group("value")
    if not _should_mask_named_value(value):
        return match.group(0)
    return match.group("prefix") + REDACTED + match.group("suffix")


def _mask_assigned_value(match):
    value = match.group("value")
    unquoted = value[1:-1] if value[:1] in {'"', "'"} and value[-1:] == value[:1] else value
    if not _should_mask_named_value(unquoted):
        return match.group(0)
    if value[:1] in {'"', "'"} and value[-1:] == value[:1]:
        replacement = value[0] + REDACTED + value[0]
    else:
        replacement = REDACTED
    return match.group("prefix") + replacement


def redact(text: str) -> str:
    """Mask requested token, private-key, and named-assignment shapes in text."""
    text = _PRIVATE_KEY_BLOCK.sub(REDACTED, text)
    text = _JSON_NAMED_VALUE.sub(_mask_json_named_value, text)
    text = _ASSIGNED_NAMED_VALUE.sub(_mask_assigned_value, text)
    text = _BEARER_TOKEN.sub(lambda match: match.group(1) + REDACTED, text)
    return _TOKEN_SHAPES.sub(REDACTED, text)
