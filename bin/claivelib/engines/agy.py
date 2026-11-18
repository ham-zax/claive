"""Antigravity print-mode adapter. Conversation state belongs to the supervisor."""
import json
import os
from pathlib import Path
import shutil

from claivelib import config
from claivelib.engine import SOFT_READ_NOTE as READ_ONLY_NOTE, WorkerEngine
from claivelib.usage import aggregate_usage


AGY_MODEL = "gemini-3.8-flash-high"
AGY_MODEL_FAMILIES = {
    "gemini-3.8-flash": {"high": "gemini-3.8-flash-high", "medium": "gemini-3.8-flash-medium", "low": "gemini-3.8-flash-low"},
    "gemini-4-carbon": {"high": "gemini-4-carbon-high", "medium": "gemini-4-carbon-medium", "low": "gemini-4-carbon-low"},
    "gemini-4-argon": {"high": "gemini-4-argon-high", "medium": "gemini-4-argon-medium", "low": "gemini-4-argon-low"},
    "gemini-4-hydrogen": {"xhigh": "gemini-4-hydrogen-xhigh", "high": "gemini-4-hydrogen-high", "medium": "gemini-4-hydrogen-medium", "low": "gemini-4-hydrogen-low"},
}
AGY_FAMILY_DEFAULTS = {
    "gemini-3.8-flash": "high",
    "gemini-4-carbon": "high",
    "gemini-4-argon": "high",
    "gemini-4-hydrogen": "high",
}


def resolve_agy_model(model: str | None, reasoning_effort: str | None = None) -> str:
    if model is None:
        if reasoning_effort and reasoning_effort in AGY_MODEL_FAMILIES["gemini-3.8-flash"]:
            return AGY_MODEL_FAMILIES["gemini-3.8-flash"][reasoning_effort]
        return AGY_MODEL
    if model in AGY_MODEL_FAMILIES:
        eff = reasoning_effort or AGY_FAMILY_DEFAULTS[model]
        if eff not in AGY_MODEL_FAMILIES[model]:
            raise ValueError(
                f"AGY model family {model} supports efforts {', '.join(AGY_MODEL_FAMILIES[model])}, not {eff}"
            )
        return AGY_MODEL_FAMILIES[model][eff]
    return model
AGY_MODELS = {
    "gemini-3.8-flash-high": ("high",),
    "gemini-3.8-flash-medium": ("medium",),
    "gemini-3.8-flash-low": ("low",),
    "gemini-4-carbon-high": ("high",),
    "gemini-4-carbon-medium": ("medium",),
    "gemini-4-carbon-low": ("low",),
    "gemini-4-argon-high": ("high",),
    "gemini-4-argon-medium": ("medium",),
    "gemini-4-argon-low": ("low",),
    "gemini-4-hydrogen-xhigh": ("xhigh",),
    "gemini-4-hydrogen-high": ("high",),
    "gemini-4-hydrogen-medium": ("medium",),
    "gemini-4-hydrogen-low": ("low",),
    "gemini-pro-orchestrator": ("high",),
    "gemini-pro-orchestrator-argon": ("high",),
    "claude-sonnet-4-6": ("high",),
    "claude-opus-4-6-thinking": ("high",),
    "gpt-oss-120b-medium": ("medium",),
}
PROMPT_LIMIT = 100000


def default_binary():
    local = Path.home() / ".local/bin/agy"
    return os.environ.get("AGY_WORKER_BINARY", str(local if local.is_file() else shutil.which("agy") or local))


