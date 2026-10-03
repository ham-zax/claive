"""Muse execution engine."""
import os
from pathlib import Path

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
