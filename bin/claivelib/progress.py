"""Semantic progress extraction and provider-ID-paired action condensation for Claive."""
from collections import deque
import json
import os
from pathlib import Path
import re
import shlex

from claivelib import intent as intent_mod
from claivelib import redaction
from claivelib.ansi import strip_controls

MAX_COMMAND_CHARS = 2000
MAX_OUTPUT_CHARS = 2400
MAX_SUMMARY_CHARS = 140
MAX_TARGET_CHARS = 72
MAX_OPEN_CALLS = 64

_EDIT_TOOLS = {
    "edit", "write", "create", "replace", "patch", "apply_patch", "file_change",
    "write_to_file", "replace_file_content", "multi_replace_file_content",
    "dev_edit", "dev_write", "edit_file", "write_file",
}
_READ_TOOLS = {
    "read", "view", "read_file", "view_file", "list_directory", "list_dir",
    "dev_read", "dev_list_directory", "ls", "cat",
}
_SEARCH_TOOLS = {
    "search", "grep", "rg", "find", "grep_search", "find_by_name", "dev_search",
    "glob", "web_search", "websearch",
}
_SHELL_TOOLS = {
    "shell", "bash", "exec", "command_execution", "run_command", "terminal",
    "dev_exec", "dev_bash",
}

_TEST_PATTERNS = (
    "pytest", "unittest", "vitest", "jest", "mocha", "cargo test", "go test",
    "npm test", "pnpm test", "yarn test", "just test", "just dash-test",
    "node --test", "node --import tsx --test", "tsx --test", "rspec",
)
_BUILD_PATTERNS = (
    "cargo build", "cargo check", "cargo clippy", "tsc", "oxlint", "eslint",
    "ruff", "shellcheck", "mypy", "pyright", "pnpm run check", "npm run check",
    "just check", "just lint", "just dash-build", " build", " lint", "compileall",
)
_HEREDOC_FILE_RE = re.compile(
    r"""(?:open|Path|readFileSync|writeFileSync|read_text|write_text)\(\s*['"]([^'"\n]{2,120})['"]"""
    r"""|(?:^|\s|;)p\s*=\s*['"]([^'"\n]{2,120})['"]"""
)
_SUMMARY_RESULT_RE = re.compile(
    r"\b(\d+\s+(?:passed|failed|ok|tests?|errors?|warnings?)|all checks passed|finished|built in|successfully)\b",
    re.IGNORECASE,
)


def _clean_line(text: str, limit: int = MAX_SUMMARY_CHARS) -> str:
    cleaned = " ".join(strip_controls(str(text or "")).split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(1, limit - 1)].rstrip() + "…"


def _clip_multiline(text: str, limit: int) -> tuple[str, bool]:
    cleaned = strip_controls(str(text or "").replace("\r\n", "\n").replace("\r", "\n")).strip()
    if len(cleaned) <= limit:
        return cleaned, False
    head = limit * 2 // 3
    tail = limit - head
    elided = len(cleaned) - limit
    return f"{cleaned[:head].rstrip()}\n[… {elided} chars elided …]\n{cleaned[-tail:].lstrip()}", True


def _shorten_path(raw: str, workspace: str | None = None) -> str:
    candidate = str(raw or "").strip().strip("'\"")
    if not candidate:
        return ""
    if workspace and os.path.isabs(candidate):
        try:
            rel = Path(candidate).resolve(strict=False).relative_to(Path(workspace).resolve(strict=False))
            candidate = rel.as_posix()
        except (OSError, ValueError):
            pass
    if len(candidate) <= MAX_TARGET_CHARS:
        return candidate
    parts = [p for p in candidate.split("/") if p]
    if len(parts) >= 3:
        short = ".../" + "/".join(parts[-3:])
        if len(short) <= MAX_TARGET_CHARS:
            return short
    return "…" + candidate[-(MAX_TARGET_CHARS - 1):]


