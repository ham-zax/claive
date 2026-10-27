"""Engine-neutral turn request contract."""
from dataclasses import dataclass, field
import json
from pathlib import Path
import subprocess
from typing import Optional, Tuple
import uuid

__all__ = ["TurnRequest", "WorkerEngine", "SteeringCapability"]


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
    reasoning_effort: str
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


class WorkerEngine:
    name = ""
    supports_fork = False
    default_reasoning_effort = "high"
    default_max_model_steps = None
    supports_max_model_steps = False

    def default_effort_for(self, model):
        """Effort for a turn that names none; an engine with per-model defaults overrides this."""
        return self.default_reasoning_effort

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

    def child_env(self):
        """Extra environment variables for the worker process."""
        return {}

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

    def prepare_isolation(self, launch, workspace, job_dir):
        """Create a managed worktree for the worker."""
        isolation = launch.get("isolation") or {}
        if isolation.get("mode") != "create":
            return launch
        target = Path(job_dir) / "worktree"
        base = isolation.get("base") or "HEAD"
        resolved = subprocess.run(["git", "-C", workspace, "rev-parse", "--verify", "--quiet", base + "^{commit}"],
                                  capture_output=True, text=True, timeout=60)
        if resolved.returncode:
            raise ValueError(f"cannot create the worktree: {base} is not a commit in a git repository at {workspace}")
        base_commit = resolved.stdout.strip()
        branch = None if launch.get("read_only") else f"claive/{Path(job_dir).name}"
        command = ["git", "-C", workspace, "worktree", "add"]
        command += ["--detach", str(target)] if branch is None else ["-b", branch, str(target)]
        result = subprocess.run(command + [base_commit], capture_output=True, text=True, timeout=120)
        if result.returncode:
            detail = " ".join(result.stderr.split())[:300]
            raise ValueError(f"cannot create the worktree at {base}: {detail}")
        isolation = dict(mode="existing", base=None, existing_path=str(target), owner="claive",
                         source=workspace, branch=branch, base_commit=base_commit)
        from claivelib import worktrees
        try:
            worktrees.record_isolation("created", Path(job_dir).name, isolation)
        except BaseException:
            subprocess.run(["git", "-C", workspace, "worktree", "remove", "--force", str(target)],
                           capture_output=True, timeout=120)
            if branch:
                subprocess.run(["git", "-C", workspace, "branch", "-D", branch], capture_output=True, timeout=120)
            raise
        return dict(launch, isolation=isolation)

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

    def session_usage(self, state):
        raise ValueError(f"worker engine {self.name or 'unknown'} does not provide session usage")
