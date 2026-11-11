"""Pi 1.0 JSON transport and retained sessions, independent of worker supervision."""
import json
import os
from pathlib import Path
import re
import shlex
import tempfile
import time

from claivelib import config, provider
from claivelib.engine import SteeringCapability, WorkerEngine
from claivelib.state import launch_config, session_id

# Pi has no CLI read-only mode: read-only turns keep the full tools and get this request
# in the system prompt (before the task) and after the task text. The model, not Pi, enforces it.
READ_ONLY_NOTE = ("This is a read-only turn. Do not create, change or delete files, and do not run "
                  "commands that write to the workspace or to git. Read, search and run read-only "
                  "commands only, then report.")

WRITE_CHUNK_NOTE = ("Keep write/edit tool calls small: write or edit at most 150 lines per call. "
                    "Split larger changes into successive chunks.")


def _benign_bash_probe(command, reason):
    """Recognize silent predicate failures, without interpreting general shell programs."""
    if not isinstance(command, str) or not re.fullmatch(
            r"\s*(?:\(no output\)\s*)?Command exited with code 1\s*", reason):
        return False
    # Reject expansion, redirects, pipelines and other shell constructs even inside
    # quotes. False negatives are preferable to hiding an actual tool failure.
    if any(char in command for char in "$`\n\r;|<>(){}\\#"):
        return False
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars="&")
        lexer.whitespace_split = True
        words = list(lexer)
    except ValueError:
        return False
    # A single, silent cd prefix is common in Pi searches. Any other compound is
    # ambiguous: its exit 1 may belong to an earlier command.
    if "&&" in words:
        if (words.count("&&") != 1 or words.index("&&") != 2 or words[0] != "cd"
                or not words[1] or words[1].startswith("-")
                or any(char in words[1] for char in "*?[]~")):
            return False
        words = words[3:]
    if not words or any("&" in word for word in words):
        return False
    tool, *args = words
    if tool in {"rg", "grep"}:
        # Only options whose exit 1 means no match; no preprocessors, output files,
        # inverted file listing, or arbitrary options with operands.
        flags = {"--line-number", "--ignore-case", "--fixed-strings", "--quiet",
                 "--files-with-matches", "--word-regexp", "--invert-match"}
        while args and args[0].startswith("-"):
            flag = args.pop(0)
            if flag == "--":
                break
            short = "niFqlwvx" + ("EHhrsR" if tool == "grep" else "HhsSU")
            if flag not in flags and not re.fullmatch(r"-[" + short + r"]+", flag):
                return False
        return bool(args) and all(not arg.startswith("-") for arg in args[1:])
    if tool == "git":
        return args in (["diff", "--quiet"], ["diff", "--quiet", "--exit-code"],
                        ["diff", "--cached", "--quiet"], ["diff", "--staged", "--quiet"])
    if tool == "[":
        if not args or args.pop() != "]":
            return False
    elif tool != "test":
        return False
    if any(any(char in arg for char in "*?[]~") for arg in args):
        return False
    if len(args) == 1:
        return not args[0].startswith("-") and args[0] != "!"
    if len(args) == 2:
        return args[0] in {"-e", "-f", "-d", "-s", "-r", "-w", "-x", "-L", "-h", "-n", "-z"}
    if len(args) == 3:
        if args[1] in {"=", "==", "!="}:
            return all(not arg.startswith("-") and arg != "!" for arg in (args[0], args[2]))
        return (args[1] in {"-eq", "-ne", "-lt", "-le", "-gt", "-ge"}
                and all(re.fullmatch(r"-?[0-9]+", arg) for arg in (args[0], args[2])))
    return False

STALE_LOCK_SECONDS = 10


def default_binary():
    return os.environ.get("PI_WORKER_BINARY", str(Path.home() / ".local/bin/pi"))


