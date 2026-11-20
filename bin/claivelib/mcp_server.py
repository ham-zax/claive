"""Flat-schema MCP stdio server for zero-shell claive orchestration."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

from claivelib import cli, inbox, peek, status_file
from claivelib.engines import DEFAULT_ENGINE, get_engine

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "claive-mcp", "version": "1.0.0"}

FORBIDDEN_SCHEMA_KEYS = frozenset({
    "anyOf",
    "oneOf",
    "allOf",
    "not",
    "$ref",
    "$defs",
    "additionalProperties",
    "minLength",
    "maxLength",
    "minimum",
    "maximum",
    "pattern",
})

SCALAR_TYPES = frozenset({"string", "integer", "number", "boolean"})

TOOL_DEFINITIONS = [
    {
        "name": "claive_start",
        "description": "Start a detached claive worker turn and return its initial status summary.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace": {"type": "string", "description": "Absolute path to the target Git workspace."},
                "prompt": {"type": "string", "description": "Task prompt text for the worker."},
                "engine": {"type": "string", "description": "Optional engine name (muse, pi, claude, codex, agy)."},
                "label": {"type": "string", "description": "Short human label for the worker."},
                "alias": {"type": "string", "description": "Optional @alias name to bind."},
                "model": {"type": "string", "description": "Optional model override (or provider/model on pi)."},
                "provider": {"type": "string", "description": "Optional provider override."},
                "reasoning_effort": {"type": "string", "description": "Optional reasoning effort level."},
                "read_only": {"type": "boolean", "description": "Run in read-only mode."},
                "worktree": {"type": "boolean", "description": "Run inside an isolated managed Git worktree."},
                "carry_dirty": {"type": "boolean", "description": "Copy uncommitted parent edits into --worktree at HEAD."},
                "link_deps": {"type": "boolean", "description": "Symlink existing .venv and node_modules into --worktree."},
                "reuse_alias": {"type": "boolean", "description": "Automatically rebind @alias when prior worker is settled and accepted."},
                "parent_session": {"type": "string", "description": "Optional parent session token for scoped inbox and watcher tracking."},
                "park": {"type": "boolean", "description": "Mark worker as intentionally parked (exempt from Stop-hook orphan block)."},
                "notify_worker": {"type": "string", "description": "Optional reusable @worker address to wake when this worker settles."},
                "enable_steering": {"type": "boolean", "description": "Enable live mid-turn steering transport."},
                "report": {"type": "boolean", "description": "Require a structured claive-report block."},
                "no_fallback": {"type": "boolean", "description": "Disable configured fallback_models for this worker."},
                "verify_in_worktree": {"type": "boolean", "description": "Run verification checks directly in the worktree."},
                "turn_timeout": {"type": "number", "description": "Wall-clock turn timeout in seconds."},
                "tool_idle_timeout": {"type": "number", "description": "Kill silent active tool children after N seconds."},
                "check": {"type": "string", "description": "Optional required check(s) as NAME=COMMAND (newline-separated for multiple checks)."},
                "owned_path": {"type": "string", "description": "Optional workspace-relative owned path(s), comma- or newline-separated."},
                "fit_checked": {"type": "boolean", "description": "Skip delegation-fit reminder notes when true."},
            },
            "required": ["workspace", "prompt"],
        },
    },
    {
        "name": "claive_peek",
        "description": "Inspect a worker's compact status and recent event steps without shell polling.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Worker ID or @alias."},
                "steps": {"type": "integer", "description": "Number of recent steps to return (default 5)."},
                "start": {"type": "integer", "description": "Optional 1-based step index to page forward from."},
            },
            "required": ["id"],
        },
    },
    {
        "name": "claive_steer",
        "description": "Send mid-turn steering input or interrupt a hung active tool child on a running worker.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Worker ID or @alias."},
                "message": {"type": "string", "description": "Steering message text (optional when interrupt_tool is true)."},
                "interrupt_tool": {"type": "boolean", "description": "Kill active transient tool child processes first."},
                "wait": {"type": "number", "description": "Seconds to wait for receipt acknowledgement (default 2)."},
            },
            "required": ["id"],
        },
    },
    {
        "name": "claive_wait",
        "description": "Block until one or more workers settle or new inbox events arrive.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Worker ID, @alias, or space/comma-separated worker IDs."},
                "any_worker": {"type": "boolean", "description": "Return as soon as any listed worker settles."},
                "all_workers": {"type": "boolean", "description": "Wait until every listed worker settles (short-circuiting early on ASK or failure)."},
                "timeout": {"type": "number", "description": "Maximum seconds to wait (default 60, capped at 300)."},
                "inbox_consumer": {"type": "string", "description": "Optional inbox consumer name to wake on."},
                "parent_session": {"type": "string", "description": "Optional parent session token to scope wake events."},
                "since_seq": {"type": "integer", "description": "Optional monotonic sequence cursor; wakes immediately on seq > since_seq."},
            },
            "required": [],
        },
    },
    {
        "name": "claive_cancel",
        "description": "Cancel a running or orphaned worker (and its entire process tree/scope).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Worker ID or @alias."},
            },
            "required": ["id"],
        },
    },
    {
        "name": "claive_verify",
        "description": "Run or record verification checks on a settled worker turn.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Worker ID or @alias."},
                "check": {"type": "string", "description": "Optional single check name to verify or attest."},
                "in_worktree": {"type": "boolean", "description": "Run checks in place inside the worker worktree."},
                "status": {"type": "string", "description": "Optional manual attestation status (passed, failed, blocked, not_run)."},
                "reason": {"type": "string", "description": "Optional reason when recording manual attestation."},
                "environment": {"type": "string", "description": "Optional environment note for manual attestation."},
            },
            "required": ["id"],
        },
    },
    {
        "name": "claive_list",
        "description": "List active and recent workers with compact status summaries and optional inbox events.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Maximum number of workers to return (default 20)."},
                "active_only": {"type": "boolean", "description": "Only include active/idle workers."},
                "inbox_consumer": {"type": "string", "description": "Optional inbox consumer cursor to read."},
                "parent_session": {"type": "string", "description": "Optional parent session token to filter workers and inbox."},
                "since_seq": {"type": "integer", "description": "Optional monotonic sequence cursor to read events after."},
                "peek_inbox": {"type": "boolean", "description": "Read without advancing the cursor. Defaults to true for a session-only list and false for a named inbox_consumer."},
                "include_watch_status": {"type": "boolean", "description": "Include watcher and unwatched worker summary."},
            },
            "required": [],
        },
    },
    {
        "name": "claive_followup",
        "description": "Queue a follow-up prompt turn on an open reusable worker.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Worker ID or @alias."},
                "prompt": {"type": "string", "description": "Follow-up prompt text for the worker."},
                "label": {"type": "string", "description": "Optional short label for this follow-up turn."},
                "reasoning_effort": {"type": "string", "description": "Optional reasoning effort override for this turn."},
            },
            "required": ["id", "prompt"],
        },
    },
    {
        "name": "claive_apply",
        "description": "Apply a settled worker's isolated worktree edits back into its source workspace, optionally closing the worker.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Worker ID or @alias."},
                "dry_run": {"type": "boolean", "description": "Preview files that would be applied or deleted without modifying the source workspace."},
                "force": {"type": "boolean", "description": "Overwrite conflicting files in the source workspace."},
                "close": {"type": "boolean", "description": "Close an open reusable worker after applying edits."},
            },
            "required": ["id"],
        },
    },
    {
        "name": "claive_close",
        "description": "Gracefully close an idle reusable worker supervisor (retaining history).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Worker ID or @alias."},
            },
            "required": ["id"],
        },
    },
]


def _validate_schema_node(node, where):
    if isinstance(node, dict):
        bad = FORBIDDEN_SCHEMA_KEYS.intersection(node.keys())
        if bad:
            raise ValueError(f"{where} uses forbidden JSON Schema keywords: {', '.join(sorted(bad))}")
        if "type" in node and not isinstance(node["type"], str):
            raise ValueError(f"{where}.type must be a scalar string, got {type(node['type']).__name__}")
        for key, value in node.items():
            _validate_schema_node(value, f"{where}.{key}")
    elif isinstance(node, list):
        for index, item in enumerate(node):
            _validate_schema_node(item, f"{where}[{index}]")


def validate_flat_schemas():
    """Validate that every registered MCP tool uses a strictly flat, combinator-free JSON schema."""
    names = []
    for tool in TOOL_DEFINITIONS:
        name = tool["name"]
        names.append(name)
        schema = tool.get("inputSchema")
        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise ValueError(f"{name}: inputSchema must be a top-level object")
        if set(schema.keys()) != {"type", "properties", "required"}:
            raise ValueError(f"{name}: inputSchema must contain only type, properties, and required")
        props = schema["properties"]
        req = schema["required"]
        if not isinstance(props, dict) or not isinstance(req, list):
            raise ValueError(f"{name}: invalid properties or required container")
        _validate_schema_node(schema, f"{name}.inputSchema")
        for prop_name, prop_schema in props.items():
            if not isinstance(prop_schema, dict):
                raise ValueError(f"{name}.{prop_name} must be an object")
            if prop_schema.get("type") not in SCALAR_TYPES:
                raise ValueError(f"{name}.{prop_name} must use a scalar type in {sorted(SCALAR_TYPES)}")
            if not set(prop_schema.keys()).issubset({"type", "description", "enum"}):
                extra = sorted(set(prop_schema.keys()) - {"type", "description", "enum"})
                raise ValueError(f"{name}.{prop_name} has disallowed property keys: {', '.join(extra)}")
        for item in req:
            if item not in props:
                raise ValueError(f"{name}: required field {item!r} missing from properties")
    return names


def _check_not_nested():
    worker_id = os.environ.get("CLAIVE_WORKER_ID")
    if worker_id:
        raise ValueError(f"claive-mcp is not permitted inside a claive worker turn ({worker_id})")


def _run_cli(args, timeout=60.0):
    cmd = [sys.executable, cli.launcher_path(), *args]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return res.returncode, res.stdout, res.stderr


def _tool_start(arguments):
    _check_not_nested()
    workspace = str(arguments.get("workspace") or "").strip()
    prompt_text = str(arguments.get("prompt") or "")
    if not workspace:
        raise ValueError("workspace is required")
    if not prompt_text.strip():
        raise ValueError("prompt must not be empty")
    prompts_dir = cli.root() / "mcp-prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    prompt_file = prompts_dir / f"{uuid.uuid4().hex}.md"
    prompt_file.write_text(prompt_text, encoding="utf-8")

    cmd = ["start", "--workspace", workspace, "--prompt-file", str(prompt_file)]
    for flag, key in (
        ("--engine", "engine"),
        ("--label", "label"),
        ("--alias", "alias"),
        ("--model", "model"),
        ("--provider", "provider"),
        ("--reasoning-effort", "reasoning_effort"),
        ("--session", "parent_session"),
        ("--notify-worker", "notify_worker"),
    ):
        val = arguments.get(key)
        if isinstance(val, str) and val.strip():
            cmd.extend([flag, val.strip()])
    explicit_session = arguments.get("parent_session")
    if not (isinstance(explicit_session, str) and explicit_session.strip()):
        resolved_session = inbox.resolve_parent_session(workspace=workspace)
        if resolved_session:
            cmd.extend(["--session", resolved_session])
    check_raw = arguments.get("check")
    if isinstance(check_raw, str) and check_raw.strip():
        for ln in [item.strip() for item in check_raw.splitlines() if item.strip()]:
            cmd.extend(["--check", ln])
    owned_raw = arguments.get("owned_path")
    if isinstance(owned_raw, str) and owned_raw.strip():
        for path_item in [tok.strip() for tok in re.split(r"[,\n]+", owned_raw) if tok.strip()]:
            cmd.extend(["--owned-path", path_item])
    for flag, key in (
        ("--read-only", "read_only"),
        ("--worktree", "worktree"),
        ("--carry-dirty", "carry_dirty"),
        ("--link-deps", "link_deps"),
        ("--reuse-alias", "reuse_alias"),
        ("--park", "park"),
        ("--report", "report"),
        ("--no-fallback", "no_fallback"),
        ("--verify-in-worktree", "verify_in_worktree"),
        ("--fit-checked", "fit_checked"),
    ):
        if arguments.get(key) is True:
            cmd.append(flag)
    if arguments.get("enable_steering") is True:
        cmd.append("--enable-steering")
    elif arguments.get("enable_steering") is False:
        cmd.append("--disable-steering")
    for flag, key in (("--turn-timeout", "turn_timeout"), ("--tool-idle-timeout", "tool_idle_timeout")):
        val = arguments.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool) and val > 0:
            cmd.extend([flag, str(val)])

    code, stdout, stderr = _run_cli(cmd, timeout=60.0)
    if code != 0:
        raise ValueError((stderr or stdout).strip() or f"claive start exited with {code}")
    match = re.search(r"Worker ([0-9a-f]{12})", stdout)
    if not match:
        raise ValueError(f"could not parse worker ID from claive start output: {stdout.strip()}")
    worker_id = match.group(1)
    job_dir = cli.job_path(worker_id)
    state = cli.load(job_dir)
    notes = [line for line in stderr.splitlines() if line.strip()]
    return {
        "worker": status_file.read_or_build(job_dir, state),
        "notes": notes,
    }


def _tool_peek(arguments):
    raw_id = str(arguments.get("id") or "").strip()
    if not raw_id:
        raise ValueError("id is required")
    steps_count = arguments.get("steps", 5)
    if isinstance(steps_count, bool) or not isinstance(steps_count, int) or steps_count < 1:
        steps_count = 5
    steps_count = min(steps_count, 50)
    start_idx = arguments.get("start")
    if isinstance(start_idx, bool) or not isinstance(start_idx, int) or start_idx < 1:
        start_idx = None
    job_dir = cli.job_path(raw_id)
    state = cli.load(job_dir)
    engine = get_engine(state.get("engine", DEFAULT_ENGINE))
    steps, total, latest_intent = peek.steps_window(job_dir, engine, steps_count, start_idx, human_output=False)
    return {
        "worker": status_file.read_or_build(job_dir, state),
        "total_steps": total,
        "latest_intent": latest_intent,
        "steps": steps,
    }


def _tool_steer(arguments):
    _check_not_nested()
    raw_id = str(arguments.get("id") or "").strip()
    if not raw_id:
        raise ValueError("id is required")
    message = arguments.get("message")
    msg_text = message if isinstance(message, str) and message.strip() else None
    wait_s = arguments.get("wait", 2.0)
    if isinstance(wait_s, bool) or not isinstance(wait_s, (int, float)) or wait_s < 0:
        wait_s = 2.0
    wait_s = min(float(wait_s), 30.0)
    if arguments.get("interrupt_tool") is True:
        cmd = ["interrupt-tool", raw_id, "--timeout", str(max(1.0, wait_s)), "--json"]
        if msg_text is not None:
            cmd.extend(["--message", msg_text])
        code, stdout, stderr = _run_cli(cmd, timeout=wait_s + 10.0)
        if code != 0:
            raise ValueError((stderr or stdout).strip() or f"claive interrupt-tool exited with {code}")
        return json.loads(stdout)
    if msg_text is None:
        raise ValueError("message is required unless interrupt_tool is true")
    cmd = ["steer", raw_id, "--message", msg_text, "--wait", str(wait_s), "--json"]
    code, stdout, stderr = _run_cli(cmd, timeout=wait_s + 10.0)
    if code != 0:
        raise ValueError((stderr or stdout).strip() or f"claive steer exited with {code}")
    return json.loads(stdout)


def _tool_wait(arguments):
    raw_ids = str(arguments.get("id") or "").strip()
    ids = [tok for tok in re.split(r"[\s,]+", raw_ids) if tok] if raw_ids else []
    inbox_consumer = str(arguments.get("inbox_consumer") or "").strip()
    parent_session = str(arguments.get("parent_session") or "").strip()
    since_seq = arguments.get("since_seq")
    has_since_seq = isinstance(since_seq, int) and not isinstance(since_seq, bool) and since_seq >= 0
    if not ids and not inbox_consumer and not parent_session and not has_since_seq:
        raise ValueError("provide id, inbox_consumer, parent_session, or since_seq")
    timeout = arguments.get("timeout", 60.0)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        timeout = 60.0
    timeout = min(float(timeout), 300.0)
    cmd = ["wait", *ids, "--timeout", str(timeout), "--json"]
    if arguments.get("all_workers") is True:
        cmd.append("--all")
    elif arguments.get("any_worker") is True:
        cmd.append("--any")
    if inbox_consumer:
        cmd.extend(["--inbox", inbox_consumer])
    if parent_session:
        cmd.extend(["--session", parent_session])
    if has_since_seq:
        cmd.extend(["--since-seq", str(since_seq)])
    code, stdout, stderr = _run_cli(cmd, timeout=timeout + 15.0)
    if not stdout.strip():
        raise ValueError((stderr or "").strip() or f"claive wait exited with {code}")
    parsed = json.loads(stdout)
    parsed["exit_code"] = code
    settled_worker = parsed.get("worker")
    if isinstance(settled_worker, str) and settled_worker:
        try:
            job_dir = cli.job_path(settled_worker)
            parsed["worker_status"] = status_file.read_or_build(job_dir, cli.load(job_dir))
        except Exception:
            pass
    return parsed


def _tool_cancel(arguments):
    _check_not_nested()
    raw_id = str(arguments.get("id") or "").strip()
    if not raw_id:
        raise ValueError("id is required")
    code, stdout, stderr = _run_cli(["cancel", raw_id], timeout=30.0)
    if code != 0:
        raise ValueError((stderr or stdout).strip() or f"claive cancel exited with {code}")
    job_dir = cli.job_path(raw_id)
    state = cli.load(job_dir)
    return {
        "message": stdout.strip(),
        "worker": status_file.read_or_build(job_dir, state),
    }


def _tool_verify(arguments):
    _check_not_nested()
    raw_id = str(arguments.get("id") or "").strip()
    if not raw_id:
        raise ValueError("id is required")
    cmd = ["verify", raw_id]
    for flag, key in (
        ("--check", "check"),
        ("--status", "status"),
        ("--reason", "reason"),
        ("--environment", "environment"),
    ):
        val = arguments.get(key)
        if isinstance(val, str) and val.strip():
            cmd.extend([flag, val.strip()])
    if arguments.get("in_worktree") is True:
        cmd.append("--verify-in-worktree")
    code, stdout, stderr = _run_cli(cmd, timeout=600.0)
    if code not in {0, 2} or not stdout.strip():
        raise ValueError((stderr or stdout).strip() or f"claive verify exited with {code}")
    result = json.loads(stdout)
    job_dir = cli.job_path(raw_id)
    result["worker"] = status_file.read_or_build(job_dir, cli.load(job_dir))
    return result


def _tool_list(arguments):
    limit = arguments.get("limit", 20)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        limit = 20
    limit = min(limit, 100)
    active_only = arguments.get("active_only") is True
    parent_session = str(arguments.get("parent_session") or "").strip() or None
    if parent_session is not None:
        inbox.check_consumer(parent_session, label="session")
    records = cli.jobs(limit=None if (active_only or parent_session) else limit)
    if parent_session is not None:
        records = [item for item in records if item.get("parent_session") == parent_session]
    if active_only:
        records = [item for item in records if item.get("status") in cli.ACTIVE]
    records = records[:limit]
    workers = []
    for state in records:
        worker_id = state.get("id")
        if not worker_id:
            continue
        workers.append(status_file.read_or_build(cli.root() / worker_id, state))
    payload = {"workers": workers, "latest_seq": inbox.current_seq()}
    consumer = str(arguments.get("inbox_consumer") or "").strip()
    since_seq = arguments.get("since_seq")
    has_since_seq = isinstance(since_seq, int) and not isinstance(since_seq, bool) and since_seq >= 0
    if has_since_seq and not consumer:
        payload["inbox_events"] = inbox.read_since(since_seq, parent_session=parent_session, wake_only=True)
    elif consumer or parent_session:
        eff_consumer = consumer or inbox.session_consumer(parent_session)
        payload["inbox_events"] = inbox.read_new(
            eff_consumer,
            peek=arguments.get("peek_inbox", not consumer) is True,
            parent_session=parent_session,
            since_seq=since_seq if has_since_seq else None,
        )
    if arguments.get("include_watch_status") is True:
        from claivelib import watchers
        payload["watch_status"] = watchers.watch_status(parent_session=parent_session, consumer=consumer or None)
    return payload


def _tool_followup(arguments):
    _check_not_nested()
    raw_id = str(arguments.get("id") or "").strip()
    if not raw_id:
        raise ValueError("id is required")
    prompt_text = str(arguments.get("prompt") or "")
    if not prompt_text.strip():
        raise ValueError("prompt must not be empty")
    prompts_dir = cli.root() / "mcp-prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    prompt_file = prompts_dir / f"{uuid.uuid4().hex}.md"
    prompt_file.write_text(prompt_text, encoding="utf-8")

    cmd = ["followup", raw_id, "--prompt-file", str(prompt_file)]
    for flag, key in (("--label", "label"), ("--reasoning-effort", "reasoning_effort")):
        val = arguments.get(key)
        if isinstance(val, str) and val.strip():
            cmd.extend([flag, val.strip()])

    code, stdout, stderr = _run_cli(cmd, timeout=30.0)
    if code != 0:
        raise ValueError((stderr or stdout).strip() or f"claive followup exited with {code}")
    job_dir = cli.job_path(raw_id)
    return {
        "queued": True,
        "worker": status_file.read_or_build(job_dir, cli.load(job_dir)),
    }


def _tool_apply(arguments):
    _check_not_nested()
    raw_id = str(arguments.get("id") or "").strip()
    if not raw_id:
        raise ValueError("id is required")
    dry_run = arguments.get("dry_run") is True
    force = arguments.get("force") is True
    close = arguments.get("close") is True
    flags = []
    if dry_run:
        flags.append("--dry-run")
    if force:
        flags.append("--force")

    top_cmd = ["apply", raw_id, *flags, *(["--close"] if close else []), "--json"]
    used_fallback = False
    code, stdout, stderr = _run_cli(top_cmd, timeout=60.0)
    if code == 2 and "invalid choice" in (stderr or "") and not stdout.strip():
        used_fallback = True
        code, stdout, stderr = _run_cli(["worktree", "apply", raw_id, *flags, "--json"], timeout=60.0)

    if not stdout.strip():
        raise ValueError((stderr or stdout).strip() or f"claive apply exited with {code}")
    result = json.loads(stdout)
    if code != 0 and result.get("status") != "conflict":
        raise ValueError((stderr or stdout).strip() or f"claive apply exited with {code}")

    if used_fallback and close and not dry_run and code == 0:
        c_code, c_stdout, c_stderr = _run_cli(["close", raw_id], timeout=30.0)
        if c_code != 0:
            raise ValueError((c_stderr or c_stdout).strip() or f"claive close exited with {c_code}")
        result["closed"] = True

    job_dir = cli.job_path(raw_id)
    result["worker"] = status_file.read_or_build(job_dir, cli.load(job_dir))
    return result


def _tool_close(arguments):
    _check_not_nested()
    raw_id = str(arguments.get("id") or "").strip()
    if not raw_id:
        raise ValueError("id is required")
    code, stdout, stderr = _run_cli(["close", raw_id], timeout=30.0)
    if code != 0:
        raise ValueError((stderr or stdout).strip() or f"claive close exited with {code}")
    job_dir = cli.job_path(raw_id)
    return {
        "closed": True,
        "message": stdout.strip(),
        "worker": status_file.read_or_build(job_dir, cli.load(job_dir)),
    }


HANDLERS = {
    "claive_start": _tool_start,
    "claive_peek": _tool_peek,
    "claive_steer": _tool_steer,
    "claive_wait": _tool_wait,
    "claive_cancel": _tool_cancel,
    "claive_verify": _tool_verify,
    "claive_list": _tool_list,
    "claive_followup": _tool_followup,
    "claive_apply": _tool_apply,
    "claive_close": _tool_close,
}


def dispatch_tool(name, arguments):
    """Execute one MCP tool call and return the MCP CallToolResult dict."""
    handler = HANDLERS.get(name)
    if handler is None:
        raise KeyError(f"unknown tool: {name}")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise ValueError("tool arguments must be a JSON object")
    try:
        output = handler(arguments)
        return {
            "content": [{"type": "text", "text": json.dumps(output, indent=2, ensure_ascii=False)}],
            "isError": False,
        }
    except Exception as error:
        return {
            "content": [{"type": "text", "text": json.dumps({"error": str(error)}, indent=2)}],
            "isError": True,
        }


def _read_exact(stream, count):
    chunks = []
    remaining = count
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _read_message(stream):
    """Read one JSON-RPC message supporting both NDJSON and Content-Length framing."""
    while True:
        line = stream.readline()
        if not line:
            return None, None
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(b"{"):
            return json.loads(stripped.decode("utf-8")), "ndjson"
        headers = {}
        cur = line
        while True:
            text = cur.decode("ascii", errors="replace").strip()
            if not text:
                break
            key, sep, val = text.partition(":")
            if sep:
                headers[key.strip().lower()] = val.strip()
            cur = stream.readline()
            if not cur:
                return None, None
        length = int(headers.get("content-length", "0"))
        if length <= 0:
            continue
        body = _read_exact(stream, length)
        if not body:
            return None, None
        return json.loads(body.decode("utf-8")), "lsp"


def _write_message(stream, payload, mode):
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if mode == "lsp":
        header = f"Content-Length: {len(encoded)}\r\n\r\n".encode("ascii")
        stream.write(header + encoded)
    else:
        stream.write(encoded + b"\n")
    stream.flush()


def handle_rpc(request):
    """Process one JSON-RPC 2.0 dict; return response dict or None for notifications."""
    if not isinstance(request, dict):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
    req_id = request.get("id")
    has_id = "id" in request
    method = request.get("method")
    params = request.get("params") if isinstance(request.get("params"), dict) else {}

    if not has_id:
        return None
    if method == "initialize":
        requested_version = params.get("protocolVersion")
        version = requested_version if isinstance(requested_version, str) and requested_version else PROTOCOL_VERSION
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": dict(SERVER_INFO),
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOL_DEFINITIONS}}
    if method == "tools/call":
        tool_name = params.get("name")
        if tool_name not in HANDLERS:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32602, "message": f"Unknown tool: {tool_name}"},
            }
        result = dispatch_tool(tool_name, params.get("arguments"))
        return {"jsonrpc": "2.0", "id": req_id, "result": result}
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def serve_stdio(stdin=None, stdout=None):
    """Run the MCP JSON-RPC 2.0 server over stdio."""
    _check_not_nested()
    validate_flat_schemas()
    in_stream = sys.stdin.buffer if stdin is None else stdin
    out_stream = sys.stdout.buffer if stdout is None else stdout
    while True:
        try:
            message, mode = _read_message(in_stream)
        except ValueError as error:
            _write_message(
                out_stream,
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": f"Parse error: {error}"}},
                "ndjson",
            )
            continue
        if message is None:
            return 0
        response = handle_rpc(message)
        if response is not None:
            _write_message(out_stream, response, mode or "ndjson")


def main_from_args(check=False, json_output=False):
    names = validate_flat_schemas()
    if check:
        payload = {"ok": True, "flat_schemas": True, "tool_count": len(names), "tools": names}
        if json_output:
            print(json.dumps(payload, indent=2))
        else:
            print(f"ok claive-mcp flat schemas ({len(names)} tools): " + ", ".join(names))
        return 0
    return serve_stdio()


def entrypoint(launcher=None):
    os.umask(0o077)
    if launcher is not None:
        sibling = Path(launcher).resolve().with_name("claive")
        if sibling.is_file():
            cli.set_launcher_path(sibling)
    arg_parser = argparse.ArgumentParser(description="Flat-schema MCP stdio server for claive.")
    arg_parser.add_argument("--check", action="store_true", help="validate flat tool schemas and exit")
    arg_parser.add_argument("--json", action="store_true", help="JSON output for --check")
    args = arg_parser.parse_args()
    try:
        return main_from_args(check=args.check, json_output=args.json)
    except KeyboardInterrupt:
        return 130
    except ValueError as error:
        print(f"claive-mcp: {error}", file=sys.stderr)
        return 1
