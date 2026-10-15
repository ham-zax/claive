"""Codex CLI engine (`codex exec --json`), strictly locked to a fixed list of model and effort pairs."""
import json
import os
import shutil
import sys
from pathlib import Path
import uuid

from claivelib.engine import SteeringCapability, WorkerEngine
from claivelib.config import engine_policy
from claivelib.state import launch_config, session_id

CODEX_MODEL = "gpt-6-luna"
CODEX_PROVIDER = "openai"
CODEX_EFFORT = "max"
# Efforts each allowed model accepts: Luna only at max; Sol at low to xhigh, never max or ultra.
CODEX_EFFORTS = {CODEX_MODEL: (CODEX_EFFORT,), "gpt-6.1-sol": ("low", "medium", "high", "xhigh")}
# Effort for a turn that names a model but no effort; other models keep the engine default.
CODEX_DEFAULT_EFFORTS = {"gpt-6.1-sol": "medium"}


def default_binary():
    """CODEX_WORKER_BINARY, else ~/.local/bin/codex, else the pinned nvm install, else PATH."""
    local = Path.home() / ".local/bin/codex"
    pinned = Path.home() / ".nvm/versions/node/v24.19.0/bin/codex"
    found = next((path for path in (local, pinned) if path.is_file()), None)
    return os.environ.get("CODEX_WORKER_BINARY", str(found or shutil.which("codex") or local))


def _home():
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()


def _yolo(request):
    """Write turns skip Codex's sandbox and approvals unless CLAIVE_CODEX_YOLO=0; read-only turns never do."""
    return not request.read_only and os.environ.get("CLAIVE_CODEX_YOLO", "").lower() not in {"0", "false", "no", "off"}


def _use_user_config():
    return engine_policy("codex").get("user_config", False) or os.environ.get(
        "CLAIVE_CODEX_USE_USER_CONFIG", "").lower() in {"1", "true", "yes"}


