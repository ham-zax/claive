"""Claude Code engine (headless stream-json), strictly locked to Haiku 5.5."""
import json
import os
from pathlib import Path
import uuid
from datetime import datetime, timezone

from claivelib.engine import SteeringCapability, WorkerEngine
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
    default_reasoning_effort = "xhigh"
    efforts = {"low", "medium", "high", "xhigh", "max"}

    def __init__(self):
        self._last_text = ""

    def steering_capability(self):
        return SteeringCapability(True, "claude-stream-json", notes=(
            "Requires --enable-steering; input joins the turn after the current tool calls finish.",
            "Acknowledgement is Claude's replay of the message as it enters the conversation; "
            "compliance is not established.",))

    @staticmethod
    def _user_frame(text, identifier=None):
        frame = {"type": "user", "message": {"role": "user", "content": text}}
        if identifier:
            frame["uuid"] = str(uuid.UUID(identifier))
        return json.dumps(frame) + "\n"

    def initial_input(self, request):
        if not request.steering:
            return None
        return self._user_frame(Path(request.prompt_file).read_text(encoding="utf-8"))

    def steering_frame(self, identifier, message):
        return self._user_frame(message, identifier)

    def resolve_session_id(self, value, session_logging=True):
        if not session_logging:
            return None
        return str(uuid.UUID(value)) if value else str(uuid.uuid4())

    def resolve_launch(self, **options):
        return {
            "binary": default_binary(),
            "steering": bool(options.get("steering")),
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
            self._schema(launch["output_schema"])
        isolation = launch.get("isolation") or {}
        if isolation.get("mode", "none") != "none" or isolation.get("base"):
            raise ValueError("Claude does not manage worktrees; pass an externally retained worktree as --workspace")

    @staticmethod
    def _schema(path):
        """The JSON Schema text for --json-schema; Claude takes the schema itself, not a file path."""
        schema = Path(path)
        if not schema.is_absolute() or not schema.is_file():
            raise ValueError("--output-schema must be an existing absolute file")
        try:
            if not isinstance(json.loads(schema.read_text(encoding="utf-8")), dict):
                raise ValueError("not a JSON object")
        except ValueError as error:
            raise ValueError(f"--output-schema must hold a JSON Schema object: {error}") from error
        return schema.read_text(encoding="utf-8")

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
        if request.output_schema:
            # Claude validates the final answer against the schema and returns it as the result text.
            command += ["--json-schema", self._schema(request.output_schema)]
        if request.steering:
            # Duplex turn: the prompt and later steering arrive as stream-json user messages on stdin,
            # and Claude replays each one on stdout when it enters the conversation.
            return command + ["--input-format", "stream-json", "--replay-user-messages"]
        # claive runs workers with stdin closed and Claude has no prompt-file flag, so the prompt is argv.
        command += ["--", Path(request.prompt_file).read_text(encoding="utf-8")]
        return command

    def describe_event(self, event) -> list[dict]:
        from claivelib.peek import content_text, compact

        kind = event.get("type")
        if not isinstance(kind, str):
            return super().describe_event(event)
        if kind in {"assistant", "user"}:
            if kind == "user" and event.get("isReplay"):
                return []
            message = event.get("message")
            blocks = message.get("content") if isinstance(message, dict) else None
            if isinstance(blocks, list):
                steps = []
                for block in blocks:
                    if not isinstance(block, dict):
                        continue
                    if kind == "assistant" and block.get("type") == "text":
                        steps.append({"kind": "text", "text": content_text([block])})
                    elif kind == "assistant" and block.get("type") == "tool_use":
                        steps.append({"kind": "tool_call", "text": str(block.get("name", "tool")) +
                                      " " + compact(block.get("input", {}))})
                    elif kind == "user" and block.get("type") == "tool_result":
                        steps.append({"kind": "tool_result", "text":
                                      ("error: " if block.get("is_error") else "") +
                                      content_text(block.get("content"))})
                return steps
        if kind == "result":
            if event.get("subtype") == "success" and not event.get("is_error"):
                return []  # The assistant event already contains the final text.
            return [{"kind": "error", "text": str(event.get("result") or
                                                  event.get("subtype") or "Claude turn failed")}]
        if kind == "system" and event.get("subtype") == "init":
            return []
        return super().describe_event(event)

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
                result = []
                for block in blocks:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use":
                        result.append({"type": "tool_started", "native_kind": kind,
                                       "tool": block.get("name", "tool"), "tool_id": block.get("id")})
                    elif block.get("type") == "text":
                        self._last_text = block.get("text", "")
                        result.append({"type": "output_delta", "native_kind": kind})
                return result or [{"type": "model_step", "native_kind": kind}]
        elif kind == "user" and event.get("isReplay"):
            identifier = str(event.get("uuid") or "").replace("-", "")
            return [{"type": "control_response", "native_kind": kind, "id": identifier, "command": "steer",
                     "success": True, "data": "entered the conversation"}]
        elif kind == "user":
            message = event.get("message")
            blocks = message.get("content") if isinstance(message, dict) else None
            result = []
            for block in blocks if isinstance(blocks, list) else []:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                result.append({"type": "tool_completed", "native_kind": kind, "tool_id": block.get("tool_use_id")})
                if block.get("is_error"):
                    content = block.get("content")
                    reason = content if isinstance(content, str) else " ".join(
                        part.get("text", "") for part in content or []
                        if isinstance(part, dict) and part.get("type") == "text")
                    result.append({"type": "task_warning", "native_kind": kind, "reason": reason,
                                   "tool_id": block.get("tool_use_id")})
            if result:
                return result
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
            if completed and isinstance(event.get("structured_output"), (dict, list)):
                text = json.dumps(event["structured_output"])
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