def unwrap_shell(raw: str | None) -> str:
    """Strip outer `/bin/bash -lc '...'` or `sh -c ...` wrappers cleanly."""
    if not isinstance(raw, str):
        return ""
    s = raw.strip()
    for prefix in ("/bin/bash -lc ", "/usr/bin/bash -lc ", "bash -lc ", "/bin/sh -c ", "sh -c "):
        if s.startswith(prefix):
            s = s[len(prefix):].strip()
            break
    if len(s) >= 2 and s[0] == s[-1] and s[0] in {"'", '"'}:
        quote = s[0]
        inner = s[1:-1]
        if quote == '"':
            inner = inner.replace('\\"', '"').replace("\\n", "\n").replace("\\t", "\t")
        return inner.strip()
    # Handle truncated/unclosed leading quote cleanly (avoid `"python3 - <<'PY'`).
    if s and s[0] in {"'", '"'}:
        quote = s[0]
        if s.count(quote) == 1 or (quote == '"' and s.endswith("\\")):
            s = s[1:]
    return s.strip()


def _extract_shell_target(cmd: str, workspace: str | None = None) -> tuple[str, str]:
    """Return (display_summary, primary_target) from an unwrapped shell command."""
    if not cmd:
        return "", ""
    first_line = next((ln.strip() for ln in cmd.splitlines() if ln.strip()), cmd.strip())

    # Inline Python/Node heredoc scripts: find literal file target inside body if any.
    if re.search(r"\b(python3?|node)\b.*<<", first_line):
        lang = "Node" if re.search(r"\bnode\b", first_line) else "Python"
        matches = []
        for m in _HEREDOC_FILE_RE.finditer(cmd[:2000]):
            found = m.group(1) or m.group(2)
            if found and found not in matches:
                matches.append(found)
            if len(matches) >= 2:
                break
        if matches:
            target = _shorten_path(matches[0], workspace)
            extra = f" (+{len(matches) - 1})" if len(matches) > 1 else ""
            return f"inline {lang}: {target}{extra}", target
        return f"inline {lang}", ""

    # Tokenize first segment (before && / ; / |) to spot file operands.
    head = re.split(r"\s*(?:&&|\|\||;|\|)\s*", first_line, maxsplit=1)[0].strip()
    try:
        tokens = shlex.split(head)
    except ValueError:
        tokens = head.split()
    if not tokens:
        return _clean_line(first_line), ""

    prog = os.path.basename(tokens[0]).lower()
    if prog in {"sed", "cat", "head", "tail", "nl", "wc"}:
        files = [t for t in tokens[1:] if not t.startswith("-") and ("/" in t or "." in t)]
        if files:
            target = _shorten_path(files[-1], workspace)
            return f"{prog} {target}", target
    if prog == "git" and len(tokens) >= 2:
        sub = tokens[1]
        if "--" in tokens:
            idx = tokens.index("--")
            paths = [t for t in tokens[idx + 1:] if t and not t.startswith("-")]
            if paths:
                target = _shorten_path(paths[0], workspace)
                suffix = f" (+{len(paths) - 1})" if len(paths) > 1 else ""
                return f"git {sub} {target}{suffix}", target
        return _clean_line(first_line), ""
    if prog in {"rg", "grep"}:
        non_flags = [t for t in tokens[1:] if not t.startswith("-")]
        if len(non_flags) >= 2:
            target = _shorten_path(non_flags[-1], workspace)
            return f"{prog} {non_flags[0]} in {target}", target
        if non_flags:
            return f"{prog} {non_flags[0]}", ""

    # Test / build runners: find package filter or test file target.
    target = ""
    for i, tok in enumerate(tokens[1:], start=1):
        if tok in {"--filter", "-p", "--package", "-C"} and i + 1 < len(tokens):
            target = _shorten_path(tokens[i + 1], workspace)
            break
        if (not tok.startswith("-")) and ("/" in tok or tok.endswith((".py", ".rs", ".ts", ".tsx", ".js", ".mjs", ".go"))):
            target = _shorten_path(tok, workspace)
            break

    return _clean_line(first_line), target


