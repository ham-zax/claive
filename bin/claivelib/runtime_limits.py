"""Time-budget policy, independent of provider transport and wall-clock changes."""
import math


WRAP_UP = ("Turn time budget nearly reached: stop further edits and commands, "
           "verify git status, and emit your final claive-report now.")
TRUNCATION_HINT = ("Your tool call hit the output token limit. Do not resend the full file or "
                   "command. Split writes/edits into chunks of at most 150 lines, starting "
                   "with a compact skeleton if necessary.")
TOOL_IDLE_WRAP_UP = ("[claive] Active tool command was killed after {seconds:g}s without output. "
                     "Do not re-run the hanging command without a bounded timeout; recover and "
                     "emit your final claive-report.")
TOOL_INTERRUPTED_NOTE = "[claive] Active tool command was interrupted by the parent.{extra}"
STREAM_FINISH_GRACE_SECONDS = 20.0
STREAM_ACTIVE_WINDOW_SECONDS = 15.0


def seconds(value, name, default=0):
    if value is None:
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a finite nonnegative number") from None
    if isinstance(value, bool) or not math.isfinite(number) or number < 0:
        raise ValueError(f"{name} must be a finite nonnegative number")
    return number


def turn_action(elapsed, inactive, active_tools, budget, stall, grace=0, wrapped=False,
                tool_idle=0, tool_inactive=0, streaming_final=False):
    effective_grace = grace
    if streaming_final and not active_tools and inactive < STREAM_ACTIVE_WINDOW_SECONDS:
        effective_grace = max(grace, STREAM_FINISH_GRACE_SECONDS)
    if budget is not None and elapsed >= budget + effective_grace:
        return "timeout"
    if tool_idle and active_tools and tool_inactive >= tool_idle:
        return "tool_idle_timeout"
    if stall and inactive >= stall and not active_tools:
        return "stall_timeout"
    if budget is not None and not wrapped and elapsed >= budget * .85:
        return "wrap_up"
    return None
