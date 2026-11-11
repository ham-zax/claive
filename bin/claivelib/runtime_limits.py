"""Time-budget policy, independent of provider transport and wall-clock changes."""
import math


WRAP_UP = ("Turn time budget nearly reached: stop further edits and commands, "
           "verify git status, and emit your final claive-report now.")
TRUNCATION_HINT = ("Your tool call hit the output token limit. Do not resend the full file or "
                   "command. Split writes/edits into chunks of at most 150 lines, starting "
                   "with a compact skeleton if necessary.")


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


def turn_action(elapsed, inactive, active_tools, budget, stall, grace=0, wrapped=False):
    if budget is not None and elapsed >= budget + grace:
        return "timeout"
    if stall and inactive >= stall and not active_tools:
        return "stall_timeout"
    if budget is not None and not wrapped and elapsed >= budget * .85:
        return "wrap_up"
    return None
