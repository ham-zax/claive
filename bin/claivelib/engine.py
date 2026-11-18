"""Engine-neutral turn request contract."""
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
from typing import Optional, Tuple
import uuid

__all__ = ["TurnRequest", "WorkerEngine", "SteeringCapability"]

SOFT_READ_NOTE = (
    "Read-only task. Read and investigate only. Do not edit, create, or delete files. "
    "Do not run commands that change files or external state. This is soft read mode, "
    "an instruction without enforced write prevention."
)


def entry_for_path(path):
    """Return acceptance snapshot entry [kind, ...] for path, or None when absent."""
    path = Path(path)
    if path.is_symlink():
        return ["symlink", os.readlink(path)]
    if path.is_file():
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return ["file", digest.hexdigest(), path.stat().st_mode & 0o777]
    return None


@dataclass(frozen=True)
class SteeringCapability:
    """Whether an engine can deliver genuine in-flight steering.

    In-flight steering means provider-confirmed input into the turn that is
    already running (mid-turn injection at a model-call boundary). It is not
    a queued follow-up: follow-ups wait for the current turn to settle and
    are handled by claivelib.turns. Engines without a delivery transport
    report supported=False with the reason why.
    """
    supported: bool
    mechanism: Optional[str] = None
    reason: str = ""
    notes: Tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class TurnRequest:
    binary: str
    workspace: str
    prompt_file: str
    session_id: Optional[str]
    provider: str
    model: Optional[str]
    reasoning_effort: Optional[str]
    max_model_steps: Optional[int]
    read_only: bool
    web: bool
    output_schema: Optional[str]
    session_logging: bool
    isolation: dict
    session_dir: Optional[str] = None
    steering: bool = False
    fork_session_id: Optional[str] = None
    fork_session_dir: Optional[str] = None
    budget: Optional[dict] = None
    # Models the user explicitly allowed for this worker (--allow-model), so a follow-up
    # keeps the same permission instead of being refused by the allowlist.
    allowed_models: Tuple[str, ...] = ()