def classify_tool_action(
    tool_name: str,
    command: str | None = None,
    path: str | None = None,
    args: dict | None = None,
    workspace: str | None = None,
) -> dict:
    """Classify a tool invocation into human-readable (stage, category, target, summary)."""
    tool = str(tool_name or "tool").strip()
    tool_low = tool.lower()
    args = args if isinstance(args, dict) else {}

    # Check explicit structured path first.
    raw_path = path
    if not raw_path:
        for key in ("path", "file_path", "target_file", "Path", "TargetFile", "AbsolutePath"):
            val = args.get(key)
            if isinstance(val, str) and val.strip():
                raw_path = val.strip()
                break

    # Shell command classification.
    raw_cmd = command
    if not raw_cmd:
        for key in ("command", "CommandLine"):
            val = args.get(key)
            if isinstance(val, str) and val.strip():
                raw_cmd = val.strip()
                break

    if raw_cmd or tool_low in _SHELL_TOOLS:
        unwrapped = unwrap_shell(raw_cmd or (tool if " " in tool else ""))
        cmd_low = unwrapped.lower()
        summary, target = _extract_shell_target(unwrapped, workspace)
        if not target and raw_path:
            target = _shorten_path(raw_path, workspace)

        if any(pat in cmd_low for pat in _TEST_PATTERNS):
            stage, category, verb = "testing", "test", "Test"
        elif any(pat in cmd_low for pat in _BUILD_PATTERNS):
            stage, category, verb = "building", "build", "Build"
        elif re.match(r"^(sed|cat|head|tail|ls|find|rg|grep|git\s+(status|diff|log|show))\b", cmd_low):
            stage, category, verb = "exploring", "read", "Inspect"
        elif "inline " in summary and any(k in cmd_low for k in (".write(", "write_text", "writefilesync", "replace(")):
            stage, category, verb = "editing", "edit", "Edit"
        else:
            stage, category, verb = "running", "run", "Run"

        label = f"{verb} {target or summary}" if (target or summary) else f"{verb} command"
        return {
            "stage": stage,
            "category": category,
            "target": target,
            "summary": _clean_line(summary or unwrapped or tool),
            "label": _clean_line(label, 80),
            "command": unwrapped,
        }

    target = _shorten_path(raw_path, workspace) if raw_path else ""
    if tool_low in _EDIT_TOOLS or any(k in tool_low for k in ("edit", "write", "patch", "replace")):
        extra = ""
        edits = args.get("edits")
        if isinstance(edits, list) and edits:
            extra = f" ({len(edits)} edit{'s' if len(edits) != 1 else ''})"
        elif tool_low == "file_change" and isinstance(args.get("changes"), list):
            changes = args["changes"]
            if changes and isinstance(changes[0], dict) and isinstance(changes[0].get("path"), str):
                target = _shorten_path(changes[0]["path"], workspace)
                if len(changes) > 1:
                    extra = f" (+{len(changes) - 1} files)"
        summary = f"{target or tool}{extra}"
        return {
            "stage": "editing",
            "category": "edit",
            "target": target,
            "summary": _clean_line(summary),
            "label": _clean_line(f"Edit {target or tool}", 80),
            "command": None,
        }

    if tool_low in _READ_TOOLS or "read" in tool_low or "list" in tool_low:
        return {
            "stage": "exploring",
            "category": "read",
            "target": target,
            "summary": _clean_line(target or tool),
            "label": _clean_line(f"Read {target or tool}", 80),
            "command": None,
        }

    if tool_low in _SEARCH_TOOLS or "search" in tool_low or "grep" in tool_low:
        pattern = args.get("pattern") or args.get("query") or ""
        pat_str = _clean_line(str(pattern), 48) if pattern else ""
        summary = f"/{pat_str}/ in {target}" if (pat_str and target) else (pat_str or target or tool)
        return {
            "stage": "exploring",
            "category": "search",
            "target": target or pat_str,
            "summary": _clean_line(summary),
            "label": _clean_line(f"Find {pat_str or target or tool}", 80),
            "command": None,
        }

    return {
        "stage": "running",
        "category": "tool",
        "target": target,
        "summary": _clean_line(f"{tool} {target}".strip()),
        "label": _clean_line(f"{tool} {target}".strip(), 80),
        "command": None,
    }


