"""Test-only worker engine used to prove the generic lifecycle boundary."""
import os
from pathlib import Path

from codex_workers.engine import WorkerEngine


class FixtureEngine(WorkerEngine):
    name = "fixture"

    def validate_launch(self, launch):
        binary = Path(launch["binary"])
        if not binary.is_absolute() or not os.access(binary, os.X_OK):
            raise ValueError("fixture binary must be an absolute executable path")

    def build_command(self, request):
        return [request.binary]

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
