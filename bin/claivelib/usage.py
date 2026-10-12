"""Interpret per-call provider counters without claiming live context size."""

def recent_usage(calls):
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