def summarize_output(output: str | None, exit_code: int | None = None, is_error: bool = False) -> str:
    """Pick the single highest-signal outcome line from tool output."""
    text = strip_controls(str(output or "").replace("\r\n", "\n").replace("\r", "\n"))
    lines = [" ".join(ln.split()) for ln in text.splitlines() if ln.strip()]
    lines = [ln for ln in lines if not ln.startswith("[…") and not ln.startswith("[...")]
    non_banner = [ln for ln in lines if not (ln.startswith("> ") and len(ln) > 2)]
    if non_banner:
        lines = non_banner
    elif lines and not is_error and exit_code in (None, 0):
        return "ok"

    if is_error or (exit_code is not None and exit_code != 0):
        prefix = f"exit {exit_code}: " if exit_code not in (None, 0) else ""
        for ln in reversed(lines):
            low = ln.lower()
            if any(k in low for k in ("error", "failed", "assertion", "exception", "traceback", "exit status")):
                return _clean_line(f"{prefix}{ln}", 100)
        if lines:
            return _clean_line(f"{prefix}{lines[-1]}", 100)
        return f"exit {exit_code}" if exit_code not in (None, 0) else "failed"

    for ln in reversed(lines[-6:]):
        if _SUMMARY_RESULT_RE.search(ln):
            return _clean_line(ln, 100)
    if len(lines) == 1:
        return _clean_line(lines[0], 100)
    if lines:
        return _clean_line(f"{lines[0]} ({len(lines)} lines)", 100)
    return "done"


def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(
        block["text"] for block in content
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
    )


def _record_milestone(items: list[dict], next_id: int, kind: str, text: str) -> int:
    redacted = redaction.redact(str(text or "")).strip()
    if not redacted:
        return next_id
    # Collapse duplicate adjacent assistant narratives (common on AGY final result).
    if items and items[-1]["kind"] == kind and items[-1].get("detail") == redacted[:MAX_OUTPUT_CHARS]:
        return next_id
    headline = intent_mod.intent_from_steps([{"kind": "text", "text": redacted}]) or _clean_line(redacted)
    detail, truncated = _clip_multiline(redacted, MAX_OUTPUT_CHARS)
    items.append({
        "id": next_id,
        "kind": kind,
        "category": "say" if kind == "say" else "err",
        "stage": "blocked" if kind == "error" else "narrative",
        "tool": None,
        "target": "",
        "summary": _clean_line(headline),
        "outcome": None,
        "status": "error" if kind == "error" else "done",
        "exit_code": None,
        "command": None,
        "output": detail,
        "detail": detail,
        "truncated": truncated,
    })
    return next_id + 1


