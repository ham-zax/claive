"""Claude Code engine (headless stream-json). The claive config, not this module, decides the allowed models."""
import json
import os
from pathlib import Path
import uuid
from datetime import datetime, timezone

from claivelib import config
from claivelib.budget import HOOK_SETTINGS
from claivelib.engine import SteeringCapability, WorkerEngine
from claivelib.state import launch_config, session_id

CLAUDE_MODEL = "claude-haiku-5-5"  # the default when the config names no model
CLAUDE_MODELS = ("claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-1")
CLAUDE_PROVIDER = "anthropic"
HAIKU_COMPACTION_POINT = 105000
DEFAULT_COMPACTION_POINT = 155000
# Room past stop for the handoff reply. When the budget gate does not deny the next tool call,
# the margin only lowers the risk that its result reaches compaction. Keep the 25k margin
# for non-Haiku workers even though the interactive hooks now use 15k.
HAIKU_BUDGET_MARGIN = 20000
BUDGET_COMPACTION_MARGIN = 25000
# Claude Code 2.1.300 compacts 33000 tokens before the configured window.
AUTOCOMPACT_OFFSET = 33000


def budget_margin(model):
    return HAIKU_BUDGET_MARGIN if "haiku" in model.lower() else BUDGET_COMPACTION_MARGIN


def compaction_point(model, budget):
    if isinstance(budget, dict):
        return budget["stop"] + budget_margin(model)
    return HAIKU_COMPACTION_POINT if "haiku" in model.lower() else DEFAULT_COMPACTION_POINT


def default_binary():
    return os.environ.get("CLAUDE_WORKER_BINARY", str(Path.home() / ".local/bin/claude"))


def _config_dir():
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))).expanduser()


def instruction_dirs(workspace):
    """Directories whose CLAUDE.md files the worker loads: the claive worker dir, then the git root down to the workspace.

    `--setting-sources ""` turns off the native user and project memory, so claive passes these as
    `--add-dir` with CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD=1. The walk stops below the home
    directory so a home-level repository does not load the parent's ~/.claude/CLAUDE.md.
    """
    dirs = []
    worker_dir = config.config_path().parent / "claude"
    if (worker_dir / "CLAUDE.md").is_file():
        dirs.append(str(worker_dir))
    start = Path(workspace).resolve()
    chain = []
    for directory in (start, *start.parents):
        if directory == Path.home().resolve() or directory == directory.parent:
            chain = [start]
            break
        chain.append(directory)
        if (directory / ".git").exists():
            break
    return dirs + [str(directory) for directory in reversed(chain)]