class AgyEngine(WorkerEngine):
    name = "agy"
    default_model = AGY_MODEL
    handles_read_only_prompt = True
    allows_startup_banner = True

    def supported_models(self):
        return dict(AGY_MODELS)

    def defaults(self):
        return dict(super().defaults(), binary=default_binary(), provider="antigravity",
                    default_model=AGY_MODEL, prompt_limit=PROMPT_LIMIT)

    def default_effort_for(self, model):
        if model is None:
            model = AGY_MODEL
        elif model in AGY_MODEL_FAMILIES:
            model = resolve_agy_model(model)
        catalog = config.supported_models(self.name, self) or {}
        if model not in catalog:
            raise ValueError(f"unsupported AGY model: {model}; choose {', '.join(catalog)}")
        return catalog[model][0]

    def fallback_effort_for(self, model, current):
        return self.default_effort_for(model)

    def resolve_session_id(self, value, session_logging=True):
        if value is not None:
            raise ValueError("AGY does not support importing --session-id; use followup on a retained worker")
        if not session_logging:
            raise ValueError("AGY does not support --no-session-log")
        return None

    def resolve_launch(self, **options):
        self.resolve_session_id(options.get("session_id"), options.get("session_logging", True))
        model = resolve_agy_model(options.get("model") or None, options.get("reasoning_effort"))
        return dict(binary=config.engine_setting(self.name, "binary", default_binary()),
                    provider=options.get("provider") or config.engine_setting(self.name, "provider", "antigravity"),
                    model=model,
                    read_only=bool(options.get("read_only")), web=bool(options.get("web")),
                    output_schema=options.get("output_schema"),
                    session_logging=options.get("session_logging", True),
                    isolation=dict(options.get("isolation") or {}),
                    steering=bool(options.get("steering")))

    def validate_launch(self, launch):
        binary = Path(launch["binary"])
        if not binary.is_absolute() or not binary.is_file() or not os.access(binary, os.X_OK):
            raise ValueError("AGY binary must be an absolute executable file")
        self.default_effort_for(launch.get("model"))
        if launch.get("output_schema"):
            self._validate_schema(launch["output_schema"])
        self._unsupported(launch)
        self.validate_isolation(launch)

    @staticmethod
    def _validate_schema(path):
        schema = Path(str(path))
        if not schema.is_absolute() or not schema.is_file():
            raise ValueError("--output-schema must be an existing absolute file")
        try:
            loaded = json.loads(schema.read_text(encoding="utf-8"))
        except ValueError as error:
            raise ValueError(f"--output-schema must hold a JSON Schema object: {error}") from error
        if not isinstance(loaded, dict):
            raise ValueError("--output-schema must hold a JSON Schema object")
        return str(schema)

    @staticmethod
    def _unsupported(options):
        for key, flag in (("web", "--web"), ("steering", "--enable-steering"),
                          ("max_model_steps", "--max-model-steps"),
                          ("fork_session_id", "fork"), ("budget", "budget")):
            if options.get(key) is not None and options.get(key) is not False:
                raise ValueError(f"AGY does not support {flag}")
        if not options.get("session_logging", True):
            raise ValueError("AGY does not support --no-session-log")
        if options.get("provider") not in (None, "antigravity"):
            raise ValueError("AGY does not support provider selection")

    def validate_turn(self, request):
        catalog = config.supported_models(self.name, self) or {}
        model = AGY_MODEL if request.model is None else request.model
        if model not in catalog:
            raise ValueError(f"unsupported AGY model: {model}; choose {', '.join(catalog)}")
        effort = catalog[model][0]
        if request.reasoning_effort is not None and request.reasoning_effort != effort:
            raise ValueError(f"AGY model {request.model} requires effort {effort}, not {request.reasoning_effort}")
        if request.output_schema:
            self._validate_schema(request.output_schema)
        self._unsupported(vars(request))

    def build_command(self, request):
        self.validate_turn(request)
        prompt = Path(request.prompt_file).read_text(encoding="utf-8")
        self.validate_prompt(prompt, request.read_only)
        if request.read_only:
            prompt = READ_ONLY_NOTE + "\n\n" + prompt + "\n\n" + READ_ONLY_NOTE
        command = [request.binary, "--output-format", "stream-json", "--model", request.model]
        if os.environ.get("CLAIVE_AGY_YOLO", "").lower() not in {"0", "false", "no", "off"}:
            command.append("--dangerously-skip-permissions")
        if request.output_schema:
            command += ["--json-schema", self._validate_schema(request.output_schema)]
        if request.session_id:
            command += ["--conversation", request.session_id]
        return command + ["-p", prompt]

    def validate_prompt(self, text, read_only=False):
        if read_only:
            text = READ_ONLY_NOTE + "\n\n" + text + "\n\n" + READ_ONLY_NOTE
        size = len(text.encode("utf-8"))
        limit = config.engine_setting(self.name, "prompt_limit", PROMPT_LIMIT)
        if size > limit:
            raise ValueError(f"AGY prompt is {size} UTF-8 bytes; limit is {limit} bytes")

    def normalize_event(self, event):
        kind = event.get("event")
        if not isinstance(kind, str) or not isinstance(event.get(kind), dict):
            raise ValueError("AGY event needs a matching object envelope")
        body = event[kind]
        identifier = event.get("conversation_id") if kind == "init" else body.get("conversation_id")
        mapped = []
        if identifier is not None:
            if not isinstance(identifier, str) or not identifier.strip():
                raise ValueError("invalid AGY conversation id")
            mapped.append(dict(type="session_bound", session_id=identifier, strict=True, native_kind=kind))
        activity = dict(type="activity", native_kind=kind)
        if kind == "init":
            activity["text"] = f"{body.get('model', '')} cwd={body.get('cwd', '')}"
            return mapped + [activity]
        if kind == "step_update":
            step_type, state = body.get("step_type"), body.get("state")
            if not isinstance(step_type, str) or not isinstance(state, str):
                raise ValueError("invalid AGY step type or state")
            index = body.get("step_index")
            if not isinstance(index, int) or isinstance(index, bool) or index < 0:
                raise ValueError("invalid AGY step index")
            if body.get("text_delta") is not None:
                if not isinstance(body["text_delta"], str):
                    raise ValueError("invalid AGY text delta")
                if body["text_delta"]:
                    mapped.append(dict(type="output_delta", text=body["text_delta"], native_kind=kind))
            if step_type == "agent_response" and state == "DONE" and isinstance(body.get("usage"), dict):
                mapped.append(dict(type="model_step", native_kind=kind))
            if step_type in {"tool", "subagent"} and body.get("tool_name") and state in {"ACTIVE", "DONE"}:
                entry = dict(type="tool_started" if state == "ACTIVE" else "tool_completed",
                             tool=body["tool_name"], tool_id=f"{identifier}:{index}", native_kind=kind)
                info = body.get("tool_info")
                params = info.get("parameters") if isinstance(info, dict) and isinstance(info.get("parameters"), dict) else {}
                cmd = params.get("CommandLine") or params.get("command")
                if isinstance(cmd, str) and cmd:
                    entry["command"] = cmd
                path = params.get("Path") or params.get("TargetFile") or params.get("AbsolutePath") or params.get("file_path")
                if isinstance(path, str) and path:
                    entry["path"] = path
                mapped.append(entry)
            return mapped or [activity]
        if kind != "result":
            return mapped + [activity]
        if not identifier:
            raise ValueError("AGY result has no conversation id")
        status, reason = body.get("status"), body.get("finished_reason")
        failure = None
        if reason == "print_timeout":
            failure = "timeout"
        elif (reason not in (None, "", "completed", "interrupted", "error")
              or not isinstance(status, str) or status not in {"SUCCESS", "ERROR"}):
            failure = "protocol"
        elif status == "ERROR" or reason in {"interrupted", "error"}:
            failure = "worker"
        text = body.get("response", "")
        if not isinstance(text, str):
            raise ValueError("invalid AGY result response")
        mapped.append(dict(type="terminal_failed" if failure else "terminal_completed",
                           terminal="failed" if failure else "completed", text=text,
                           reason=reason or (status if failure else None), failure_kind=failure,
                           usage=body.get("usage"), usage_cumulative=True,
                           conversation_id=identifier, native_kind=kind))
        return mapped

    def describe_event(self, event):
        kind = event.get("event")
        body = event.get(kind) if isinstance(kind, str) else None
        if not isinstance(body, dict):
            return super().describe_event(event)
        if kind == "step_update":
            if body.get("text_delta"):
                return [dict(kind="text", text=body["text_delta"])]
            if body.get("tool_name"):
                return [dict(kind="tool_call" if body.get("state") == "ACTIVE" else "tool_result",
                             text=body["tool_name"])]
            return []
        if kind == "result":
            try:
                terminal = self.normalize_event(event)[-1]
            except (ValueError, TypeError) as error:
                return [dict(kind="error", text=f"malformed AGY result: {error}")]
            if terminal["terminal"] == "failed":
                return [dict(kind="error", text=str(terminal.get("reason") or "AGY failed"))]
            return [dict(kind="text", text=body["response"])] if body.get("response") else []
        return []

    def dedupe_key(self, event):
        # One index can carry multiple deltas and both tool boundaries. Dropping
        # adjacent events with that index loses real output, even within one turn.
        return None

    def session_usage(self, state):
        return aggregate_usage(state)