def extract_structured_actions(
    events_path: Path,
    engine_name: str,
    count: int = 40,
    workspace: str | None = None,
) -> list[dict]:
    """Stream `events.jsonl` and emit provider-ID-paired Actions + Milestones before clipping."""
    try:
        stream = events_path.open("rb")
    except FileNotFoundError:
        return []

    items: list[dict] = []
    open_calls: dict[str, dict] = {}
    agy_text_buf: dict[str, list[str]] = {}
    agy_text_order: list[str] = []
    next_id = 1

    def flush_agy_text(step_key: str | None = None):
        nonlocal next_id
        keys = [step_key] if step_key is not None else list(agy_text_order)
        for k in keys:
            chunks = agy_text_buf.pop(k, None)
            if k in agy_text_order:
                agy_text_order.remove(k)
            if chunks:
                next_id = _record_milestone(items, next_id, "say", "".join(chunks))

    def start_call(call_id: str | None, tool: str, command=None, path=None, args=None):
        nonlocal next_id
        info = classify_tool_action(tool, command=command, path=path, args=args, workspace=workspace)
        cmd_redacted = redaction.redact(info["command"]) if info["command"] else None
        cmd_clipped, cmd_trunc = _clip_multiline(cmd_redacted, MAX_COMMAND_CHARS) if cmd_redacted else (None, False)
        action = {
            "id": next_id,
            "kind": "action",
            "category": info["category"],
            "stage": info["stage"],
            "tool": tool,
            "target": info["target"],
            "summary": redaction.redact(info["summary"]),
            "outcome": None,
            "status": "running",
            "exit_code": None,
            "command": cmd_clipped,
            "output": None,
            "truncated": cmd_trunc,
        }
        next_id += 1
        items.append(action)
        if call_id:
            if len(open_calls) >= MAX_OPEN_CALLS:
                open_calls.pop(next(iter(open_calls)))
            open_calls[call_id] = action
        return action

    def finish_call(call_id: str | None, *, tool: str = "tool", command=None, path=None,
                    args=None, output: str = "", exit_code: int | None = None, is_error: bool = False):
        action = open_calls.pop(call_id, None) if call_id else None
        if action is None:
            action = start_call(None, tool, command=command, path=path, args=args)
        elif command and not action.get("command"):
            info = classify_tool_action(tool, command=command, path=path, args=args, workspace=workspace)
            cmd_redacted = redaction.redact(info["command"]) if info["command"] else None
            if cmd_redacted:
                action["command"], cmd_trunc = _clip_multiline(cmd_redacted, MAX_COMMAND_CHARS)
                action["truncated"] = action["truncated"] or cmd_trunc
        redacted_out = redaction.redact(output) if output else ""
        out_clipped, out_trunc = _clip_multiline(redacted_out, MAX_OUTPUT_CHARS) if redacted_out else ("", False)
        failed = bool(is_error) or (exit_code is not None and exit_code != 0)
        action["status"] = "error" if failed else "ok"
        action["exit_code"] = exit_code
        action["outcome"] = summarize_output(redacted_out, exit_code=exit_code, is_error=failed)
        action["output"] = out_clipped or None
        action["truncated"] = bool(action.get("truncated") or out_trunc)
        return action

    eng = (engine_name or "").lower()
    with stream:
        for raw in stream:
            if not raw.endswith(b"\n"):
                break
            try:
                event = json.loads(raw)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(event, dict):
                continue

            if eng == "codex":
                etype = event.get("type")
                item = event.get("item") if isinstance(event.get("item"), dict) else None
                if item:
                    itype = item.get("type")
                    iid = item.get("id") if isinstance(item.get("id"), str) else None
                    if etype == "item.completed" and itype == "agent_message" and isinstance(item.get("text"), str):
                        next_id = _record_milestone(items, next_id, "say", item["text"])
                    elif etype == "item.completed" and itype == "error":
                        next_id = _record_milestone(items, next_id, "error", str(item.get("message") or "Codex error"))
                    elif itype in {"command_execution", "mcp_tool_call", "file_change"}:
                        tool = "shell" if itype == "command_execution" else str(item.get("tool") or itype)
                        cmd = item.get("command") if isinstance(item.get("command"), str) else None
                        if etype == "item.started":
                            start_call(iid, tool, command=cmd, args=item)
                        elif etype == "item.completed":
                            exit_code = item.get("exit_code") if isinstance(item.get("exit_code"), int) else None
                            status_val = str(item.get("status") or "").lower()
                            is_err = (exit_code not in (None, 0)) or status_val in {"failed", "error"}
                            out_text = item.get("aggregated_output") or item.get("output") or ""
                            if not out_text and itype == "file_change" and isinstance(item.get("changes"), list):
                                out_text = "\n".join(
                                    f"{c.get('kind', 'change')}: {c.get('path', '')}"
                                    for c in item["changes"] if isinstance(c, dict)
                                )
                            finish_call(iid, tool=tool, command=cmd, args=item,
                                        output=str(out_text), exit_code=exit_code, is_error=is_err)
                elif etype == "turn.failed":
                    err = event.get("error")
                    msg = err.get("message") if isinstance(err, dict) else str(err or "Codex turn failed")
                    next_id = _record_milestone(items, next_id, "error", str(msg))

            elif eng == "pi":
                etype = event.get("type")
                if etype == "tool_execution_start":
                    args = event.get("args") if isinstance(event.get("args"), dict) else {}
                    cid = event.get("toolCallId") if isinstance(event.get("toolCallId"), str) else None
                    start_call(cid, str(event.get("toolName") or "tool"),
                               command=args.get("command"), path=args.get("path"), args=args)
                elif etype == "tool_execution_end":
                    cid = event.get("toolCallId") if isinstance(event.get("toolCallId"), str) else None
                    res = event.get("result")
                    out_text = _content_text(res.get("content")) if isinstance(res, dict) else ""
                    args = event.get("args") if isinstance(event.get("args"), dict) else {}
                    finish_call(cid, tool=str(event.get("toolName") or "tool"),
                                command=args.get("command"), path=args.get("path"), args=args,
                                output=out_text, is_error=bool(event.get("isError")))
                elif etype == "message_end":
                    msg = event.get("message")
                    if isinstance(msg, dict) and msg.get("role") == "assistant":
                        if msg.get("stopReason") in {"error", "aborted"}:
                            next_id = _record_milestone(items, next_id, "error", str(msg.get("errorMessage") or msg["stopReason"]))
                        else:
                            text = _content_text(msg.get("content"))
                            if text:
                                next_id = _record_milestone(items, next_id, "say", text)
                elif etype == "response" and event.get("success") is False:
                    next_id = _record_milestone(items, next_id, "error", str(event.get("error") or "Pi prompt rejected"))

            elif eng == "claude":
                etype = event.get("type")
                if etype in {"assistant", "user"}:
                    if etype == "user" and event.get("isReplay"):
                        continue
                    msg = event.get("message")
                    blocks = msg.get("content") if isinstance(msg, dict) else None
                    if isinstance(blocks, list):
                        for block in blocks:
                            if not isinstance(block, dict):
                                continue
                            btype = block.get("type")
                            if etype == "assistant" and btype == "text":
                                next_id = _record_milestone(items, next_id, "say", _content_text([block]))
                            elif etype == "assistant" and btype == "tool_use":
                                inp = block.get("input") if isinstance(block.get("input"), dict) else {}
                                cid = block.get("id") if isinstance(block.get("id"), str) else None
                                start_call(cid, str(block.get("name") or "tool"),
                                           command=inp.get("command"),
                                           path=inp.get("file_path") or inp.get("path"), args=inp)
                            elif etype == "user" and btype == "tool_result":
                                cid = block.get("tool_use_id") if isinstance(block.get("tool_use_id"), str) else None
                                out_text = _content_text(block.get("content"))
                                finish_call(cid, output=out_text, is_error=bool(block.get("is_error")))
                elif etype == "result" and (event.get("subtype") != "success" or event.get("is_error")):
                    next_id = _record_milestone(items, next_id, "error", str(event.get("result") or event.get("subtype") or "Claude failed"))

            elif eng == "agy":
                ekind = event.get("event")
                body = event.get(ekind) if isinstance(ekind, str) else None
                if not isinstance(body, dict):
                    continue
                if ekind == "step_update":
                    conv_id = body.get("conversation_id") or ""
                    step_idx = body.get("step_index")
                    skey = f"{conv_id}:{step_idx}"
                    if body.get("text_delta"):
                        if skey not in agy_text_buf:
                            agy_text_buf[skey] = []
                            agy_text_order.append(skey)
                        agy_text_buf[skey].append(str(body["text_delta"]))
                    if body.get("step_type") == "agent_response" and body.get("state") == "DONE":
                        flush_agy_text(skey)
                    if body.get("tool_name"):
                        flush_agy_text()
                        info = body.get("tool_info")
                        params = info.get("parameters") if isinstance(info, dict) and isinstance(info.get("parameters"), dict) else {}
                        cmd = params.get("CommandLine") or params.get("command")
                        path = params.get("Path") or params.get("TargetFile") or params.get("AbsolutePath") or params.get("file_path")
                        if body.get("state") == "ACTIVE":
                            start_call(skey, str(body["tool_name"]), command=cmd, path=path, args=params)
                        elif body.get("state") == "DONE":
                            out_text = ""
                            if isinstance(info, dict):
                                out_text = str(info.get("result") or info.get("output") or "")
                            finish_call(skey, tool=str(body["tool_name"]), command=cmd, path=path, args=params, output=out_text)
                elif ekind == "result":
                    flush_agy_text()
                    if body.get("status") == "ERROR":
                        next_id = _record_milestone(items, next_id, "error", str(body.get("finished_reason") or "AGY failed"))
                    elif body.get("response"):
                        next_id = _record_milestone(items, next_id, "say", str(body["response"]))

    flush_agy_text()
    # Clean internal helper field before returning.
    trimmed = deque(items, maxlen=max(1, count))
    result = []
    for item in trimmed:
        clean_item = {k: v for k, v in item.items() if k != "detail"}
        result.append(clean_item)
    return result