class PiEngine(WorkerEngine):
    handles_read_only_prompt = True
    name = "pi"
    supports_fork = True
    efforts = {"off", "minimal", "low", "medium", "high", "xhigh", "max"}

    def defaults(self):
        return dict(super().defaults(), binary=default_binary(),
                    provider=config.pi_default_provider(),
                    default_model=provider.FALLBACK_MODEL)

    def steering_capability(self):
        return SteeringCapability(True, "pi-rpc", notes=(
            "Enabled by default for writers; input is queued at the next model boundary.",
            "Provider acknowledgement does not prove the model consumed the input.",))

    def initial_input(self, request):
        if not request.steering:
            return None
        message = Path(request.prompt_file).read_text()
        if request.read_only:
            message = message.rstrip("\n") + "\n\n" + READ_ONLY_NOTE + "\n"
        return json.dumps({"type": "prompt", "id": "claive-initial", "message": message}) + "\n"

    def __init__(self):
        self._last_assistant = None
        self._selected = None
        self._tool_calls = {}

    @staticmethod
    def _settings_path():
        directory = os.environ.get("PI_CODING_AGENT_DIR", str(Path.home() / ".pi/agent"))
        return Path(directory).expanduser().resolve() / "settings.json"

    @classmethod
    def _settings(cls, model=None, provider_name=None):
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
                try:
                    # Like Pi's lockfile: a lock untouched for 10s belongs to a crashed writer.
                    if time.time() - lock.stat().st_mtime > STALE_LOCK_SECONDS:
                        lock.rmdir()
                        continue
                except OSError:
                    pass
                if attempt == 9:
                    raise ValueError(f"Pi settings are locked: {path}")
                time.sleep(0.02)
        temporary = None
        try:
            settings = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
            if not isinstance(settings, dict):
                raise ValueError(f"Pi settings must be an object: {path}")
            if model is not None:
                settings.update(defaultProvider=provider_name or provider.PROVIDER, defaultModel=model)
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
        chosen = options.get("provider") or config.pi_default_provider()
        remembered = settings.get("defaultModel") if settings.get("defaultProvider") == chosen else None
        # Only a model the user chose (--model) becomes Pi's shared default; role models,
        # batch stage models and timeout fallbacks are per-worker choices.
        self._selected = (chosen, options.get("model")) if options.get("remember_model") and options.get("model") else None
        launch = {
            "binary": config.engine_setting(self.name, "binary", default_binary()),
            "steering": bool(options.get("steering")),
            "provider": chosen,
            "model": options.get("model") or remembered or provider.FALLBACK_MODEL,
            "read_only": bool(options.get("read_only")),
            "web": bool(options.get("web")),
            "output_schema": options.get("output_schema"),
            "session_logging": bool(options.get("session_logging", True)),
            "session_dir": str(Path(options["session_root"]) / "pi" / identifier) if identifier else None,
            "isolation": dict(options.get("isolation") or {}),
        }
        override = provider.override_url(chosen)
        if override:
            launch["provider_base_url"] = override  # visible in the job record; applied per turn
        return launch

    def validate_launch(self, launch):
        binary = Path(launch["binary"])
        if not binary.is_absolute() or not binary.is_file() or not os.access(binary, os.X_OK):
            raise ValueError("Pi binary must be an absolute executable file")
        if launch.get("output_schema"):
            raise ValueError("Pi does not support --output-schema")
        if launch.get("web"):
            raise ValueError("Pi does not support --web in this adapter")
        self.validate_isolation(launch)

    def validate_turn(self, request):
        if not isinstance(request.provider, str) or not request.provider.strip():
            raise ValueError("Pi needs a provider name")
        if request.reasoning_effort is not None and request.reasoning_effort not in self.efforts:
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
            request.binary, "--mode", "rpc" if request.steering else "json", *([] if request.steering else ["--print"]), "--offline",
            "--no-extensions", "--no-prompt-templates", "--no-themes", "--no-approve", "--provider", request.provider,
            "--model", request.model,
            "--tools", "*",
        ]
        if request.reasoning_effort is not None:
            command += ["--thinking", request.reasoning_effort]
        if request.read_only:
            command += ["--append-system-prompt", READ_ONLY_NOTE]
        else:
            command += ["--append-system-prompt", WRITE_CHUNK_NOTE]
        if request.session_logging:
            if not request.session_id or not request.session_dir or not Path(request.session_dir).is_absolute():
                raise ValueError("Pi requires a session ID and absolute session directory")
            command += ["--session-dir", request.session_dir]
            if request.fork_session_id:
                if not request.fork_session_dir:
                    raise ValueError("Pi fork needs the source session directory")
                source = self._session_file(request.fork_session_dir, request.fork_session_id, request.workspace)
                if source is None:
                    raise ValueError("retained Pi session to fork is unavailable")
                command += ["--fork", str(source)]
            else:
                retained = self._session_file(request.session_dir, request.session_id, request.workspace)
                command += ["--session", str(retained)] if retained else ["--session-id", request.session_id]
        else:
            command += ["--no-session"]
        # Pi expands @files after '--'; no shell interpolation or prompt argv copying.
        if not request.steering:
            command += ["--", "@" + request.prompt_file, *([READ_ONLY_NOTE] if request.read_only else [])]
        # Remember only an explicit selection after all command checks pass.
        # Follow-ups keep their original launch model without resetting the default.
        if self._selected is not None:
            self._settings(self._selected[1], self._selected[0])
            self._selected = None
        return command

    def describe_event(self, event) -> list[dict]:
        from claivelib.peek import content_text, compact

        kind = event.get("type")
        if not isinstance(kind, str):
            return super().describe_event(event)
        if kind == "message_end":
            message = event.get("message")
            if isinstance(message, dict) and message.get("role") == "assistant":
                if message.get("stopReason") in {"error", "aborted"}:
                    return [{"kind": "error", "text": str(message.get("errorMessage") or
                                                          message["stopReason"])}]
                text = content_text(message.get("content"))
                return [{"kind": "text", "text": text}] if text else []
            return []
        if kind == "tool_execution_start":
            return [{"kind": "tool_call", "text": str(event.get("toolName", "tool")) +
                     " " + compact(event.get("args", {}))}]
        if kind == "tool_execution_end":
            result = event.get("result")
            text = content_text(result.get("content")) if isinstance(result, dict) else ""
            return [{"kind": "tool_result", "text": ("error: " if event.get("isError") else "") + text}]
        if kind == "response" and event.get("success") is False:
            return [{"kind": "error", "text": str(event.get("error") or "Pi prompt rejected")}]
        if kind in {"message_start", "message_update", "agent_start", "agent_end",
                    "agent_settled", "turn_start", "turn_end", "session"}:
            return []
        return super().describe_event(event)

    def normalize_event(self, event):
        kind = event.get("type")
        if not isinstance(kind, str):
            raise ValueError("Pi event has no string type")
        activity = {"type": "activity", "native_kind": kind}
        if kind == "response":
            if event.get("id") == "claive-initial" and event.get("success") is False:
                return [{"type": "terminal_failed", "native_kind": kind, "terminal": "failed",
                         "reason": event.get("error", "Pi prompt rejected"), "text": ""}]
            return [{"type": "control_response", "native_kind": kind, "id": event.get("id"),
                     "command": event.get("command"), "success": event.get("success"),
                     "data": event.get("data"), "error": event.get("error")}]
        if kind == "session":
            self._last_assistant = None
            self._tool_calls.clear()
            if isinstance(event.get("id"), str):
                return [{"type": "session_bound", "native_kind": kind, "session_id": event["id"]}]
        elif kind == "agent_start":
            self._last_assistant = None
            self._tool_calls.clear()
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
            args = event.get("args") if isinstance(event.get("args"), dict) else {}
            tool_id = event.get("toolCallId")
            if isinstance(tool_id, str):
                self._tool_calls[tool_id] = (event.get("toolName", "tool"), dict(args))
            return [{"type": "tool_started", "native_kind": kind, "tool": event.get("toolName", "tool"),
                     "tool_id": tool_id, "command": args.get("command")}]
        elif kind == "tool_execution_end":
            tool_id = event.get("toolCallId")
            tool, args = self._tool_calls.pop(tool_id, ("tool", {})) if isinstance(tool_id, str) else ("tool", {})
            end_args = event.get("args") if isinstance(event.get("args"), dict) else {}
            metadata = {"native_kind": kind, "tool": event.get("toolName", tool),
                        "tool_id": tool_id, "command": end_args.get("command", args.get("command"))}
            if event.get("isError"):
                result = event.get("result") if isinstance(event.get("result"), dict) else {}
                content = result.get("content")
                reason = " ".join(part["text"] for part in (content if isinstance(content, list) else [])
                                  if isinstance(part, dict) and part.get("type") == "text"
                                  and isinstance(part.get("text"), str))
                normalized_type = "task_warning"
                if re.search(r"\bhit the output token limit\b", reason):
                    metadata["native_kind"] = "tool_output_truncated"
                elif metadata["tool"] == "bash" and _benign_bash_probe(metadata["command"], reason):
                    normalized_type = "tool_probe"
                return [{"type": normalized_type, **metadata, "reason": reason}]
            return [{"type": "tool_completed", **metadata}]
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
        path = self._session_file(directory, identifier, state.get("actual_workspace") or state["workspace"])
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
