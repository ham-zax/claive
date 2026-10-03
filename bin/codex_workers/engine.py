"""Engine-neutral turn request contract."""
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class TurnRequest:
    binary: str
    workspace: str
    prompt_file: str
    session_id: Optional[str]
    provider: str
    model: Optional[str]
    reasoning_effort: str
    max_model_steps: int
    read_only: bool
    web: bool
    output_schema: Optional[str]
    session_logging: bool
    isolation: dict


class WorkerEngine:
    name = ""

    def validate_launch(self, launch):
        raise NotImplementedError

    def build_command(self, request):
        raise NotImplementedError

    def normalize_event(self, event):
        raise NotImplementedError

    def discover_workspace(self, command, stderr_text):
        return None
