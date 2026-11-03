"""Interpret provider counters without claiming live context size or call counts."""

TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "thinking_tokens", "total_tokens")


def record_cumulative(state, event=None):
    """Record one attempt, preserving missing usage and cumulative baselines.

    Call without an event after a process ends to account for a missing result.
    A declining counter is a protocol error. Its old baseline remains intact.
    """
    records = state.setdefault("usage_turns", [])
    turn, attempt = state.get("turn", 1), state.get("attempt", 1)
    if any(row["turn"] == turn and row["attempt"] == attempt for row in records):
        return
    identifier = (event or {}).get("conversation_id") or state.get("session_id")
    row = dict(turn=turn, attempt=attempt, conversation_id=identifier,
               model=state.get("model"), granularity="attempt", complete=False, counters_complete=False)
    raw = (event or {}).get("usage")
    if raw is not None and not isinstance(raw, dict):
        raise ValueError("AGY usage must be an object or null")
    if isinstance(raw, dict):
        current = {key: raw[key] for key in TOKEN_FIELDS if key in raw}
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in current.values()):
            raise ValueError("AGY usage counters must be nonnegative integers")
        baseline = state.setdefault("usage_baseline", {}).get(identifier, {})
        if any(key != "cache_read_tokens" and key in baseline and value < baseline[key]
               for key, value in current.items()):
            raise ValueError("AGY cumulative usage decreased")
        # A gap makes the next delta span more than this attempt. Keep its bounds
        # unknown rather than assigning previous unreported consumption to this turn.
        previous = next((previous for previous in reversed(records)
                         if previous.get("conversation_id") == identifier), None)
        gap = previous is not None and not previous.get("counters_complete", previous["complete"])
        delta = {key: value - baseline.get(key, 0) for key, value in current.items()
                 if key != "cache_read_tokens"}
        if "cache_read_tokens" in current:
            # Native cache semantics are unverified. Preserve the observation,
            # without subtracting or adding it to normalized session totals.
            row["native_cache_read_tokens"] = current["cache_read_tokens"]
        row.update({key: value for key, value in delta.items() if key not in {"thinking_tokens"}})
        if "thinking_tokens" in delta:
            row["reasoning_tokens"] = delta["thinking_tokens"]
        row["counters_complete"] = all(key in current for key in TOKEN_FIELDS)
        row["complete"] = row["counters_complete"] and not gap
        if gap:
            row["spans_unreported_attempts"] = True
        state["usage_baseline"].setdefault(identifier, {}).update(current)
    records.append(row)
    state["turn_usage"] = row


def aggregate_usage(state):
    records = list(state.get("usage_turns", []))
    fields = ("input_tokens", "output_tokens", "reasoning_tokens", "total_tokens")
    totals = {key: sum(row[key] for row in records if key in row)
              for key in fields if any(key in row for row in records)}
    latest_index = max((row["turn"] for row in records), default=0)
    latest = [row for row in records if row["turn"] == latest_index]
    turn = dict(index=latest_index, complete=bool(latest) and all(row["complete"] for row in latest))
    turn.update({key: sum(row[key] for row in latest if key in row)
                 for key in fields if any(key in row for row in latest)})
    return dict(model_calls=None, calls=[], aggregates=records, totals=totals, turn=turn,
                usage_complete=bool(records) and all(row["complete"] for row in records),
                cache_hit_ratio=None, granularity="attempt")

def recent_usage(calls):
    calls = [call for call in calls if call.get("granularity", "call") == "call"]
    recent = calls[-5:]
    latest = recent[-1] if recent else {}
    inputs = latest.get("input_tokens")
    cached = latest.get("cached_tokens")
    ratio = cached / inputs if inputs and isinstance(cached, int) and 0 <= cached <= inputs else None
    previous = recent[-2].get("input_tokens") if len(recent) > 1 else None
    growth = inputs - previous if isinstance(inputs, int) and isinstance(previous, int) else None
    return dict(input_tokens=inputs if inputs is not None else "unknown",
                cache_percent=f"{ratio:.1%}" if ratio is not None else "unknown",
                growth_tokens=growth if growth is not None else "unknown",
                trend="unknown" if growth is None else "growing" if growth > 0 else "shrinking" if growth < 0 else "stable",
                recent_calls=recent)
