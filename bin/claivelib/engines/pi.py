"""Pi 1.0 JSON transport and retained sessions, independent of worker supervision."""
import json
import os
from pathlib import Path
import re
import tempfile
import time

from claivelib.engine import WorkerEngine
from claivelib.state import launch_config, session_id


def default_binary():
    return os.environ.get("PI_WORKER_BINARY", str(Path.home() / ".local/bin/pi"))


class PiEngine(WorkerEngine):
    name = "pi"
    default_reasoning_effort = "max"  # free models; Pi clamps to each model's highest level
    efforts = {"off", "minimal", "low", "medium", "high", "xhigh", "max"}

    def __init__(self):
        self._last_assistant = None
        self._selected_model = None

    @staticmethod
    def _settings_path():
        directory = os.environ.get("PI_CODING_AGENT_DIR", str(Path.home() / ".pi/agent"))
        return Path(directory).expanduser().resolve() / "settings.json"

    @classmethod
    def _settings(cls, model=None):
        """Share Pi's global preference, using its settings.json.lock directory."""
        path = cls._settings_path()
        if model is None and not path.exists():
            return {}
        path.parent.mkdir(parents=True, exist_ok=True)
        lock = path.with_name(path.name + ".lock")
        for attempt in range(10):
            try:
                lock.mkdir()
                break
            except FileExistsError:
                if attempt == 9:
                    raise ValueError(f"Pi settings are locked: {path}")
                time.sleep(0.02)
        temporary = None
        try:
            settings = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
            if not isinstance(settings, dict):
                raise ValueError(f"Pi settings must be an object: {path}")
            if model is not None:
                settings.update(defaultProvider="opencode2api", defaultModel=model)
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                                 prefix=".settings-", delete=False) as stream:
                    temporary = Path(stream.name)
                    json.dump(settings, stream, indent=2, ensure_ascii=False)
                    stream.write("\n")
                temporary.replace(path)
            return settings
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            lock.rmdir()

    def resolve_session_id(self, value, session_logging=True):
        identifier = super().resolve_session_id(value, session_logging)
        if identifier and not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?", identifier):
            raise ValueError("Pi session IDs must start/end with a letter or number and contain only letters, numbers, '.', '_', '-'")
        return identifier

    def resolve_launch(self, **options):
        identifier = options.get("session_id")
        settings = self._settings()
        remembered = settings.get("defaultModel") if settings.get("defaultProvider") == "opencode2api" else None
        self._selected_model = options.get("model")
        return {
            "binary": default_binary(),
            "provider": options.get("provider") or "opencode2api",
            "model": options.get("model") or remembered or "muse-spark-1.3-contributor-free",
            "read_only": bool(options.get("read_only")),
            "web": bool(options.get("web")),
            "output_schema": options.get("output_schema"),
            "session_logging": bool(options.get("session_logging", True)),
            "session_dir": str(Path(options["session_root"]) / "pi" / identifier) if identifier else None,
            "isolation": dict(options.get("isolation") or {}),
        }

    def validate_launch(self, launch):
        binary = Path(launch["binary"])
        if not binary.is_absolute() or not binary.is_file() or not os.access(binary, os.X_OK):
            raise ValueError("Pi binary must be an absolute executable file")
        if launch.get("output_schema"):
            raise ValueError("Pi does not support --output-schema")
        if launch.get("web"):
            raise ValueError("Pi does not support --web in this adapter")
        isolation = launch.get("isolation") or {}
        if isolation.get("mode", "none") != "none" or isolation.get("base"):
            raise ValueError("Pi does not manage worktrees; pass an externally retained worktree as --workspace")

    def validate_turn(self, request):
        if request.provider != "opencode2api":
            raise ValueError("Pi currently supports only the opencode2api provider")
        if request.reasoning_effort not in self.efforts:
            raise ValueError(f"unsupported Pi reasoning effort: {request.reasoning_effort}")
        if request.max_model_steps is not None:
            raise ValueError("Pi does not support --max-model-steps")

    @staticmethod
    def _session_file(directory, identifier, workspace):
        """Locate the exact retained session, refusing cross-workspace reuse."""
        matches = []
        for path in sorted(Path(directory).glob("*.jsonl")):
            try:
                with path.open() as stream:
                    header = json.loads(stream.readline())
            except (OSError, ValueError) as error:
                raise ValueError(f"cannot read Pi session header: {path}") from error
            if not isinstance(header, dict) or header.get("type") != "session":
                raise ValueError(f"invalid Pi session header: {path}")
            if header.get("id") != identifier:
                continue
            if not isinstance(header.get("cwd"), str) or Path(header["cwd"]).resolve() != Path(workspace).resolve():
                raise ValueError("Pi session belongs to a different workspace")
            matches.append(path)
        if len(matches) > 1:
            raise ValueError("multiple Pi session files share this session ID")
        return matches[0] if matches else None

    def build_command(self, request):
        self.validate_turn(request)
        command = [
            request.binary, "--mode", "json", "--print", "--offline",
            "--no-extensions", "--no-approve", "--provider", request.provider,
            "--model", request.model, "--thinking", request.reasoning_effort,
            "--tools", "read,grep,find,ls" if request.read_only else "read,bash,edit,write,grep,find,ls",
        ]
        if request.session_logging:
            if not request.session_id or not request.session_dir or not Path(request.session_dir).is_absolute():
                raise ValueError("Pi requires a session ID and absolute session directory")
            retained = self._session_file(request.session_dir, request.session_id, request.workspace)
            command += ["--session-dir", request.session_dir]
            command += ["--session", str(retained)] if retained else ["--session-id", request.session_id]
        else:
            command += ["--no-session"]
        # Pi expands @files after '--'; no shell interpolation or prompt argv copying.
        command += ["--", "@" + request.prompt_file]
        # Remember only an explicit selection after all command checks pass.
        # Follow-ups keep their original launch model without resetting the default.
        if self._selected_model is not None:
            self._settings(self._selected_model)
            self._selected_model = None
        return command

    def normalize_event(self, event):
        kind = event.get("type")
        if not isinstance(kind, str):
            raise ValueError("Pi event has no string type")
        activity = {"type": "activity", "native_kind": kind}
        if kind in {"session", "agent_start"}:
            self._last_assistant = None
        elif kind == "message_start":
            message = event.get("message")
            if isinstance(message, dict) and message.get("role") == "assistant":
                return [{"type": "model_step", "native_kind": kind}]
        elif kind == "message_update":
            delta = event.get("assistantMessageEvent")
            if isinstance(delta, dict) and delta.get("type") == "text_delta":
                return [{"type": "output_delta", "native_kind": kind}]
        elif kind == "message_end":
            message = event.get("message")
            if isinstance(message, dict) and message.get("role") == "assistant":
                self._last_assistant = message
        elif kind == "tool_execution_start":
            return [{"type": "tool_started", "native_kind": kind, "tool": event.get("toolName", "tool")}]
        elif kind == "tool_execution_end" and event.get("isError"):
            result = event.get("result") if isinstance(event.get("result"), dict) else {}
            reason = " ".join(part.get("text", "") for part in result.get("content", [])
                              if isinstance(part, dict) and part.get("type") == "text")
            return [{"type": "task_warning", "native_kind": kind,
                     "tool": event.get("toolName", "tool"), "reason": reason}]
        elif kind == "agent_settled":
            message = self._last_assistant or {}
            stop = message.get("stopReason")
            completed = stop == "stop"
            content = message.get("content", [])
            if not isinstance(content, list) or any(not isinstance(block, dict) for block in content):
                raise ValueError("invalid Pi assistant content")
            text = "".join(block.get("text", "") for block in content if block.get("type") == "text")
            return [{
                "type": "terminal_completed" if completed else "terminal_failed",
                "native_kind": kind,
                "terminal": "completed" if completed else "failed",
                "reason": None if completed else message.get("errorMessage") or f"Pi assistant outcome: {stop or 'missing'}",
                "text": text,
            }]
        return [activity]

    @staticmethod
    def _usage_call(message):
        usage = message.get("usage")
        if not isinstance(usage, dict):
            return None
        values = {key: value for key, value in usage.items()
                  if isinstance(value, int) and not isinstance(value, bool) and value >= 0}
        call = {"model": message.get("model")}
        mapping = {"output": "output_tokens", "cacheRead": "cached_tokens", "reasoning": "reasoning_tokens",
                   "cacheWrite": "cache_write_tokens"}
        call.update({target: values[source] for source, target in mapping.items() if source in values})
        if "cacheRead" in values:
            call["cache_read_tokens"] = values["cacheRead"]
        # Pi's input excludes cache reads/writes. Normalize to total input supplied.
        if all(key in values for key in ("input", "cacheRead", "cacheWrite")):
            call["input_tokens"] = values["input"] + values["cacheRead"] + values["cacheWrite"]
        return call

    def session_usage(self, state):
        launch = launch_config(state)
        identifier = session_id(state)
        directory = launch.get("session_dir")
        if not identifier or not directory or not launch.get("session_logging"):
            raise ValueError("Pi usage needs a retained session")
        path = self._session_file(directory, identifier, state["workspace"])
        if path is None:
            raise ValueError("retained Pi session file is unavailable")
        calls = []
        with path.open() as stream:
            for line in stream:
                record = json.loads(line)
                message = record.get("message", {})
                if record.get("type") != "message" or message.get("role") != "assistant":
                    continue
                call = self._usage_call(message)
                if call is not None:
                    calls.append(call)
        totals = {key: sum(call[key] for call in calls if key in call)
                  for key in ("input_tokens", "output_tokens", "cached_tokens", "reasoning_tokens")
                  if any(key in call for call in calls)}
        complete = bool(calls) and all("input_tokens" in call and "cached_tokens" in call for call in calls)
        inputs = totals.get("input_tokens", 0)
        ratio = totals["cached_tokens"] / inputs if complete and inputs else None
        return dict(model_calls=len(calls), calls=calls, totals=totals, cache_hit_ratio=ratio)
