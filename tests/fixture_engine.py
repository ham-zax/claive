"""Test-only worker engine used to prove the generic lifecycle boundary."""
import os
from pathlib import Path

from claivelib.engine import WorkerEngine


class FixtureEngine(WorkerEngine):
    name = "fixture"
    default_max_model_steps = 100

    def resolve_launch(self, **options):
        return {
            "binary": os.environ["FIXTURE_WORKER_BINARY"],
            "provider": options.get("provider") or "fixture",
            "model": options.get("model"),
            "read_only": bool(options.get("read_only")),
            "web": bool(options.get("web")),
            "output_schema": options.get("output_schema"),
            "session_logging": bool(options.get("session_logging", True)),
            "isolation": dict(options.get("isolation") or {}),
        }

    def validate_launch(self, launch):
        binary = Path(launch["binary"])
        if not binary.is_absolute() or not os.access(binary, os.X_OK):
            raise ValueError("fixture binary must be an absolute executable path")

    def build_command(self, request):
        return [request.binary, request.prompt_file, *(["--model", request.model] if request.model else [])]

    def normalize_event(self, event):
        kind = event.get("kind")
        if kind == "model":
            return [{"type": "model_step", "native_kind": kind}]
        if kind == "tool":
            return [{"type": "tool_started", "native_kind": kind, "tool": event.get("tool", "fixture")}]
        if kind == "output":
            return [{"type": "output_delta", "native_kind": kind}]
        if kind == "warning":
            return [{"type": "task_warning", "native_kind": kind, "reason": event.get("reason", "")}]
        if kind == "terminal":
            status = event.get("status")
            return [{
                "type": "terminal_completed" if status == "completed" else "terminal_failed",
                "native_kind": kind,
                "terminal": status,
                "reason": event.get("reason"),
                "text": event.get("text", ""),
            }]
        return [{"type": "activity", "native_kind": str(kind or "fixture")}]