class WorkerEngine:
    name = ""
    cwd_is_worktree = True
    read_only_enforced = False
    handles_read_only_prompt = False
    allows_startup_banner = False
    supports_fork = False
    default_reasoning_effort = None
    default_max_model_steps = None
    supports_max_model_steps = False

    def default_effort_for(self, model):
        """Effort for a turn that names none; an engine with per-model defaults overrides this."""
        return self.default_reasoning_effort

    def fallback_effort_for(self, model, current):
        return current

    def supported_models(self):
        """Models and the efforts each accepts, or None when the engine takes any model.

        The claive config "models" allowlist may only narrow this; it can never widen it.
        """
        return None

    def resolve_session_id(self, value, session_logging=True):
        return (value or str(uuid.uuid4())) if session_logging else None

    def steering_capability(self):
        """Describe genuine in-flight steering support for this engine.

        The default is unsupported: most supervised children run one prompt
        per process with stdin closed, so nothing can reach the running turn.
        Engines with a working delivery transport override this. Never report
        a queued follow-up as steering.
        """
        return SteeringCapability(
            supported=False,
            mechanism=None,
            reason=(f"the {self.name or 'unknown'} engine has no in-flight "
                    "steering delivery in this adapter"),
        )

    def initial_input(self, request):
        """Optional stdin protocol input for an opt-in duplex transport."""
        return None

    def steering_frame(self, identifier, message):
        """One stdin line that delivers a steering instruction over the duplex transport."""
        return json.dumps(dict(type="steer", id=identifier, message=message)) + "\n"

    def validate_turn(self, request):
        """Reject unsupported turn options before launch or queue acceptance."""

    def validate_prompt(self, text, read_only=False):
        """Optional provider prompt limit, checked before job creation and on follow-up."""

    def child_env(self):
        """Extra environment variables for the worker process."""
        return {}

    def child_env_for_turn(self, request):
        """Turn-specific process settings: the engine's own env plus engines.<name>.env."""
        from claivelib import config
        return dict(self.child_env(), **config.engine_env(self.name))

    def defaults(self):
        """The engine's declared defaults: every value config.json may override.

        Each engine extends this with its binary, provider and prompt limit, so
        `claive engine config` can report a value and its source for every key.
        """
        return {"default_model": getattr(self, "default_model", None),
                "env": {}, "prompt_limit": None}

    def resolve_launch(self, **options):
        raise NotImplementedError

    def validate_launch(self, launch):
        raise NotImplementedError

    def build_command(self, request):
        raise NotImplementedError

    def normalize_event(self, event):
        raise NotImplementedError

    def describe_event(self, event) -> list[dict]:
        """Describe a raw provider event without updating engine state."""
        return [{"kind": "event", "text": str(event.get("type", "unknown"))}]

    def dedupe_key(self, event):
        """Return a key for adjacent events that should count as one peek step."""
        return None

    def discover_workspace(self, command, stderr_text):
        return None

    def _carry_dirty_changes(self, workspace, target, job_dir, base_commit):
        """Mirror uncommitted changes (staged, unstaged, untracked, deletions) into target."""
        from claivelib import acceptance
        status_res = subprocess.run(
            ["git", "-C", workspace, "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            capture_output=True, timeout=60,
        )
        if status_res.returncode:
            detail = os.fsdecode(status_res.stderr).strip()[:300]
            raise ValueError(f"cannot read uncommitted changes in {workspace}: {detail}")
        entries = iter(status_res.stdout.split(b"\0"))
        paths = []
        for entry in entries:
            if not entry:
                continue
            code = os.fsdecode(entry[:2])
            if "U" in code or code in {"AA", "DD"}:
                raise ValueError("cannot --carry-dirty from a workspace with unresolved merge conflicts")
            rel = os.fsdecode(entry[3:])
            paths.append(rel)
            if "R" in code or "C" in code:
                paths.append(os.fsdecode(next(entries, b"")))
        unique_paths = [p for p in dict.fromkeys(paths) if p]
        ws_root = Path(workspace)
        wt_root = Path(target)
        for rel in unique_paths:
            parts = PurePosixPath(rel).parts
            if not parts or rel.startswith("/") or ".." in parts or ".git" in parts:
                raise ValueError(f"unsafe dirty path in workspace: {rel}")
            src = ws_root / rel
            dst = wt_root / rel
            if os.path.lexists(src):
                if src.is_symlink():
                    if dst.is_dir() and not dst.is_symlink():
                        shutil.rmtree(dst)
                    elif os.path.lexists(dst):
                        dst.unlink()
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.symlink_to(os.readlink(src))
                elif src.is_file():
                    if dst.is_dir() and not dst.is_symlink():
                        shutil.rmtree(dst)
                    elif dst.is_symlink():
                        dst.unlink()
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
            else:
                if dst.is_dir() and not dst.is_symlink():
                    shutil.rmtree(dst)
                elif os.path.lexists(dst):
                    dst.unlink()
                parent = dst.parent
                while parent != wt_root:
                    try:
                        parent.rmdir()
                    except OSError:
                        break
                    parent = parent.parent
        dirty_files = {rel: entry_for_path(wt_root / rel) for rel in unique_paths}
        acceptance.atomic(
            Path(job_dir) / "carried-baseline.json",
            {"base_commit": base_commit, "dirty_files": dirty_files},
        )
        return len(unique_paths)

    def prepare_isolation(self, launch, workspace, job_dir):
        """Create a managed worktree for the worker."""
        isolation = launch.get("isolation") or {}
        if isolation.get("mode") != "create":
            return launch
        carry_dirty = bool(isolation.get("carry_dirty"))
        target = Path(job_dir) / "worktree"
        base = isolation.get("base") or "HEAD"
        resolved = subprocess.run(["git", "-C", workspace, "rev-parse", "--verify", "--quiet", base + "^{commit}"],
                                  capture_output=True, text=True, timeout=60)
        if resolved.returncode:
            raise ValueError(f"cannot create the worktree: {base} is not a commit in a git repository at {workspace}")
        base_commit = resolved.stdout.strip()
        if carry_dirty:
            head_res = subprocess.run(["git", "-C", workspace, "rev-parse", "--verify", "--quiet", "HEAD^{commit}"],
                                      capture_output=True, text=True, timeout=60)
            if head_res.returncode or head_res.stdout.strip() != base_commit:
                raise ValueError("--carry-dirty requires --worktree at HEAD")
        branch = None if launch.get("read_only") else f"claive/{Path(job_dir).name}"
        command = ["git", "-C", workspace, "worktree", "add"]
        command += ["--detach", str(target)] if branch is None else ["-b", branch, str(target)]
        result = subprocess.run(command + [base_commit], capture_output=True, text=True, timeout=120)
        if result.returncode:
            detail = " ".join(result.stderr.split())[:300]
            raise ValueError(f"cannot create the worktree at {base}: {detail}")
        new_isolation = dict(mode="existing", base=None, existing_path=str(target), owner="claive",
                             source=workspace, branch=branch, base_commit=base_commit)
        from claivelib import worktrees
        try:
            if carry_dirty:
                carried_count = self._carry_dirty_changes(workspace, target, job_dir, base_commit)
                new_isolation["carried_dirty"] = carried_count
            worktrees.record_isolation("created", Path(job_dir).name, new_isolation)
        except BaseException:
            subprocess.run(["git", "-C", workspace, "worktree", "remove", "--force", str(target)],
                           capture_output=True, timeout=120)
            if branch:
                subprocess.run(["git", "-C", workspace, "branch", "-D", branch], capture_output=True, timeout=120)
            raise
        return dict(launch, isolation=new_isolation)

    def validate_isolation(self, launch):
        isolation = launch.get("isolation") or {}
        mode = isolation.get("mode", "none")
        if mode not in {"none", "create", "existing"}:
            raise ValueError(f"unsupported isolation mode: {mode}")
        if mode == "existing":
            path = Path(isolation.get("existing_path") or "")
            if not path.is_absolute() or not path.is_dir():
                raise ValueError("--worktree-existing must be an existing absolute directory")
        if isolation.get("base") and mode != "create":
            raise ValueError("--worktree-base requires --worktree")
        if isolation.get("carry_dirty") and mode != "create" and isolation.get("owner") != "claive":
            raise ValueError("--carry-dirty requires --worktree")

    def session_usage(self, state):
        raise ValueError(f"worker engine {self.name or 'unknown'} does not provide session usage")
