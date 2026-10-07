"""Engine-neutral turn request contract."""
from dataclasses import dataclass
from typing import Optional
import uuid


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


class WorkerEngine:
    name = ""
    default_reasoning_effort = "high"
    default_max_model_steps = None

    def resolve_session_id(self, value, session_logging=True):
        return (value or str(uuid.uuid4())) if session_logging else None

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

    def discover_workspace(self, command, stderr_text):
        return None

    def prepare_isolation(self, launch, workspace, job_dir):
        """Return the launch, adjusted for isolation claive must set up itself in job_dir."""
        return launch

    def session_usage(self, state):
        raise ValueError(f"worker engine {self.name or 'unknown'} does not provide session usage")