class ClaudeEngine(WorkerEngine):
    name = "claude"
    supports_fork = True
    default_model = CLAUDE_MODEL
    default_reasoning_effort = "xhigh"
    efforts = {"low", "medium", "high", "xhigh", "max"}

    def supported_models(self):
        return {model: ("low", "medium", "high", "xhigh", "max") for model in CLAUDE_MODELS}

    def __init__(self):
        self._last_text = ""
        self._last_api_error_text = None

    def child_env(self):
        # Without this, Claude Code loads no CLAUDE.md from the --add-dir directories.
        return {"CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD": "1"}

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
            "steering": bool(options.get("steering")) or isinstance(options.get("budget"), dict),
            "budget": options.get("budget"),
            "budget_off_reason": options.get("budget_off_reason"),
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
        if not isinstance(model, str) or not model:
            raise ValueError("the claude engine needs a model name")

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
        point = compaction_point(request.model, request.budget)
        command = [
            request.binary, "--print", "--output-format", "stream-json", "--verbose",
            "--model", request.model, "--effort", request.reasoning_effort,
            "--tools", "default", "--permission-mode", "plan" if request.read_only else "bypassPermissions",
            "--autocompact", f"{(point + AUTOCOMPACT_OFFSET) // 1000}k",
            # Isolate user/project settings so the parent's hooks and permission rules do not run in workers.
            "--setting-sources", "",
        ]
        for directory in instruction_dirs(request.workspace):
            command += ["--add-dir", directory]
        if isinstance(request.budget, dict):
            command += ["--settings", json.dumps(HOOK_SETTINGS)]
        if request.session_logging:
            if not request.session_id:
                raise ValueError("Claude requires a session ID when session logging is on")
            if request.fork_session_id:
                command += ["--resume", request.fork_session_id, "--fork-session"]
            elif self._session_file(request.session_id):
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
            self._last_api_error_text = None
            return super().describe_event(event)
        if kind == "result":
            matching_error_text = self._last_api_error_text
            self._last_api_error_text = None
        else:
            self._last_api_error_text = None
        if kind in {"assistant", "user"}:
            if kind == "user" and event.get("isReplay"):
                return []
            message = event.get("message")
            blocks = message.get("content") if isinstance(message, dict) else None
            if kind == "assistant":
                api_error_blocks = [block for block in blocks if isinstance(block, dict)
                                    and block.get("isApiErrorMessage") is True] if isinstance(blocks, list) else []
                api_error = (event.get("error") or event.get("isApiErrorMessage") is True
                             or (isinstance(message, dict) and
                                 (message.get("error") or message.get("isApiErrorMessage") is True))
                             or bool(api_error_blocks))
                if api_error:
                    error_text = content_text(api_error_blocks or blocks)
                    if not error_text:
                        error = event.get("error")
                        if not error and isinstance(message, dict):
                            error = message.get("error")
                        if isinstance(error, dict):
                            error = error.get("message") or error.get("type")
                        error_text = str(error or "Claude API error")
                    self._last_api_error_text = error_text
                    return [{"kind": "error", "text": error_text}]
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
            result_text = str(event.get("result") or event.get("subtype") or "Claude turn failed")
            if matching_error_text is not None and result_text == matching_error_text:
                return []
            return [{"kind": "error", "text": result_text}]
        if kind == "rate_limit_event":
            normalized = self.normalize_event(event)
            quota = next((item for item in normalized if item.get("type") == "quota_exhausted"), None)
            if quota is None:
                return []
            return [{"kind": "error", "text": compact(quota)}]
        if kind == "system":
            error = event.get("error")
            if not error:
                return []
            if isinstance(error, dict):
                error = error.get("message") or compact(error)
            return [{"kind": "error", "text": str(error)}]
        return super().describe_event(event)

    def normalize_event(self, event):
        kind = event.get("type")
        if not isinstance(kind, str):
            raise ValueError("Claude event has no string type")
        if kind == "system" and event.get("subtype") == "init":
            self._last_text = ""
            if isinstance(event.get("session_id"), str):
                return [{"type": "session_bound", "native_kind": kind, "session_id": event["session_id"]}]
        elif kind == "system" and event.get("subtype") == "compact_boundary":
            return [{"type": "context_compacted", "native_kind": kind}]
        elif kind == "assistant":
            message = event.get("message")
            blocks = message.get("content") if isinstance(message, dict) else None
            result = []
            if isinstance(blocks, list):
                for block in blocks:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use":
                        result.append({"type": "tool_started", "native_kind": kind,
                                       "tool": block.get("name", "tool"), "tool_id": block.get("id")})
                    elif block.get("type") == "text":
                        self._last_text = block.get("text", "")
                        result.append({"type": "output_delta", "native_kind": kind})
                result = result or [{"type": "model_step", "native_kind": kind}]
            else:
                result = [{"type": "activity", "native_kind": kind}]
            usage = message.get("usage") if isinstance(message, dict) else None
            if isinstance(usage, dict) and event.get("parent_tool_use_id") is None:
                tokens = sum(value for key in
                             ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
                             if isinstance(value := usage.get(key), int) and not isinstance(value, bool))
                result.append({"type": "context_usage", "native_kind": kind, "tokens": tokens})
            return result
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
