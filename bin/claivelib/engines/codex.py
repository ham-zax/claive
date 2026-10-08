"""Codex CLI engine (`codex exec --json`), strictly locked to GPT-6 Luna at max effort."""
import json
import os
from pathlib import Path
import uuid

from claivelib.engine import WorkerEngine
from claivelib.state import launch_config, session_id

CODEX_MODEL = "gpt-6-luna"
CODEX_PROVIDER = "openai"
CODEX_EFFORT = "max"


def default_binary():
    return os.environ.get("CODEX_WORKER_BINARY", str(Path.home() / ".nvm/versions/node/v24.19.0/bin/codex"))


def _home():
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()


def _yolo(request):
    """CLAIVE_CODEX_YOLO=1 lets write turns skip Codex's sandbox and approvals; read-only turns never do."""
    return not request.read_only and os.environ.get("CLAIVE_CODEX_YOLO", "").lower() in {"1", "true", "yes"}


class CodexEngine(WorkerEngine):
    name = "codex"
    default_model = CODEX_MODEL
    default_reasoning_effort = CODEX_EFFORT

    def __init__(self):
        self._last_text = ""

    def resolve_session_id(self, value, session_logging=True):
        if not session_logging:
            return None
        return str(uuid.UUID(value)) if value else str(uuid.uuid4())

    def resolve_launch(self, **options):
        return {
            "binary": default_binary(),
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
        if model != CODEX_MODEL:
            raise ValueError(f"the codex engine is locked to {CODEX_MODEL}, not {model}")

    def validate_turn(self, request):
        self._check_model(request.model)
        if request.provider != CODEX_PROVIDER:
            raise ValueError(f"the codex engine supports only the {CODEX_PROVIDER} provider")
        if request.reasoning_effort != CODEX_EFFORT:
            raise ValueError(f"the codex engine is locked to reasoning effort {CODEX_EFFORT}")
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
        if _yolo(request):
            guard = ["--dangerously-bypass-approvals-and-sandbox"]
        else:
            guard = ["-c", f'sandbox_mode="{sandbox}"', "-c", 'approval_policy="never"']
        # Default remains hermetic. Explicitly opt in for deployments that register
        # a narrow MCP browser server with `codex mcp add` and user-installed skills.
        # Keep this independent of the growth project and the browser provider.
        ignore_user_config = os.environ.get("CLAIVE_CODEX_USE_USER_CONFIG", "").lower() not in {"1", "true", "yes"}
        shared = [
            "--json", "--skip-git-repo-check",
            *(["--ignore-user-config"] if ignore_user_config else []), "--ignore-rules",
            "-m", request.model,
            "-c", f'model_reasoning_effort="{CODEX_EFFORT}"',
            *guard,
            "-c", f'web_search="{"live" if request.web else "disabled"}"',
        ]
        if not request.session_logging:
            shared += ["--ephemeral"]
        prompt = Path(request.prompt_file).read_text(encoding="utf-8")
        # Codex picks its own thread ID. The supervisor swaps it into the job's session_id on
        # thread.started, so a follow-up carries a real thread whose rollout exists to resume.
        if request.session_id and self._session_file(request.session_id):
            return [request.binary, "exec", "resume", *shared, request.session_id, "--", prompt]
        return [request.binary, "exec", *shared, "--cd", request.workspace, "--", prompt]

    def normalize_event(self, event):
        kind = event.get("type")
        if not isinstance(kind, str):
            raise ValueError("Codex event has no string type")
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        item_kind = item.get("type")
        if kind == "thread.started" and isinstance(event.get("thread_id"), str):
            self._last_text = ""
            return [{"type": "session_bound", "native_kind": kind, "session_id": event["thread_id"]}]
        if kind == "turn.started":
            self._last_text = ""
            return [{"type": "model_step", "native_kind": kind}]
        if kind == "item.started" and item_kind in {"command_execution", "mcp_tool_call", "file_change"}:
            return [{"type": "tool_started", "native_kind": kind, "tool": item.get("command") or item_kind}]
        if kind == "item.completed":
            if item_kind == "agent_message" and isinstance(item.get("text"), str):
                self._last_text = item["text"]
                return [{"type": "output_delta", "native_kind": kind}]
            if item_kind == "command_execution" and item.get("exit_code") not in (None, 0):
                return [{"type": "task_warning", "native_kind": kind, "tool": "shell",
                         "reason": f"exit {item['exit_code']}"}]
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
                    calls.append(dict(model=CODEX_MODEL, **self._counters(last)))
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