class CodexEngine(WorkerEngine):
    name = "codex"
    supports_fork = True
    default_model = CODEX_MODEL
    default_reasoning_effort = CODEX_EFFORT

    def default_effort_for(self, model):
        return CODEX_DEFAULT_EFFORTS.get(model, CODEX_EFFORT)

    def supported_models(self):
        return {model: tuple(efforts) for model, efforts in CODEX_EFFORTS.items()}

    def steering_capability(self):
        return SteeringCapability(True, "codex-app-server", notes=(
            "Requires --enable-steering; input is accepted for the active turn ID only.",
            "App Server acknowledgement does not establish model consumption.",))

    def initial_input(self, request):
        if not request.steering:
            return None
        return json.dumps({"type": "prompt", "id": "claive-initial",
                           "message": Path(request.prompt_file).read_text()}) + "\n"

    def __init__(self):
        self._last_text = ""
        self._peek_message = None

    def resolve_session_id(self, value, session_logging=True):
        if not session_logging:
            return None
        return str(uuid.UUID(value)) if value else str(uuid.uuid4())

    def resolve_launch(self, **options):
        return {
            "binary": default_binary(),
            "steering": bool(options.get("steering")),
            "provider": options.get("provider") or CODEX_PROVIDER,
            "model": options.get("model") or CODEX_MODEL,
            "read_only": bool(options.get("read_only")),
            "web": bool(options.get("web")),
            "output_schema": options.get("output_schema"),
            "session_logging": bool(options.get("session_logging", True)),
            "isolation": dict(options.get("isolation") or {}),
        }

    def validate_launch(self, launch):
        binary = Path(launch["binary"])
        if not binary.is_absolute() or not binary.is_file() or not os.access(binary, os.X_OK):
            raise ValueError("Codex binary must be an absolute executable file")
        self._check_model(launch.get("model"))
        if launch.get("output_schema"):
            raise ValueError("Codex does not support --output-schema in this adapter")
        isolation = launch.get("isolation") or {}
        if isolation.get("mode", "none") != "none" or isolation.get("base"):
            raise ValueError("Codex does not manage worktrees; pass an externally retained worktree as --workspace")

    @staticmethod
    def _check_model(model):
        if model not in CODEX_EFFORTS:
            raise ValueError(f"the codex engine is locked to {' or '.join(CODEX_EFFORTS)}, not {model}")

    def validate_turn(self, request):
        self._check_model(request.model)
        if request.provider != CODEX_PROVIDER:
            raise ValueError(f"the codex engine supports only the {CODEX_PROVIDER} provider")
        efforts = CODEX_EFFORTS[request.model]
        if request.reasoning_effort not in efforts:
            raise ValueError(f"the codex engine is locked to reasoning effort {' or '.join(efforts)} for {request.model}")
        if request.max_model_steps is not None:
            raise ValueError("Codex does not support --max-model-steps")

    @staticmethod
    def _session_file(identifier):
        """The retained rollout for a Codex thread, or None before its first turn has run."""
        matches = sorted((_home() / "sessions").glob(f"**/rollout-*-{identifier}.jsonl"))
        return matches[0] if matches else None

    def build_command(self, request):
        self.validate_turn(request)
        sandbox = "read-only" if request.read_only else "workspace-write"
        if request.steering or request.fork_session_id:
            if _yolo(request):
                sandbox = "danger-full-access"
            wrapper = Path(__file__).with_name("codex_appserver.py")
            command = [sys.executable, str(wrapper), "--binary", request.binary,
                       "--cwd", request.workspace, "--model", request.model,
                       "--effort", request.reasoning_effort, "--sandbox", sandbox]
            if request.fork_session_id:
                command.extend(["--fork", request.fork_session_id])
                if not request.steering:
                    command.extend(["--initial-prompt-file", request.prompt_file])
            elif request.session_id and self._session_file(request.session_id):
                command.extend(["--resume", request.session_id])
            if not request.session_logging:
                command.append("--ephemeral")
            if _use_user_config():
                command.append("--user-config")
            return command
        if _yolo(request):
            guard = ["--dangerously-bypass-approvals-and-sandbox"]
        else:
            guard = ["-c", f'sandbox_mode="{sandbox}"', "-c", 'approval_policy="never"']
        # Skills load independently; config.toml (including MCP servers) is opt-in.
        ignore_user_config = not _use_user_config()
        shared = [
            "--json", "--skip-git-repo-check",
            *(["--ignore-user-config"] if ignore_user_config else []), "--ignore-rules",
            "-m", request.model,
            "-c", f'model_reasoning_effort="{request.reasoning_effort}"',
            *guard,
            "-c", 'web_search="live"',
        ]
        if not request.session_logging:
            shared += ["--ephemeral"]
        prompt = Path(request.prompt_file).read_text(encoding="utf-8")
        # Codex picks its own thread ID. The supervisor swaps it into the job's session_id on
        # thread.started, so a follow-up carries a real thread whose rollout exists to resume.
        if request.session_id and self._session_file(request.session_id):
            return [request.binary, "exec", "resume", *shared, request.session_id, "--", prompt]
        return [request.binary, "exec", *shared, "--cd", request.workspace, "--", prompt]

    def dedupe_key(self, event):
        """Collapse Codex's repeated adjacent agent_message completion event."""
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        if event.get("type") != "item.completed" or item.get("type") != "agent_message":
            self._peek_message = None
            return None
        text = item.get("text")
        if not isinstance(text, str):
            self._peek_message = None
            return None
        identifier = item.get("id")
        identifier = identifier if isinstance(identifier, str) else None
        previous = self._peek_message
        if (previous is not None and previous[0] == text
                and (previous[1] is None or identifier is None or previous[1] == identifier)):
            return previous[2]
        key = (text, identifier)
        self._peek_message = (text, identifier, key)
        return key

    def describe_event(self, event) -> list[dict]:
        from claivelib.peek import compact

        kind = event.get("type")
        if not isinstance(kind, str):
            return super().describe_event(event)
        item = event.get("item")
        if isinstance(item, dict):
            item_kind = item.get("type")
            if kind == "item.completed" and item_kind == "agent_message" and isinstance(item.get("text"), str):
                return [{"kind": "text", "text": item["text"]}]
            if kind == "item.completed" and item_kind == "error":
                return [{"kind": "error", "text": str(item.get("message") or "Codex item failed")}]
            if item_kind in {"command_execution", "mcp_tool_call", "file_change"}:
                details = {key: value for key, value in item.items() if key not in {"id", "type"}}
                if kind == "item.started":
                    return [{"kind": "tool_call", "text":
                             ("shell" if item_kind == "command_execution" else item_kind) + " " + compact(details)}]
                if kind == "item.completed":
                    failed = item_kind == "command_execution" and item.get("exit_code") not in (None, 0)
                    return [{"kind": "tool_result", "text": ("error: " if failed else "") + compact(details)}]
        if kind == "turn.failed":
            error = event.get("error")
            return [{"kind": "error", "text": str(error.get("message") or "Codex turn failed")
                     if isinstance(error, dict) else "Codex turn failed"}]
        if kind in {"thread.started", "turn.started", "turn.completed"}:
            return []
        return super().describe_event(event)

    def normalize_event(self, event):
        kind = event.get("type")
        if not isinstance(kind, str):
            raise ValueError("Codex event has no string type")
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        item_kind = item.get("type")
        if kind == "control_response":
            return [{"type": "control_response", "native_kind": kind,
                     "id": event.get("id"), "command": event.get("command"),
                     "success": event.get("success"), "data": event.get("data"),
                     "error": event.get("error")}]
        if kind == "thread.started" and isinstance(event.get("thread_id"), str):
            self._last_text = ""
            return [{"type": "session_bound", "native_kind": kind, "session_id": event["thread_id"]}]
        if kind == "turn.started":
            self._last_text = ""
            return [{"type": "model_step", "native_kind": kind}]
        if kind == "item.started" and item_kind in {"command_execution", "mcp_tool_call", "file_change"}:
            return [{"type": "tool_started", "native_kind": kind, "tool": item.get("command") or item_kind, "tool_id": item.get("id")}]
        if kind == "item.completed":
            if item_kind == "agent_message" and isinstance(item.get("text"), str):
                self._last_text = item["text"]
                return [{"type": "output_delta", "native_kind": kind}]
            if item_kind == "command_execution" and item.get("exit_code") not in (None, 0):
                return [{"type": "task_warning", "native_kind": kind, "tool": "shell", "tool_id": item.get("id"),
                         "command": item.get("command"), "reason": f"exit {item['exit_code']}"}]
            if item_kind in {"command_execution", "mcp_tool_call", "file_change"}:
                return [{"type": "tool_completed", "native_kind": kind, "tool": item_kind, "tool_id": item.get("id")}]
        if kind == "turn.completed":
            return [{"type": "terminal_completed", "native_kind": kind, "terminal": "completed",
                     "reason": None, "text": self._last_text}]
        if kind == "turn.failed":
            error = event.get("error") if isinstance(event.get("error"), dict) else {}
            reason = str(error.get("message") or "Codex turn failed")
            result = [{"type": "terminal_failed", "native_kind": kind, "terminal": "failed",
                       "reason": reason, "text": ""}]
            if "usage limit" in reason.lower():
                result.append({"type": "quota_exhausted", "native_kind": kind})
            return result
        return [{"type": "activity", "native_kind": kind}]

    def session_usage(self, state):
        identifier = session_id(state)
        if not identifier or not launch_config(state).get("session_logging", True):
            raise ValueError("Codex usage needs a retained session")
        path = self._session_file(identifier)
        if path is None:
            raise ValueError("retained Codex session file is unavailable")
        calls, total = [], None
        model = launch_config(state).get("model") or CODEX_MODEL
        with path.open() as stream:
            for line in stream:
                record = json.loads(line)
                payload = record.get("payload") if isinstance(record, dict) else None
                info = payload.get("info") if isinstance(payload, dict) and payload.get("type") == "token_count" else None
                if not isinstance(info, dict):
                    continue
                total = info.get("total_token_usage")
                last = info.get("last_token_usage")
                if isinstance(last, dict):
                    calls.append(dict(model=model, **self._counters(last)))
        totals = self._counters(total) if isinstance(total, dict) else {}
        inputs = totals.get("input_tokens", 0)
        ratio = totals["cached_tokens"] / inputs if inputs and "cached_tokens" in totals else None
        return dict(model_calls=len(calls), calls=calls, totals=totals, cache_hit_ratio=ratio)

    @staticmethod
    def _counters(usage):
        mapping = {"input_tokens": "input_tokens", "cached_input_tokens": "cached_tokens",
                   "output_tokens": "output_tokens", "reasoning_output_tokens": "reasoning_tokens",
                   "cache_write_input_tokens": "cache_write_tokens"}
        return {target: usage[source] for source, target in mapping.items()
                if isinstance(usage.get(source), int) and not isinstance(usage.get(source), bool)
                and usage[source] >= 0}
