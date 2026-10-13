"""Parent-facing reminders: delegation fit at launch, reasoning-heavy calls while a worker runs."""

# One model call above this many output tokens is mostly hidden reasoning, not visible work.
HEAVY_CALL_OUTPUT_TOKENS = 32000
# A single-turn brief this long usually bundles several tasks.
LARGE_BRIEF_BYTES = 4000


def launch_notes(state, prompt_bytes=0):
    notes = []
    if not (state.get("acceptance_contract") or {}).get("checks"):
        notes.append("Fit: no --checks-file. Delegate when an executable check exists and a wrong first "
                     "attempt is costly; small or tightly coupled work is faster inline.")
    if state.get("turn_timeout") is None:
        notes.append("Bound: no --turn-timeout; a stalled turn runs until cancelled.")
    if not state.get("reusable") and prompt_bytes >= LARGE_BRIEF_BYTES:
        notes.append(f"Scope: {prompt_bytes}-byte brief for a single-turn worker. Use open --detach + followup: "
                     "one small scoped task per turn, read its summary, then send the next.")
    return notes


def heavy_call_note(calls):
    heavy = [call["output_tokens"] for call in calls
             if isinstance(call.get("output_tokens"), int) and call["output_tokens"] >= HEAVY_CALL_OUTPUT_TOKENS]
    if not heavy:
        return None
    return (f"Reasoning-heavy: {len(heavy)} of {len(calls)} model calls produced >= {HEAVY_CALL_OUTPUT_TOKENS} "
            f"output tokens (max {max(heavy)}). Lower --reasoning-effort or narrow the task; "
            "cancel if no files or report have appeared.")
