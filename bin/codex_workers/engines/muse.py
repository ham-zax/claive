"""Muse execution engine."""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

from codex_workers.engine import WorkerEngine
from codex_workers.state import launch_config, session_id

MUSE_MODEL = "muse-spark-1.3-contributor"


class MuseEngine(WorkerEngine):
    name = "muse"
    default_model = MUSE_MODEL

    def resolve_launch(self, **options):
        provider = options.get("provider") or "meta"
        return {
            "binary": os.environ.get("MUSE_WORKER_BINARY", str(Path.home() / ".local/bin/muse")),
            "provider": provider,
            "model": None if provider == "echo" else options.get("model") or MUSE_MODEL,
            "read_only": bool(options.get("read_only")),
            "web": bool(options.get("web")),
            "output_schema": options.get("output_schema"),
            "session_logging": bool(options.get("session_logging", True)),
            "isolation": dict(options.get("isolation") or {}),
        }

    def validate_launch(self, launch):
        binary = Path(launch["binary"])
        if not binary.is_absolute() or not os.access(binary, os.X_OK):
            raise ValueError("Muse binary must be an absolute executable path")
        provider = launch.get("provider") or "meta"
        if provider not in {"meta", "echo"}:
            raise ValueError(f"unsupported Muse provider: {provider}")
        if provider != "echo" and launch.get("model") != MUSE_MODEL:
            raise ValueError(f"unsupported Muse model: {launch.get('model')}")
        isolation = launch.get("isolation") or {}
        mode = isolation.get("mode", "none")
        if mode not in {"none", "create", "existing"}:
            raise ValueError(f"unsupported isolation mode: {mode}")
        existing = isolation.get("existing_path")
        if mode == "existing":
            path = Path(existing or "")
            if not path.is_absolute() or not path.is_dir():
                raise ValueError("--worktree-existing must be an existing absolute directory")
        if isolation.get("base") and mode != "create":
            raise ValueError("--worktree-base requires --worktree")
        schema = launch.get("output_schema")
        if schema:
            path = Path(schema)
            if not path.is_absolute() or not path.is_file():
                raise ValueError("--output-schema must be an existing absolute file")

    @staticmethod
    def summarize_usage(export):
        calls = []
        for item in export.get("events", []):
            envelope = item.get("envelope", {})
            event = envelope.get("payload", {}).get("event", {})
            if event.get("kind") != "model_completed":
                continue
            usage = event.get("usage")
            if not isinstance(usage, dict):
                continue
            calls.append(dict(model=event.get("model"), **{
                key: value for key, value in usage.items()
                if key in {"input_tokens", "output_tokens", "cached_tokens", "reasoning_tokens",
                           "cache_read_tokens", "cache_write_tokens"}
                and isinstance(value, int) and not isinstance(value, bool) and value >= 0
            }))
        totals = {
            key: sum(call[key] for call in calls if key in call)
            for key in {"input_tokens", "output_tokens", "cached_tokens", "reasoning_tokens"}
            if any(key in call for call in calls)
        }
        complete = bool(calls) and all("input_tokens" in call and "cached_tokens" in call for call in calls)
        inputs = totals.get("input_tokens", 0)
        ratio = totals["cached_tokens"] / inputs if complete and inputs else None
        return dict(model_calls=len(calls), calls=calls, totals=totals, cache_hit_ratio=ratio)

    def session_usage(self, state):
        session = session_id(state)
        launch = launch_config(state)
        if not session or not launch.get("session_logging", True):
            raise ValueError("cache usage needs a retained Muse session log")
        binary = Path(launch["binary"])
        if not binary.is_absolute() or not os.access(binary, os.X_OK):
            raise ValueError("Muse engine is unavailable; stored job metadata and logs remain accessible")
        with tempfile.TemporaryDirectory(prefix="muse-usage-") as temporary:
            target = Path(temporary) / "session.json"
            result = subprocess.run(
                [str(binary), "export", "--session", session, "--out", str(target), "--redacted"],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode:
                raise ValueError("Muse usage export failed: " + "".join(
                    char for char in result.stderr if char.isprintable()
                ))
            return self.summarize_usage(json.loads(target.read_text()))

    def discover_workspace(self, command, stderr_text):
        if "-w" not in command:
            return None
        try:
            if command[command.index("-w") + 1] != "create":
                return None
        except (ValueError, IndexError):
            return None
        matches = re.findall(r"^muse: workspace root: (.+)$", stderr_text, re.M)
        actual = matches[-1].rsplit(" (", 1)[0] if matches else ""
        return actual if actual and Path(actual).is_dir() else None

    def normalize_event(self, event):
        payload = event.get("payload", {})
        kind = event.get("payload_type", "")
        if not isinstance(payload, dict) or not isinstance(kind, str):
            return []

        detail = payload.get("event", {})
        normalized = {"type": "activity", "native_kind": kind}
        if kind == "task.lifecycle.proposed" and isinstance(detail, dict):
            task_kind = str(detail.get("task_kind", ""))
            if task_kind.startswith("model."):
                normalized = {"type": "model_step", "native_kind": kind}
            elif "tool" in task_kind or "shell" in task_kind:
                normalized = {"type": "tool_started", "native_kind": kind, "tool": task_kind}
        elif kind == "run.output.delta":
            normalized = {"type": "output_delta", "native_kind": kind}
        elif kind == "task.lifecycle.failed":
            reason = detail.get("reason", "") if isinstance(detail, dict) else ""
            normalized = {"type": "task_warning", "native_kind": kind, "reason": reason}
        elif kind.startswith("run.terminal."):
            terminal = payload.get("terminal")
            normalized = {
                "type": "terminal_completed" if terminal == "completed" else "terminal_failed",
                "native_kind": kind,
                "terminal": terminal,
                "reason": payload.get("reason"),
                "text": payload.get("text", ""),
            }

        result = [normalized]
        reason = normalized.get("reason", "")
        quota = self._quota_event(reason, kind)
        if quota is not None:
            result.append(quota)
        return result

    @staticmethod
    def _quota_event(reason, native_kind):
        if not isinstance(reason, str) or "subscription quota exhausted" not in reason.lower():
            return None
        event = {"type": "quota_exhausted", "native_kind": native_kind}
        reset = re.search(
            r"resets at\s+(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2}))",
            reason,
        )
        if reset:
            event["reset_at"] = reset.group(1)
        return event

    def build_command(self, request):
        command = [
            request.binary, "exec", "--workspace", request.workspace, "--trust-workspace",
            "--disable-approval", "--json", "--provider", request.provider,
            "--max-model-steps", str(request.max_model_steps), "--user-input-auto-resolve",
            "--prompt-file", request.prompt_file,
        ]
        if request.session_id:
            command += ["--session-id", request.session_id]
        if request.provider != "echo":
            command += ["--model", request.model or MUSE_MODEL,
                        "--reasoning-effort", request.reasoning_effort]
        if request.read_only:
            command += ["--disable-write", "--disable-shell"]
        if not request.web:
            command += ["--disable-web-tools"]

        isolation = request.isolation or {}
        mode = isolation.get("mode", "none")
        if mode == "create":
            command += ["-w", "create"]
        elif mode == "existing":
            command += ["-w", "existing", "--worktree-existing", isolation["existing_path"]]
        if isolation.get("base"):
            command += ["--worktree-base", isolation["base"]]
        if request.output_schema:
            command += ["--output-schema", request.output_schema]
        if not request.session_logging:
            command += ["--no-session-log"]
        return command
