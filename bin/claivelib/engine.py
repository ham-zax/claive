"""Engine-neutral turn request contract."""
from dataclasses import dataclass, field
import json
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
        """Return the launch, adjusted for isolation claive must set up itself in job_dir."""
        return launch

    def session_usage(self, state):
        raise ValueError(f"worker engine {self.name or 'unknown'} does not provide session usage")
