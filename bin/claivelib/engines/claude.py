"""Claude Code engine (headless stream-json), strictly locked to Haiku 5.5."""
import json
import os
from pathlib import Path
import uuid
from datetime import datetime, timezone

from claivelib.engine import WorkerEngine
from claivelib.state import launch_config, session_id

CLAUDE_MODEL = "claude-haiku-5-5"
CLAUDE_PROVIDER = "anthropic"
# Claude Code rejects anything below 100k, so this is the earliest compaction it allows.
AUTOCOMPACT_WINDOW = "100k"
READ_TOOLS = "Read,Grep,Glob"
WRITE_TOOLS = "Read,Edit,Write,Bash,Grep,Glob"


def default_binary():
    return os.environ.get("CLAUDE_WORKER_BINARY", str(Path.home() / ".local/bin/claude"))


def _config_dir():
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))).expanduser()


class ClaudeEngine(WorkerEngine):
    name = "claude"
    default_model = CLAUDE_MODEL
    default_reasoning_effort = "max"
    efforts = {"low", "medium", "high", "xhigh", "max"}

    def __init__(self):
        self._last_text = ""

    def resolve_session_id(self, value, session_logging=True):
        if not session_logging:
            return None
        return str(uuid.UUID(value)) if value else str(uuid.uuid4())

    def resolve_launch(self, **options):
        return {
            "binary": default_binary(),
            "provider": options.get("provider") or CLAUDE_PROVIDER,
            "model": options.get("model") or CLAUDE_MODEL,
            "read_only": bool(options.get("read_only")),
            "web": bool(options.get("web")),
            "output_schema": options.get("output_schema"),
            "session_logging": bool(options.get("session_logging", True)),
            "isolation": dict(options.get("isolation") or {}),
        }

    def validate_launch(self, launch):
        binary = Path(launch["binary"])
        if not binary.is_absolute() or not binary.is_file() or not os.access(binary, os.X_OK):
            raise ValueError("Claude binary must be an absolute executable file")
        self._check_model(launch.get("model"))
        if launch.get("output_schema"):
            raise ValueError("Claude does not support --output-schema")
        isolation = launch.get("isolation") or {}
        if isolation.get("mode", "none") != "none" or isolation.get("base"):
            raise ValueError("Claude does not manage worktrees; pass an externally retained worktree as --workspace")

    @staticmethod
    def _check_model(model):
        if model != CLAUDE_MODEL:
            raise ValueError(f"the claude engine is locked to {CLAUDE_MODEL}, not {model}")

    def validate_turn(self, request):
        self._check_model(request.model)
        if request.provider != CLAUDE_PROVIDER:
            raise ValueError(f"the claude engine supports only the {CLAUDE_PROVIDER} provider")
        if request.reasoning_effort not in self.efforts:
            raise ValueError(f"unsupported Claude reasoning effort: {request.reasoning_effort}")
        if request.max_model_steps is not None:
            raise ValueError("Claude does not support --max-model-steps")

    @staticmethod
    def _session_file(identifier):
        """The retained session transcript, or None before the first turn has run."""
        matches = sorted((_config_dir() / "projects").glob(f"*/{identifier}.jsonl"))
        return matches[0] if matches else None

    def build_command(self, request):
        self.validate_turn(request)
        tools = READ_TOOLS if request.read_only else WRITE_TOOLS
        if request.web:
            tools += ",WebSearch,WebFetch"
        command = [
            request.binary, "--print", "--output-format", "stream-json", "--verbose",
            "--model", request.model, "--effort", request.reasoning_effort,
            "--tools", tools, "--permission-mode", "bypassPermissions", "--autocompact", AUTOCOMPACT_WINDOW,
            # No user/project settings, hooks, CLAUDE.md-driven config, MCP servers or skills.
            "--setting-sources", "", "--strict-mcp-config", "--disable-slash-commands",
        ]
        if request.session_logging:
            if not request.session_id:
                raise ValueError("Claude requires a session ID when session logging is on")
            if self._session_file(request.session_id):
                command += ["--resume", request.session_id]
            else:
                command += ["--session-id", request.session_id]
        else:
            command += ["--no-session-persistence"]
        # claive runs workers with stdin closed and Claude has no prompt-file flag, so the prompt is argv.
        command += ["--", Path(request.prompt_file).read_text(encoding="utf-8")]
        return command

    def normalize_event(self, event):
        kind = event.get("type")
        if not isinstance(kind, str):
            raise ValueError("Claude event has no string type")
        if kind == "system" and event.get("subtype") == "init":
            self._last_text = ""
        elif kind == "assistant":
            message = event.get("message")
            blocks = message.get("content") if isinstance(message, dict) else None
            if isinstance(blocks, list):
                for block in blocks:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use":
                        return [{"type": "tool_started", "native_kind": kind, "tool": block.get("name", "tool")}]
                    if block.get("type") == "text":
                        self._last_text = block.get("text", "")
                        return [{"type": "output_delta", "native_kind": kind}]
                return [{"type": "model_step", "native_kind": kind}]
        elif kind == "user":
            message = event.get("message")
            blocks = message.get("content") if isinstance(message, dict) else None
            for block in blocks if isinstance(blocks, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error"):
                    content = block.get("content")
                    reason = content if isinstance(content, str) else " ".join(
                        part.get("text", "") for part in content or []
                        if isinstance(part, dict) and part.get("type") == "text")
                    return [{"type": "task_warning", "native_kind": kind, "reason": reason}]
        elif kind == "rate_limit_event":
            info = event.get("rate_limit_info")
            if isinstance(info, dict) and info.get("status") == "rejected":
                quota = {"type": "quota_exhausted", "native_kind": kind}
                reset = info.get("resetsAt")
                if isinstance(reset, (int, float)) and not isinstance(reset, bool):
                    quota["reset_at"] = datetime.fromtimestamp(reset, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                return [{"type": "activity", "native_kind": kind}, quota]
        elif kind == "result":
            completed = event.get("subtype") == "success" and not event.get("is_error")
            text = event.get("result") if isinstance(event.get("result"), str) else self._last_text
            return [{
                "type": "terminal_completed" if completed else "terminal_failed",
                "native_kind": kind,
                "terminal": "completed" if completed else "failed",
                "reason": None if completed else text or f"Claude outcome: {event.get('subtype') or 'missing'}",
                "text": text if completed else "",
            }]
        return [{"type": "activity", "native_kind": kind}]

    def session_usage(self, state):
        identifier = session_id(state)
        if not identifier or not launch_config(state).get("session_logging", True):
            raise ValueError("Claude usage needs a retained session")
        path = self._session_file(identifier)
        if path is None:
            raise ValueError("retained Claude session file is unavailable")
        by_message = {}
        with path.open() as stream:
            for line in stream:
                record = json.loads(line)
                message = record.get("message") if isinstance(record, dict) else None
                usage = message.get("usage") if isinstance(message, dict) else None
                if record.get("type") != "assistant" or not isinstance(usage, dict):
                    continue
                values = {key: value for key, value in usage.items()
                          if isinstance(value, int) and not isinstance(value, bool) and value >= 0}
                if "input_tokens" not in values or "cache_read_input_tokens" not in values:
                    continue
                by_message[message.get("id") or len(by_message)] = dict(
                    model=message.get("model"),
                    # Claude's input_tokens excludes cache reads/writes. Normalize to total input supplied.
                    input_tokens=values["input_tokens"] + values["cache_read_input_tokens"]
                    + values.get("cache_creation_input_tokens", 0),
                    output_tokens=values.get("output_tokens", 0),
                    cached_tokens=values["cache_read_input_tokens"],
                    cache_read_tokens=values["cache_read_input_tokens"],
                    cache_write_tokens=values.get("cache_creation_input_tokens", 0),
                )
        calls = list(by_message.values())
        totals = {key: sum(call[key] for call in calls)
                  for key in ("input_tokens", "output_tokens", "cached_tokens")} if calls else {}
        inputs = totals.get("input_tokens", 0)
        ratio = totals["cached_tokens"] / inputs if calls and inputs else None
        return dict(model_calls=len(calls), calls=calls, totals=totals, cache_hit_ratio=ratio)
