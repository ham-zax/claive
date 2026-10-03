"""Muse execution engine."""
import os
from pathlib import Path
import re

from codex_workers.engine import WorkerEngine

MUSE_MODEL = "muse-spark-1.3-contributor"


class MuseEngine(WorkerEngine):
    name = "muse"

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
