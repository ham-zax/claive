"""Deterministic arbiter for verifier-gated refinement runs (claive-orch). No dependencies.

Models propose; this code decides. A frontier parent (Claude Code or Codex) launches
workers through claive and reports their results here. This module owns the run
history, verification, checkpoints, budgets, and the single legal next action.
See docs/experiment/06-skill-driven-implementation.md.
"""

import argparse
import datetime
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import random
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid

from claivelib import cli as workers
from claivelib import memcap
from claivelib.engines import DEFAULT_ENGINE, default_engine, get_engine

ARMS = {
    "A": "single worker, verify once",
    "R": "two independent same-configuration runs, verifier picks",
    "R'": "two runs with different configurations, verifier picks",
    "B": "refinement: critique -> correct -> verify, rules control",
    "B0": "refinement without the critic (verifier output only)",
    "D": "full ladder: refinement, then one diverse second candidate after a stall",
}
REFINING = {"B", "B0", "D"}
TWO_LANES = {"R", "R'"}
MAX_ROUNDS = 3
MAX_LANES = 2
BREADTH_ROUNDS = 2
STALL_AFTER = 2
DIFF_LIMIT = 60000
OUTPUT_LIMIT = 8000
NO_SIGN = ("-c", "commit.gpgsign=false")  # checkpoint commits are internal; never prompt for a key
GIT_IDENTITY = {"GIT_AUTHOR_NAME": "claive-orch", "GIT_AUTHOR_EMAIL": "claive-orch@localhost",
                "GIT_COMMITTER_NAME": "claive-orch", "GIT_COMMITTER_EMAIL": "claive-orch@localhost"}
CATEGORIES = ("change", "bug-fix", "feature", "debugging", "refactor", "docs", "other")
CRITIC_ROSTER = ("mimo-v2.6-flash-free", "big-pickle", "space-bunny-free")
CRITIC_RESERVE = ("longcat-2.5-preview-free",)
VERSION_TOKENS = {"free", "preview", "flash", "lightning", "ultra", "contributor", "pro", "mini"}


# ---------------------------------------------------------------- pure helpers

def model_family(model, engine=None):
    """Family of a model ID: leading name tokens before the first version-like token.

    muse-spark-1.3-contributor -> muse-spark, nemotron-3-ultra-free -> nemotron,
    mimo-v2.6-flash-free -> mimo, big-pickle -> big-pickle.
    """
    if not model:
        return engine or "unknown"
    name = model.split("/")[-1].lower()
    tokens = []
    for token in name.split("-"):
        if any(char.isdigit() for char in token) or token in VERSION_TOKENS:
            break
        tokens.append(token)
    return "-".join(tokens) or name


SCORE_PATTERNS = [
    # cargo: "test result: FAILED. 3 passed; 1 failed;"
    ("cargo", re.compile(r"(\d+) passed; (\d+) failed")),
    # jest: "Tests:       1 failed, 4 passed, 5 total"
    ("jest", re.compile(r"Tests:\s+(?:(\d+) failed, )?(?:\d+ skipped, )?(?:(\d+) passed, )?(\d+) total")),
    # vitest: "Tests  1 failed | 4 passed (5)"
    ("vitest", re.compile(r"Tests\s+(?:(\d+) failed \| )?(\d+) passed[^\n(]*\((\d+)\)")),
    # node:test / TAP: "# pass 4" ... "# fail 1"
    ("tap", re.compile(r"^# pass (\d+)\s*$.*?^# fail (\d+)\s*$", re.M | re.S)),
    # unittest: "Ran 5 tests" + optional "FAILED (failures=1, errors=1)"
    ("unittest", re.compile(r"^Ran (\d+) tests?", re.M)),
    # pytest summary: "=== 1 failed, 4 passed, 1 error, 3 subtests passed in 0.1s ===" (no rules with -q)
    ("pytest", re.compile(r"^(?:=+ )?(\d+ \w+(?:, \d+ \w+(?: \w+)?)*) in [\d.]+s", re.M)),
]


def parse_score(output, custom=None):
    """Return (passed, total) from test-runner output, or None when unrecognised."""
    if custom:
        match = None
        try:
            for match in re.finditer(custom, output, re.M):
                pass
            if match:
                groups = match.groupdict()
                passed = int(groups["passed"]) if groups.get("passed") else 0
                if groups.get("total"):
                    return passed, int(groups["total"])
                return passed, passed + int(groups.get("failed") or 0)
        except (re.error, ValueError):
            pass  # validated at init; a run recorded before that scores as unrecognised
        return None
    for kind, pattern in SCORE_PATTERNS:
        matches = list(pattern.finditer(output))
        if not matches:
            continue
        if kind == "cargo":
            passed = sum(int(m.group(1)) for m in matches)
            failed = sum(int(m.group(2)) for m in matches)
            return passed, passed + failed
        last = matches[-1]
        if kind == "jest":
            failed, passed, total = (int(value or 0) for value in last.groups())
            return passed, total
        if kind == "vitest":
            failed, passed, total = (int(value or 0) for value in last.groups())
            return passed, total
        if kind == "tap":
            passed, failed = int(last.group(1)), int(last.group(2))
            return passed, passed + failed
        if kind == "unittest":
            total = int(last.group(1))
            bad = re.search(r"^FAILED \(([^)]*)\)", output[last.end():], re.M)
            failed = sum(int(n) for n in re.findall(r"(?:failures|errors)=(\d+)", bad.group(1))) if bad else 0
            return max(total - failed, 0), total
        if kind == "pytest":
            counts = dict((word, int(n)) for n, word in re.findall(r"(\d+) (passed|failed|errors?)", last.group(1)))
            passed = counts.get("passed", 0)
            failed = counts.get("failed", 0) + counts.get("error", 0) + counts.get("errors", 0)
            return passed, passed + failed
    return None


def verification_key(result):
    """Total order for verification results: passing first, then fraction passed."""
    if result is None:
        return (False, -1.0)
    return (bool(result["passed"]), float(result["score"]))


def extract_json(text):
    """Last fenced ```json block, else the last balanced top-level object in the text."""
    blocks = re.findall(r"```(?:json)?\s*\n(.*?)\n```", text, re.S)
    for block in reversed(blocks):
        try:
            return json.loads(block)
        except ValueError:
            continue
    end = text.rfind("}")
    while end != -1:
        depth = 0
        for start in range(end, -1, -1):
            depth += {"}": 1, "{": -1}.get(text[start], 0)
            if depth == 0:
                try:
                    return json.loads(text[start:end + 1])
                except ValueError:
                    break
        end = text.rfind("}", 0, end)
    raise ValueError("no JSON object found in answer")


def validate_critique(raw):
    """Normalise a critic answer. Invalid output becomes noConcreteDefect plus an error."""
    if not isinstance(raw, dict):
        return {"no_concrete_defect": True, "approach_sound": True, "defects": [],
                "error": "critique is not a JSON object"}
    defects, dropped = [], 0
    for item in raw.get("defects") or []:
        if not isinstance(item, dict):
            dropped += 1
            continue
        location = str(item.get("location") or "").strip()
        description = str(item.get("description") or item.get("problem") or "").strip()
        evidence = item.get("evidence") if item.get("evidence") in {"verifier", "diff", "both"} else None
        if not location or not description or evidence is None:
            dropped += 1
            continue
        defects.append({"location": location[:300], "description": description[:2000],
                        "evidence": evidence, "localized": item.get("localized", True) is not False})
    claimed = raw.get("no_concrete_defect", raw.get("noConcreteDefect"))
    result = {"no_concrete_defect": bool(claimed) or not defects,
              "approach_sound": raw.get("approach_sound", raw.get("approachSound", True)) is not False,
              "defects": defects if not claimed else [], "dropped_defects": dropped}
    if not claimed and not defects:
        result["error"] = "critic reported defects but none had location, description and evidence"
    return result


def fold(events):
    """Rebuild run state from the append-only event history."""
    state = {"config": None, "base": None, "lanes": {}, "lane_order": [], "review": None,
             "finished": None, "integrated": None, "usage": {}, "warnings": [], "started_at": None}
    for event in events:
        kind, data = event["type"], event.get("data", {})
        lane = state["lanes"].get(data.get("lane"))
        if kind == "run.started":
            state["config"] = data["config"]
            state["started_at"] = event["time"]
        elif kind == "base.verified":
            state["base"] = data["result"]
        elif kind == "lane.added":
            state["lanes"][data["lane"]] = dict(data, workers=[], verifications=[], critiques=[],
                                                best=None, best_commit=data["base_commit"],
                                                no_progress=0, reverted_last=False)
            state["lane_order"].append(data["lane"])
        elif kind == "worker.registered" and lane is not None:
            lane["workers"].append(data)
        elif kind == "verification.completed" and lane is not None:
            lane["verifications"].append(data["result"])
        elif kind == "checkpoint.accepted" and lane is not None:
            improved = data["improved"]
            lane["best"], lane["best_commit"] = data["result"], data["commit"]
            lane["no_progress"] = 0 if improved else lane["no_progress"] + 1
            lane["reverted_last"] = False
        elif kind == "checkpoint.reverted" and lane is not None:
            lane["no_progress"] += 1
            lane["reverted_last"] = True
        elif kind == "critique.completed" and lane is not None:
            entry = dict(data)
            entry["rejected"] = list(data.get("rejected") or [])
            entry["_original_defects"] = [dict(d) for d in data.get("defects") or []]
            lane["critiques"].append(entry)
        elif kind == "verification.rescored" and lane is not None:
            if lane["verifications"]:
                lane["verifications"][-1] = data["result"]
                lane["best"] = data["result"]
                rounds = max(len(lane["verifications"]) - 1, 0)
                lane["critiques"] = [c for c in lane["critiques"] if c["round"] <= rounds]
        elif kind == "critique.rejected" and lane is not None:
            matches = [c for c in lane["critiques"] if c.get("round") == data.get("round")]
            if matches:
                crit = matches[-1]
                orig = crit.get("_original_defects")
                if orig is None:
                    orig = list(crit.get("defects") or [])
                    crit["_original_defects"] = [dict(d) for d in orig]
                if crit.get("rejected") is None:
                    crit["rejected"] = []
                seen = {r.get("index") for r in crit["rejected"]}
                for number in data.get("defects") or []:
                    try:
                        index = int(number)
                    except (TypeError, ValueError):
                        continue
                    if index in seen:
                        continue
                    if index < 1 or index > len(orig):
                        continue
                    description = orig[index - 1].get("description", "") if isinstance(orig[index - 1], dict) else ""
                    crit["rejected"].append({"index": index, "description": description,
                                            "reason": data.get("reason", "")})
                    seen.add(index)
                crit["defects"] = [dict(d) for i, d in enumerate(orig, 1) if i not in seen]
                if not crit["defects"]:
                    crit["no_concrete_defect"] = True
        elif kind == "review.completed":
            state["review"] = data
        elif kind == "usage.collected":
            state["usage"] = data["workers"]
        elif kind == "warning":
            state["warnings"].append(data["message"])
        elif kind == "run.finished":
            state["finished"] = data
        elif kind == "run.integrated":
            state["integrated"] = data
    return state


def lane_rounds(lane):
    """Refinement rounds completed: verifications after the first one."""
    return max(len(lane["verifications"]) - 1, 0)


def lane_status(config, lane, limit):
    """('running', None) or ('passed'|'stalled', reason) for one lane under a refining arm."""
    verifications = lane["verifications"]
    if not verifications:
        return "running", None
    if verifications[-1]["passed"] or (lane["best"] and lane["best"]["passed"]):
        return "passed", "verifier passed"
    if config["arm"] not in REFINING:
        return "stalled", "arm does not refine"
    rounds = lane_rounds(lane)
    if rounds >= limit:
        return "stalled", f"round limit {limit} reached"
    if lane["no_progress"] >= STALL_AFTER:
        return "stalled", f"no improvement in {STALL_AFTER} consecutive rounds"
    pending = [c for c in lane["critiques"] if c["round"] == rounds + 1]
    if pending:
        critique = pending[-1]
        if critique["no_concrete_defect"]:
            return "stalled", "critic found no concrete defect"
        unlikely = not critique["approach_sound"] or not all(d["localized"] for d in critique["defects"])
        # Repair-unlikely escalates lane a to breadth under arm D; elsewhere the worker still corrects.
        if unlikely and config["arm"] == "D" and lane["lane"] == "a":
            return "stalled", "repair unlikely: critic flagged a non-localized defect or unsound approach"
    return "running", None


def select(state):
    """Choose among lanes: verifier first; ties go to the reviewer (arm D) or the smaller diff."""
    lanes = [state["lanes"][name] for name in state["lane_order"] if state["lanes"][name]["best"]]
    if not lanes:
        return None, "no lane produced a verified checkpoint", False
    lanes.sort(key=lambda lane: verification_key(lane["best"]), reverse=True)
    top = [lane for lane in lanes if verification_key(lane["best"]) == verification_key(lanes[0]["best"])]
    if len(top) == 1:
        return top[0], "highest verification", False
    review = state.get("review")
    if review and review.get("prefer") in {lane["lane"] for lane in top}:
        chosen = next(lane for lane in top if lane["lane"] == review["prefer"])
        return chosen, "verification tie broken by reviewer", True
    top.sort(key=lambda lane: (lane.get("diff_lines", 0), lane["lane"]))
    return top[0], "verification tie broken by smaller diff", True


def action(name, reason, lane=None, **extra):
    return dict({"action": name, "lane": lane, "reason": reason}, **extra)


def decide(state, now=None):
    """Pure arbiter: (run state) -> exactly one legal next action with a reason."""
    config = state["config"]
    if state["finished"]:
        return action("done", "run already finished", outcome=state["finished"]["outcome"])
    arm, rounds = config["arm"], config["rounds"]
    if config.get("max_minutes") and now is not None and state["started_at"] is not None:
        if now - state["started_at"] > config["max_minutes"] * 60:
            return finish(state, "budget", "wall-clock budget exhausted")
    order = state["lane_order"]
    if not order:
        return action("add_lane", "every run starts with lane a (L0 direct)", lane="a")
    for name in order:
        lane = state["lanes"][name]
        if not lane["verifications"]:
            if not [w for w in lane["workers"] if w["role"] == "implementer"]:
                return action("implement", "lane has no implementer yet", lane=name)
            return action("verify", "implementer registered; verify after its turn completes", lane=name)
    if arm in TWO_LANES and len(order) < 2:
        return action("add_lane", f"arm {arm} runs two independent candidates", lane="b",
                      diversity="same" if arm == "R" else "different")
    if arm == "A" or arm in TWO_LANES:
        return finish(state, None, f"arm {arm}: all candidates verified")
    for name in order:
        lane = state["lanes"][name]
        limit = rounds if name == "a" else min(rounds, BREADTH_ROUNDS)
        status, why = lane_status(config, lane, limit)
        if status == "passed":
            if config.get("post_pass_critic"):
                post = [c for c in lane["critiques"] if c.get("post_pass")]
                if not post:
                    return action("critique", f"lane {name}: {why}; post-pass review for regressions beyond the task",
                                  lane=name, round=lane_rounds(lane) + 1, post_pass=True)
                last = post[-1]
                if (not last.get("no_concrete_defect") and lane_rounds(lane) < last["round"]
                        and lane_rounds(lane) < limit):
                    return action("correct", f"lane {name}: post-pass critique named defects; correct them",
                                  lane=name, round=last["round"], post_pass=True)
            return finish(state, None, f"lane {name}: {why}")
        if status == "running":
            next_round = lane_rounds(lane) + 1
            if arm == "B0":
                return action("correct", "critic-less refinement: correct from verifier output",
                              lane=name, round=next_round)
            if not [c for c in lane["critiques"] if c["round"] == next_round]:
                return action("critique", "verification failed; ask a cross-family critic for defects",
                              lane=name, round=next_round)
            return action("correct", "critic named concrete defects; same session corrects them",
                          lane=name, round=next_round)
        if arm == "D" and name == "a" and len(order) == 1:
            return action("add_lane", f"lane a stalled ({why}); escalate to one diverse candidate (L2)",
                          lane="b", diversity="different")
    chosen, why, tie = select(state)
    if tie and arm == "D" and state["review"] is None and len(order) == 2:
        return action("review", "candidates tie on verification; one reviewer comparison")
    return finish(state, None, why)


def finish(state, forced, reason):
    chosen, why, tie = select(state)
    if chosen is None:
        outcome = forced or "failed"
        return action("finish", f"{reason}; {why}", outcome=outcome)
    if chosen["best"]["passed"]:
        outcome = "verified"  # a passing checkpoint stays verified even when the budget ran out
    elif forced:
        outcome = forced
    elif state["config"]["arm"] == "D" and len(state["lane_order"]) == 2:
        outcome = "unresolved"
    else:
        outcome = "stalled"
    return action("finish", f"{reason}; {why}", lane=chosen["lane"], outcome=outcome,
                  commit=chosen["best_commit"], branch=chosen["branch"], tie=tie,
                  score=chosen["best"])


def wilson(successes, n, z=1.96):
    if not n:
        return (0.0, 0.0)
    p = successes / n
    centre = p + z * z / (2 * n)
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    denominator = 1 + z * z / n
    return (max(0.0, (centre - spread) / denominator), min(1.0, (centre + spread) / denominator))


def critic_outcomes(events):
    """Outcome of each critique from the next checkpoint of the same lane."""
    out = []
    for index, event in enumerate(events):
        if event.get("type") != "critique.completed":
            continue
        data = event.get("data", {})
        if data.get("post_pass"):
            outcome = "post_pass"
        elif data.get("error"):
            outcome = "error"
        elif data.get("no_concrete_defect"):
            outcome = "no_defect"
        else:
            outcome = "unused"
            for later in events[index + 1:]:
                if later.get("type") not in ("checkpoint.accepted", "checkpoint.reverted"):
                    continue
                if later.get("data", {}).get("lane") != data.get("lane"):
                    continue
                if later["type"] == "checkpoint.reverted":
                    outcome = "reverted"
                elif later["data"].get("improved"):
                    outcome = "improved"
                else:
                    outcome = "no_change"
                break
        out.append({"lane": data.get("lane"), "round": data.get("round"), "model": data.get("model"),
                    "family": data.get("family"), "outcome": outcome})
    return out


def collect_stats(histories):
    """Aggregate critic, implementer and category stats over run histories."""
    critics, implementers, categories = {}, {}, {}
    for history in histories:
        state = fold(history)
        for entry in critic_outcomes(history):
            model = entry.get("model") or "unknown"
            row = critics.setdefault(model, {"family": entry.get("family") or model_family(model, "pi"),
                                             "critiques": 0, "improved": 0, "no_change": 0,
                                             "reverted": 0, "no_defect": 0, "error": 0,
                                             "unused": 0, "post_pass": 0})
            row["critiques"] += 1
            if entry["outcome"] in row:
                row[entry["outcome"]] += 1
        for name in state.get("lane_order", []):
            lane = state["lanes"][name]
            key = f"{lane.get('engine')}/{lane.get('model')}"
            row = implementers.setdefault(key, {"lanes": 0, "l0_passed": 0, "passed": 0})
            row["lanes"] += 1
            verifs = lane.get("verifications") or []
            if verifs and verifs[0].get("passed"):
                row["l0_passed"] += 1
            if any(v.get("passed") for v in verifs):
                row["passed"] += 1
        finished = state.get("finished")
        if finished and finished.get("outcome") != "invalid":
            config = state.get("config") or {}
            cat = config.get("category") or "uncategorized"
            row = categories.setdefault(cat, {"runs": 0, "verified": 0, "l0_passed": 0, "recovered": 0})
            row["runs"] += 1
            if finished.get("outcome") == "verified":
                row["verified"] += 1
            first = (state["lanes"].get("a", {}).get("verifications") or [])
            l0 = bool(first and first[0].get("passed"))
            if l0:
                row["l0_passed"] += 1
            if finished.get("outcome") == "verified" and not l0:
                row["recovered"] += 1
    for row in critics.values():
        scored = row["improved"] + row["no_change"] + row["reverted"] + row["no_defect"]
        row["helpful_rate"] = (row["improved"] / scored) if scored else None
    return {"critics": critics, "implementers": implementers, "categories": categories}


def pick_critic(stats_critics, lane_family, seed, roster=CRITIC_ROSTER, reserve=CRITIC_RESERVE,
                min_uses=3, explore=0.2):
    """Choose a cross-family critic: cold-start, then epsilon-greedy on smoothed rates."""
    def pool_candidates(pool):
        return [m for m in pool if model_family(m, "pi") != lane_family and not workers.disallowed_model(m)]
    candidates = pool_candidates(roster)
    order = list(roster)
    if not candidates:
        candidates = pool_candidates(reserve)
        order = list(reserve)
    if not candidates:
        raise ValueError("no critic candidate available")
    def scored(m):
        row = stats_critics.get(m, {})
        return (row.get("improved", 0) + row.get("no_change", 0) + row.get("reverted", 0)
                + row.get("no_defect", 0))
    scores = {}
    for model in candidates:
        row = stats_critics.get(model, {})
        scores[model] = (row.get("improved", 0) + 1) / (scored(model) + 2)
    positions = {model: index for index, model in enumerate(order)}
    ranked = sorted(candidates, key=lambda m: (-scores[m], positions.get(m, 0)))
    cold = next((m for m in candidates if scored(m) < min_uses), None)
    if cold is not None:
        return {"model": cold, "mode": "cold-start",
                "fallbacks": [m for m in ranked if m != cold], "scores": scores}
    rng = random.Random(seed)
    draw = rng.random()
    if draw < explore and len(ranked) >= 2:
        chosen = rng.choice(ranked[1:])
        mode = "explore"
    else:
        chosen = ranked[0]
        mode = "exploit"
    return {"model": chosen, "mode": mode,
            "fallbacks": [m for m in ranked if m != chosen], "scores": scores}


def implementer_choice(muse_available, pi_available, muse_quota_reset=None, configured_engine=None):
    """Pick the implementer engine: Muse when possible, else Pi muse free."""
    if configured_engine == "pi" and pi_available:
        return {"engine": "pi", "model": "muse-spark-1.3-contributor-free", "reasoning_effort": "max",
                "provider": "opencode2api", "fallback": False, "reason": "configured engine is pi"}
    if muse_available and not muse_quota_reset:
        return {"engine": "muse", "model": "muse-spark-1.3-contributor", "reasoning_effort": "xhigh",
                "provider": None, "fallback": False, "reason": "muse available"}
    if pi_available:
        if muse_quota_reset:
            reason = f"muse quota exhausted until {muse_quota_reset}"
        else:
            reason = "muse binary not found"
        return {"engine": "pi", "model": "muse-spark-1.3-contributor-free", "reasoning_effort": "max",
                "provider": "opencode2api", "fallback": True, "reason": reason}
    raise ValueError("no implementer available: neither muse nor pi is available")


def _engine_available(engine):
    """True when the engine binary is executable; any error means unavailable."""
    try:
        if engine == "muse":
            from claivelib.engines import muse as module
        elif engine == "pi":
            from claivelib.engines import pi as module
        else:
            return False
        return os.access(module.default_binary(), os.X_OK)
    except Exception:
        return False


def _parse_reset(value):
    """Epoch seconds for an ISO reset time (Z allowed), or None when unparsable."""
    try:
        parsed = datetime.datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.timestamp()


def muse_quota_block(now=None):
    """Latest active Muse quota reset string, or None when Muse is usable.

    Workers record quota_exhausted and quota_reset_at; records without an engine predate
    multi-engine support and are Muse. Without a reset time the 5-hour window counts from ended_at.
    """
    now = time.time() if now is None else now
    try:
        children = list(workers.root().iterdir())
    except OSError:
        return None
    reset_found, hit_found = None, None
    for child in children:
        if not re.fullmatch(r"[0-9a-f]{12}", child.name):
            continue
        try:
            record = json.loads((child / "state.json").read_text())
        except (OSError, ValueError):
            continue
        if (record.get("engine") or DEFAULT_ENGINE) != "muse" or not record.get("quota_exhausted"):
            continue
        reset = record.get("quota_reset_at")
        if reset:
            moment = _parse_reset(reset)
            if moment is not None and moment > now and (reset_found is None or moment > reset_found[0]):
                reset_found = (moment, str(reset))
            continue
        ended = record.get("ended_at")
        if isinstance(ended, (int, float)) and 0 <= now - ended <= 5 * 3600:
            if hit_found is None or ended > hit_found:
                hit_found = ended
    if reset_found:
        return reset_found[1]
    if hit_found is not None:
        iso = datetime.datetime.fromtimestamp(hit_found, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return f"unknown (quota hit at {iso})"
    return None


def _configured_engine():
    """Configured default engine, or None when unset or unreadable."""
    try:
        from claivelib import config as cfg
        return cfg.default_engine(None)
    except Exception:
        return None


def breadth_choice(stats_implementers, lane_a_family, critic_families, roster=CRITIC_ROSTER,
                   reserve=CRITIC_RESERVE):
    """Pick a diverse breadth candidate that avoids lane a and its critics."""
    critics = set(critic_families or ())
    def filtered(pool, strict):
        out = []
        for model in pool:
            family = model_family(model, "pi")
            if family == lane_a_family or workers.disallowed_model(model):
                continue
            if strict and family in critics:
                continue
            out.append(model)
        return out
    candidates = filtered(roster, True) or filtered(roster, False)
    order = list(roster)
    if not candidates:
        candidates = filtered(reserve, True) or filtered(reserve, False)
        order = list(reserve)
    if not candidates:
        raise ValueError("no breadth candidate available")
    def rate(model):
        row = stats_implementers.get(f"pi/{model}", {})
        return (row.get("passed", 0) + 1) / (row.get("lanes", 0) + 2)
    positions = {model: index for index, model in enumerate(order)}
    ranked = sorted(candidates, key=lambda m: (-rate(m), positions.get(m, 0)))
    return {"engine": "pi", "model": ranked[0], "provider": "opencode2api",
            "reasoning_effort": "max", "fallbacks": ranked[1:]}


# ------------------------------------------------------------ run storage / IO

def runs_root():
    path = workers.root() / "runs"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def run_path(run_id):
    if not re.fullmatch(r"[0-9a-f]{12}", run_id):
        raise ValueError("invalid run ID")
    path = runs_root() / run_id
    if not (path / "events.jsonl").is_file():
        raise ValueError(f"unknown run: {run_id}")
    return path


def append(path, kind, /, **data):
    with (path / "events.jsonl").open("a+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.seek(0)
        sequence = sum(1 for _ in stream)
        stream.write(json.dumps({"seq": sequence, "time": time.time(), "type": kind, "data": data},
                                ensure_ascii=True) + "\n")
        stream.flush()
    return sequence


def events(path):
    with (path / "events.jsonl").open() as stream:
        return [json.loads(line) for line in stream if line.strip()]


def load_state(run_id):
    path = run_path(run_id)
    return path, fold(events(path))


def git(repo, *arguments, check=True, env=None):
    result = subprocess.run(["git", "-C", str(repo), *arguments], capture_output=True, text=True,
                            env=dict(os.environ, **(env or {})))
    if check and result.returncode:
        raise ValueError(f"git {' '.join(arguments)} failed: {result.stderr.strip()[:500]}")
    return result.stdout.strip()


def bounded(text, limit):
    if len(text) <= limit:
        return text
    half = limit // 2
    return text[:half] + f"\n\n[... {len(text) - limit} characters omitted ...]\n\n" + text[-half:]


def cache_root_for_repo(repo_path):
    """Cache root for one repository; created on demand for CLAIVE_ORCH_CACHE."""
    digest = hashlib.sha256(str(repo_path).encode()).hexdigest()[:16]
    root = runs_root().parent / "orch-cache" / digest
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return root


def lane_local_paths(lane, config=None):
    """Repo-relative paths that stay out of checkpoints (setup outputs plus cache paths)."""
    paths = set()
    for entry in (lane.get("local_paths") if lane else None) or []:
        cleaned = str(entry).strip().rstrip("/")
        if cleaned and cleaned != ".":
            paths.add(cleaned)
    for entry in ((config or {}).get("cache_paths") or []):
        cleaned = str(entry).strip().rstrip("/")
        if cleaned and cleaned != ".":
            paths.add(cleaned)
    return sorted(paths)


def list_untracked(worktree):
    """Untracked paths in a worktree (repo-relative, trailing slashes stripped)."""
    output = git(worktree, "ls-files", "--others", "--exclude-standard", "--directory",
                 "--no-empty-directory", check=False)
    entries = set()
    for line in output.splitlines():
        cleaned = line.strip().rstrip("/")
        if cleaned:
            entries.add(cleaned)
    return entries


BYPRODUCT_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}


def is_byproduct(path):
    """True for verifier byproducts (bytecode, test and lint caches); see BYPRODUCT_EXCLUDES."""
    parts = Path(path).parts
    return bool(BYPRODUCT_DIRS.intersection(parts)) or path.endswith((".pyc", ".pyo"))


def porcelain_is_local(line, local_paths):
    """True when a porcelain status line refers only to local paths or verifier byproducts."""
    if len(line) < 4:
        return False
    cleaned_locals = [p.rstrip("/") for p in local_paths if p.rstrip("/")]

    def is_local(path):
        candidate = path.strip().strip('"').rstrip("/")
        if is_byproduct(candidate):
            return True
        for local in cleaned_locals:
            if candidate == local or candidate.startswith(local + "/"):
                return True
        return False

    part = line[3:]
    if " -> " in part:
        return all(is_local(piece) for piece in part.split(" -> "))
    return is_local(part)


def dirty_outside_local(worktree, local_paths):
    """Porcelain lines for changes outside the local paths."""
    output = git(worktree, "status", "--porcelain", check=False)
    return [line for line in output.splitlines()
            if line.strip() and not porcelain_is_local(line, local_paths)]


def remove_tree(path):
    """Remove a file, directory or symlink without following symlinks."""
    target = Path(path)
    try:
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target)
        elif os.path.lexists(target):
            target.unlink()
    except FileNotFoundError:
        pass


def cache_entry(entry):
    """Return a cache path normalized relative to the worktree, or None when it is unsafe.

    Restoring an entry deletes and replaces it, so "." (the whole worktree), anything with
    "..", absolute paths and .git are refused.
    """
    cleaned = str(entry).strip()
    if not cleaned or Path(cleaned).is_absolute() or ".." in Path(cleaned).parts:
        return None
    normal = os.path.normpath(cleaned)
    if normal in {".", ""} or Path(normal).parts[0] == ".git":
        return None
    return normal


def copy_cached_entry(source, destination):
    """Copy one cached path, keeping symlinks as symlinks."""
    src, dst = Path(source), Path(destination)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_symlink():
        remove_tree(dst)
        dst.symlink_to(os.readlink(src))
    elif src.is_dir() and not src.is_symlink():
        remove_tree(dst)
        shutil.copytree(src, dst, symlinks=True)
    else:
        remove_tree(dst)
        shutil.copy2(src, dst)


def copy_acceptance(source, target):
    """Copy held-out checks into a workspace; return the created paths for removal."""
    created = []
    for file in sorted(Path(source).rglob("*")):
        if not file.is_file():
            continue
        destination = Path(target) / file.relative_to(source)
        if destination.exists():
            remove_paths(created)
            raise ValueError(f"held-out check collides with a workspace file: {destination}")
        for parent in reversed(destination.relative_to(target).parents[:-1]):
            directory = Path(target) / parent
            if not directory.exists():
                directory.mkdir()
                created.append(directory)
        shutil.copy2(file, destination)
        created.append(destination)
    return created


def remove_paths(paths):
    for path in reversed(paths):
        if path.is_dir():
            try:
                path.rmdir()
            except OSError:
                pass
        else:
            path.unlink(missing_ok=True)


def clear_stale_pycache(created):
    """Drop .pyc caches for copied held-out checks so an edited check is re-read."""
    for entry in created:
        path = Path(entry)
        if not path.is_file() or path.suffix != ".py":
            continue
        cache_dir = path.parent / "__pycache__"
        if cache_dir.is_dir():
            for cached in cache_dir.glob(f"{path.stem}.*.pyc"):
                try:
                    cached.unlink()
                except OSError:
                    pass
        legacy = path.with_suffix(".pyc")
        try:
            if legacy.is_file():
                legacy.unlink()
        except OSError:
            pass


def run_verifier(config, workspace, output_file):
    """Run the configured verifier in a workspace; return a VerificationResult dict."""
    created = []
    started = time.time()
    cache_root = cache_root_for_repo(config.get("repo", ""))
    env = dict(os.environ, CLAIVE_ORCH_CACHE=str(cache_root), CLAIVE_ORCH_REPO=str(config.get("repo", "")))
    try:
        if config.get("acceptance_dir"):
            created = copy_acceptance(config["acceptance_dir"], workspace)
            clear_stale_pycache(created)
        limit = memcap.parse_size(config["verify_memory"]) if config.get("verify_memory") else None
        # Runs in its own session so a timeout or memory overrun kills the verifier's children too.
        process = memcap.run_capped(["bash", "-c", config["verify"]], limit, config["verify_timeout"],
                                    cwd=workspace, env=env)
        output, code, status = process["stdout"] + process["stderr"], process["code"], None
        if process["timed_out"]:
            status = "error"
            output += f"\n[claive-orch: verifier timed out after {config['verify_timeout']}s]"
        elif process["exceeded"]:
            status = "error"
            output += (f"\n[claive-orch: verifier exceeded memory limit {config['verify_memory']} "
                       f"(peak sampled {memcap.format_size(process['peak'])}) and was killed]")
    except ValueError as error:
        output, code, status = str(error), None, "error"
    finally:
        remove_paths(created)
    Path(output_file).write_text(output)
    counts = parse_score(output, config.get("score_regex"))
    passed = code == 0 and status is None
    if counts and counts[1]:
        score = counts[0] / counts[1]
    else:
        score = 1.0 if passed else 0.0
    return {"passed": passed, "status": status or ("pass" if passed else "fail"), "exit_code": code,
            "counts": list(counts) if counts else None, "score": round(score, 6),
            "seconds": round(time.time() - started, 1), "output": str(output_file)}


def run_cache_key(config, workspace):
    """Run the cache-key command; return the hex key or raise ValueError."""
    cache_root = cache_root_for_repo(config.get("repo", ""))
    env = dict(os.environ, CLAIVE_ORCH_CACHE=str(cache_root))
    try:
        process = subprocess.run(["bash", "-c", config["cache_key"]], cwd=str(workspace),
                                 capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL, env=env)
    except subprocess.TimeoutExpired as error:
        raise ValueError(f"cache-key command timed out: {(error.stdout or '')[-500:]}")
    if process.returncode:
        detail = ((process.stdout or "") + (process.stderr or "")).strip()[-500:]
        raise ValueError(f"cache-key command failed: {detail}")
    stripped = (process.stdout or "").strip()
    return hashlib.sha256(stripped.encode()).hexdigest()


def restore_cache(cache_dir, workspace, cache_paths):
    for entry in cache_paths:
        cleaned = cache_entry(entry)
        if cleaned is None:
            continue
        source, destination = Path(cache_dir) / cleaned, Path(workspace) / cleaned
        if os.path.lexists(source):
            copy_cached_entry(source, destination)


def store_cache(cache_root, key, workspace, cache_paths):
    cache_dir = Path(cache_root) / key
    if cache_dir.exists():
        return False
    tmpdir = Path(cache_root) / f"{key}.tmp-{os.getpid()}"
    try:
        if os.path.lexists(tmpdir):
            remove_tree(tmpdir)
        tmpdir.mkdir(parents=True, exist_ok=True)
        for entry in cache_paths:
            cleaned = cache_entry(entry)
            if cleaned is None:
                continue
            source, destination = Path(workspace) / cleaned, tmpdir / cleaned
            if os.path.lexists(source):
                copy_cached_entry(source, destination)
        try:
            os.rename(tmpdir, cache_dir)
        except OSError:
            if cache_dir.exists():
                remove_tree(tmpdir)
                return False
            remove_tree(tmpdir)
            return False
    except OSError:
        try:
            remove_tree(tmpdir)
        except OSError:
            pass
        return False
    return True


def verifier_timed_out(result):
    if result.get("exit_code") is not None:
        return False
    try:
        text = Path(result["output"]).read_text()
        return "timed out" in text or "exceeded memory limit" in text
    except OSError:
        return result.get("status") == "error"


def verify_with_cache(config, workspace, output_file, run_path):
    """Run the verifier with build-cache restore/store around it (lanes and base check)."""
    cache_key, cache_paths = config.get("cache_key"), config.get("cache_paths") or []
    if not cache_key or not cache_paths:
        return run_verifier(config, workspace, output_file)
    try:
        key = run_cache_key(config, workspace)
    except ValueError as error:
        append(run_path, "warning", message=f"verifier cache skipped: {error}")
        return run_verifier(config, workspace, output_file)
    root = cache_root_for_repo(config.get("repo", ""))
    cache_dir = root / key
    hit = cache_dir.exists()
    if hit:
        restore_cache(cache_dir, workspace, cache_paths)
        print(f"Cache hit {key[:12]}")
    result = run_verifier(config, workspace, output_file)
    if not verifier_timed_out(result) and not cache_dir.exists():
        if store_cache(root, key, workspace, cache_paths):
            print(f"Cache stored {key[:12]}")
    return result


def run_setup(repo, worktree, command, timeout):
    """Run the lane setup command in a worktree; raise ValueError with the output tail on failure."""
    cache_root = cache_root_for_repo(repo)
    env = dict(os.environ, CLAIVE_ORCH_REPO=str(repo), CLAIVE_ORCH_LANE=str(worktree),
               CLAIVE_ORCH_CACHE=str(cache_root))
    try:
        process = subprocess.run(["bash", "-c", command], cwd=str(worktree), capture_output=True,
                                 text=True, timeout=timeout, stdin=subprocess.DEVNULL, env=env)
    except subprocess.TimeoutExpired as error:
        output = ""
        if isinstance(error.stdout, str):
            output += error.stdout
        if isinstance(error.stderr, str):
            output += error.stderr
        raise ValueError(f"setup command timed out in {worktree}: {output[-2000:]}")
    output = (process.stdout or "") + (process.stderr or "")
    if process.returncode:
        raise ValueError(f"setup command failed in {worktree}: {output[-2000:]}")
    return output


def close_idle_reusable_workers(state):
    """Close idle reusable workers registered in this run; failures are warnings."""
    seen = []
    for name in state["lane_order"]:
        for worker in state["lanes"][name]["workers"]:
            worker_id = worker.get("worker_id")
            if worker_id and worker_id not in seen:
                seen.append(worker_id)
    claive_bin = Path(__file__).resolve().parent.parent / "claive"
    for worker_id in seen:
        try:
            _, record = worker_record(worker_id)
        except (ValueError, OSError) as error:
            print(f"warning: could not read worker {worker_id}: {error}")
            continue
        if not record.get("reusable") or record.get("status") != "idle":
            continue
        try:
            process = subprocess.run([str(claive_bin), "close", worker_id], capture_output=True,
                                     text=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as error:
            print(f"warning: could not close worker {worker_id}: {error}")
            continue
        if process.returncode:
            detail = (process.stderr or process.stdout or "").strip()[:300]
            print(f"warning: could not close worker {worker_id}: {detail}")
        else:
            print(f"Closed worker {worker_id}")


def worker_record(worker_id):
    path = workers.job_path(worker_id)
    return path, workers.load(path)


def worker_busy(path, state):
    """True while a turn is in flight (an idle reusable worker with no queued follow-up is done)."""
    if state["status"] == "idle":
        return bool(list((path / "requests").glob("*.json")))
    return state["status"] in workers.ACTIVE


def worker_answer(worker_id):
    path, state = worker_record(worker_id)
    result = path / "result.txt"
    if worker_busy(path, state):
        raise ValueError(f"worker {worker_id} is still running; wait for its turn first")
    if not result.is_file():
        raise ValueError(f"worker {worker_id} has no answer yet")
    return result.read_text()


# ------------------------------------------------------------------- prompts

WORKER_RULES = """Rules:
- Work only inside {workspace}. You are the only writer in this directory.
- Do not commit, push, create branches, or touch git history; the orchestrator checkpoints your work.
- Do not delegate to subagents or spawn other agents.
- Do not edit or delete existing tests to make them pass unless the task explicitly asks for it.
{checks}
- Finish with a short report: outcome, files changed, checks run with results, open doubts, blockers."""

CRITIC_SCHEMA = """Answer with a short explanation, then exactly one fenced ```json block:
{
  "no_concrete_defect": false,
  "approach_sound": true,
  "defects": [
    {"location": "path/file.py:42 or test name", "description": "what is wrong and why",
     "evidence": "verifier" | "diff" | "both", "localized": true}
  ]
}
- Report only concrete defects you can point at with evidence from the diff or the verifier output.
- "localized": false means the defect cannot be fixed in place and needs a different approach.
- "approach_sound": false means the overall approach is wrong.
- If you cannot find a concrete defect, set "no_concrete_defect": true and leave "defects" empty.
  Do not invent defects, style nits, or speculative risks."""


# Verifier byproducts that are never part of a candidate, even when the repo does not ignore them.
BYPRODUCT_EXCLUDES = [":(exclude,glob)**/__pycache__/**", ":(exclude,glob)**/*.py[co]",
                      ":(exclude,glob)**/.pytest_cache/**", ":(exclude,glob)**/.mypy_cache/**",
                      ":(exclude,glob)**/.ruff_cache/**"]


def stage_pathspecs(locals_):
    """Pathspecs for staging a lane: everything except local setup/cache paths and verifier byproducts."""
    return ["--", ".", *[f":(exclude,top){p}" for p in locals_], *BYPRODUCT_EXCLUDES]


def lane_diff(state, lane):
    workspace = lane["path"]
    locals_ = lane_local_paths(lane, state.get("config"))
    git(workspace, "add", "-A", "--intent-to-add", *stage_pathspecs(locals_), check=False)
    return git(workspace, "diff", lane["base_commit"], check=False)


def current_result(lane):
    """Verification describing the workspace as it is now (the best checkpoint after a revert)."""
    if lane["reverted_last"] and lane["best"]:
        return lane["best"]
    return lane["verifications"][-1] if lane["verifications"] else None


def last_output(lane):
    if not lane["verifications"]:
        return "(not verified yet)"
    path = Path(current_result(lane)["output"])
    text = path.read_text() if path.is_file() else ""
    return bounded(text, OUTPUT_LIMIT)


def describe(result):
    if result is None:
        return "unknown"
    counts = f" ({result['counts'][0]}/{result['counts'][1]} checks)" if result.get("counts") else ""
    return f"{result['status']}, score {result['score']:.3f}{counts}"


def worker_checks(config):
    """Return (command workers may run, note about held-out checks). Hidden check names never leak."""
    command = config.get("worker_verify") or (None if config.get("acceptance_dir") else config["verify"])
    if command and config.get("verify_memory"):
        command = f"claive-memcap {config['verify_memory']} -- bash -c {shlex.quote(command)}"
    note = ("Held-out checks you cannot see also run in the orchestrator; their files are not in the "
            "workspace, so do not look for them. Their failures appear in the verifier output you are given."
            if config.get("acceptance_dir") else "")
    return command, note


def build_prompt(state, kind, lane_name=None):
    config = state["config"]
    task = Path(config["task_file"]).read_text()
    lane = state["lanes"].get(lane_name) if lane_name else None
    if kind != "review" and lane is None:
        raise ValueError(f"unknown lane: {lane_name}")
    command, held_out = worker_checks(config)
    checks = "\n".join(line for line in (
        f"- You may run the visible checks yourself: `{command}` (in {lane['path']})." if command and lane else "",
        f"- {held_out}" if held_out else "") if line) or "- The orchestrator runs verification; you have no local check."
    rules = WORKER_RULES.format(workspace=lane["path"], checks=checks) if lane else ""
    if kind == "implement":
        strategy = f"\nStrategy for this attempt: {lane['strategy']}\n" if lane.get("strategy") else ""
        return f"# Task\n\n{task}\n{strategy}\nWorkspace: {lane['path']}\n\n{rules}\n"
    if kind == "critique":
        previous = [c for c in lane["critiques"]]
        history = "\n".join(f"- round {c['round']}: " + "; ".join(d["description"][:200] for d in c["defects"])
                            for c in previous if c["defects"]) or "(none)"
        diff = bounded(lane_diff(state, lane), DIFF_LIMIT) or "(no changes)"
        binding = ("Treat the task's constraints as binding: never propose relaxing, bypassing or weakening "
                   "a check, gate, validation or test to make verification pass.")
        if config.get("post_pass_critic"):
            limit = config["rounds"] if lane_name == "a" else min(config["rounds"], BREADTH_ROUNDS)
            status, _ = lane_status(config, lane, limit)
            if status == "passed" and not any(c.get("post_pass") for c in lane["critiques"]):
                return (f"# Post-pass review\n\nYou are a critic. You do not edit files. The verifier passes "
                        f"({describe(current_result(lane))}), so look for behaviour changes beyond the task: "
                        f"regressions, changed public behaviour, swallowed errors, wrong counts on error paths, "
                        f"untested paths. {binding}\n\n## Task\n\n{task}\n\n"
                        f"## Verification\n\n`{command or '(held-out checks only)'}` -> "
                        f"{describe(current_result(lane))}"
                        f"{' (plus held-out checks)' if held_out else ''}\n{held_out}\n\n"
                        f"## Verifier output (bounded)\n\n```text\n{last_output(lane)}\n```\n\n"
                        f"## Diff against base {lane['base_commit'][:12]}\n\n```diff\n{diff}\n```\n\n"
                        f"The workspace is {lane['path']}; you may read files there for context.\n\n"
                        f"{CRITIC_SCHEMA}\n")
        return (f"# Critique a candidate solution\n\nYou are a critic. You do not edit files. "
                f"Find concrete defects that explain why verification fails. {binding}\n\n## Task\n\n{task}\n\n"
                f"## Verification\n\n`{command or '(held-out checks only)'}` -> {describe(current_result(lane))}"
                f"{' (plus held-out checks)' if held_out else ''}\n{held_out}\n\n"
                f"## Verifier output (bounded)\n\n```text\n{last_output(lane)}\n```\n\n"
                f"## Diff against base {lane['base_commit'][:12]}\n\n```diff\n{diff}\n```\n\n"
                f"## Defects named in earlier rounds\n\n{history}\n\nThe workspace is {lane['path']}; "
                f"you may read files there for context.\n\n{CRITIC_SCHEMA}\n")
    if kind == "correct":
        rounds = lane_rounds(lane) + 1
        critique = [c for c in lane["critiques"] if c["round"] == rounds]
        reverted = ""
        if lane["reverted_last"]:
            reverted = (f"\nIMPORTANT: your previous changes made verification worse and were reverted. "
                        f"The workspace is back at the best checkpoint ({describe(lane['best'])}). "
                        f"Re-read the files before editing.\n")
        rejected_section = ""
        if critique and critique[-1].get("rejected"):
            lines = "\n".join(f"{r['index']}. {r['description']} (reason: {r['reason']}) Do not apply this."
                              for r in critique[-1]["rejected"])
            rejected_section = f"\n## Rejected suggestions\n\n{lines}\n"
        if critique and critique[-1]["defects"]:
            defects = "\n".join(f"{i}. [{d['location']}] {d['description']} (evidence: {d['evidence']})"
                                for i, d in enumerate(critique[-1]["defects"], 1))
            body = f"An independent critic found these defects:\n\n{defects}\n{rejected_section}"
        else:
            body = "Use the verifier output below to find and fix the failure.\n" + rejected_section
        return (f"# Correction round {rounds}\n\nVerification currently: {describe(current_result(lane))}."
                f"\n{reverted}\n{body}\n## Verifier output (bounded)\n\n```text\n{last_output(lane)}\n```\n\n"
                f"Fix these problems in place. Keep everything that already passes passing. "
                f"Do not restart from scratch.\n\n{rules}\n")
    if kind == "review":
        parts = []
        for name in state["lane_order"]:
            item = state["lanes"][name]
            diff = git(item["path"], "diff", item["base_commit"], item["best_commit"], check=False)
            parts.append(f"## Candidate {name}\n\nVerification: {describe(item['best'])}\n\n"
                         f"```diff\n{bounded(diff, DIFF_LIMIT // 2)}\n```\n")
        return (f"# Compare two candidate solutions\n\nBoth candidates tie on verification. You do not edit "
                f"files.\n\n## Task\n\n{task}\n\n" + "\n".join(parts) +
                "\nPrefer correctness, then fit to the task, then simplicity. Answer with a short explanation, "
                "then one fenced ```json block: {\"prefer\": \"a\" | \"b\" | \"none\", \"reason\": \"...\"}\n")
    raise ValueError(f"unknown prompt kind: {kind}")


# ------------------------------------------------------------------- commands

def command_init(args):
    if args.experiment and args.post_pass_critic:
        raise ValueError("--post-pass-critic is not allowed in experiment runs: "
                         "the post-pass critic changes the arm being measured")
    repo = Path(args.repo)
    task = Path(args.task_file)
    if not repo.is_absolute() or not (repo / ".git").exists():
        raise ValueError("--repo must be an absolute path to a git repository")
    if not task.is_absolute() or not task.is_file() or not task.stat().st_size:
        raise ValueError("--task-file must be an existing nonempty absolute file")
    if args.arm not in ARMS:
        raise ValueError(f"unsupported arm {args.arm}; choose one of {', '.join(ARMS)}")
    if not 1 <= args.rounds <= MAX_ROUNDS:
        raise ValueError(f"--rounds must be between 1 and {MAX_ROUNDS}")
    if args.post_pass_critic and args.arm not in {"B", "D"}:
        raise ValueError("--post-pass-critic is allowed only with arms B and D")
    if args.acceptance_dir:
        acceptance = Path(args.acceptance_dir)
        if not acceptance.is_absolute() or not acceptance.is_dir():
            raise ValueError("--acceptance-dir must be an existing absolute directory")
        if acceptance.resolve().is_relative_to(repo.resolve()):
            raise ValueError("--acceptance-dir must be outside the repository so workers cannot see it")
    cache_key = getattr(args, "cache_key", None)
    raw_cache_paths = getattr(args, "cache_path", None) or []
    cache_paths = [p for group in raw_cache_paths for p in (group if isinstance(group, list) else [group])]
    if bool(cache_key) != bool(cache_paths):
        raise ValueError("--cache-key and --cache-path go together (both or neither)")
    verify_memory = getattr(args, "verify_memory", None)
    if verify_memory:
        memcap.parse_size(verify_memory)
    if args.score_regex is not None:
        try:
            groups = re.compile(args.score_regex).groupindex
        except re.error as error:
            raise ValueError(f"--score-regex is not a valid regex: {error}") from error
        if "passed" not in groups or not {"failed", "total"} & set(groups):
            raise ValueError("--score-regex needs named groups passed and failed or total")
    for entry in cache_paths:
        if cache_entry(entry) is None:
            raise ValueError(f"--cache-path must be a repo-relative path inside the worktree "
                             f"(not '.', '..' or .git): {entry}")
    base = git(repo, "rev-parse", "--verify", (args.base or "HEAD") + "^{commit}")
    run_id = uuid.uuid4().hex[:12]
    path = runs_root() / run_id
    (path / "prompts").mkdir(parents=True)
    (path / "verify").mkdir()
    (path / "lanes").mkdir()
    shutil.copy2(task, path / "task.md")
    config = {"run_id": run_id, "repo": str(repo), "base": base, "task_file": str(path / "task.md"),
              "task_id": args.task_id or task.stem, "arm": args.arm, "rounds": args.rounds,
              "verify": args.verify, "worker_verify": args.worker_verify, "verify_timeout": args.verify_timeout,
              "score_regex": args.score_regex, "acceptance_dir": args.acceptance_dir,
              "experiment": args.experiment, "repeat": args.repeat, "max_minutes": args.max_minutes,
              "label": args.label or task.stem, "post_pass_critic": bool(args.post_pass_critic),
              "setup": args.setup, "cache_key": cache_key, "cache_paths": cache_paths,
              "verify_memory": verify_memory, "category": args.category}
    append(path, "run.started", config=config)
    print(f"Run {run_id} | arm {args.arm} | base {base[:12]} | {path}")
    if args.skip_base_check:
        append(path, "warning", message="base verification skipped; the verifier is not known to fail at base")
    else:
        base_dir = path / "base"
        git(repo, "worktree", "add", "--detach", str(base_dir), base)
        try:
            if config.get("setup"):
                run_setup(repo, base_dir, config["setup"], config["verify_timeout"])
            result = verify_with_cache(config, base_dir, path / "verify" / "base.txt", path)
        finally:
            git(repo, "worktree", "remove", "--force", str(base_dir), check=False)
        append(path, "base.verified", result=result)
        print(f"Base verification: {describe(result)}")
        if result["passed"] and not args.allow_passing_base:
            append(path, "run.finished", outcome="invalid", reason="verifier already passes at base")
            raise ValueError("verifier already passes at the base revision; the task cannot discriminate "
                             "(use --allow-passing-base to override)")
    print(f"Next: claive-orch next {run_id}")
    return 0


def command_lane(args):
    args.engine = args.engine or default_engine()
    path, state = load_state(args.run)
    config = state["config"]
    name = args.name
    if not re.fullmatch(r"[a-z]", name):
        raise ValueError("lane names are single lowercase letters (a, b)")
    if name in state["lanes"]:
        raise ValueError(f"lane {name} already exists")
    if len(state["lanes"]) >= MAX_LANES:
        raise ValueError(f"at most {MAX_LANES} candidates per run")
    expected = decide(state)
    if expected["action"] != "add_lane" or expected["lane"] != name:
        raise ValueError(f"the arbiter does not allow adding lane {name} now; next action is {expected['action']}")
    family = model_family(args.model, args.engine)
    if state["lanes"]:
        first = state["lanes"][state["lane_order"][0]]
        same = (first["engine"] == args.engine and first["family"] == family and
                (args.model or "") == (first.get("model") or ""))
        differs = (first["engine"] != args.engine or first["family"] != family or
                   bool(args.strategy and args.strategy != first.get("strategy")))
        if config["arm"] == "R" and not same:
            raise ValueError("arm R resamples the same configuration: use the same engine and model as lane a")
        if config["arm"] in {"R'", "D"} and not differs:
            raise ValueError("the second candidate must differ deliberately from lane a "
                             "(engine, model family, or strategy); temperature alone does not count")
    branch = f"orch/{config['run_id']}/{name}"
    workspace = path / "lanes" / name
    git(config["repo"], "worktree", "add", "-b", branch, str(workspace), config["base"])
    before = list_untracked(workspace)
    if config.get("setup"):
        try:
            run_setup(config["repo"], workspace, config["setup"], config["verify_timeout"])
        except ValueError:
            git(config["repo"], "worktree", "remove", "--force", str(workspace), check=False)
            git(config["repo"], "branch", "-D", branch, check=False)
            raise
    after = list_untracked(workspace) if config.get("setup") else set(before)
    local_paths = sorted((after - before) | {str(p).strip().rstrip("/")
                                             for p in config.get("cache_paths") or [] if str(p).strip().rstrip("/")})
    append(path, "lane.added", lane=name, engine=args.engine, model=args.model, family=family,
           strategy=args.strategy, branch=branch, path=str(workspace), base_commit=config["base"],
           local_paths=local_paths)
    print(f"Lane {name} | {args.engine} {args.model or '(engine default)'} | family {family} | {workspace}")
    return 0


def command_worker(args):
    path, state = load_state(args.run)
    lane = state["lanes"].get(args.lane)
    if lane is None:
        raise ValueError(f"unknown lane: {args.lane}")
    _, record = worker_record(args.worker_id)
    engine = record.get("engine", DEFAULT_ENGINE)
    model = record.get("model") or (record.get("launch") or {}).get("model")
    family = model_family(model, engine)
    if args.role == "implementer":
        if engine != lane["engine"] or (lane.get("model") and model != lane["model"]):
            raise ValueError(f"implementer {engine}/{model} does not match lane {args.lane} "
                             f"({lane['engine']}/{lane.get('model')})")
        if Path(record.get("actual_workspace") or record["workspace"]).resolve() != Path(lane["path"]).resolve():
            raise ValueError(f"implementer workspace must be the lane worktree {lane['path']}")
    else:
        if not (record.get("launch") or {}).get("read_only"):
            raise ValueError(f"{args.role} {args.worker_id} must be a read-only worker (launch it with --read-only); "
                             "it would otherwise be able to edit the lane")
        candidates = [(model, family)]
        for fallback in record.get("fallback_models") or []:
            candidates.append((fallback, model_family(fallback, engine)))
        clash = next((candidate_family for _, candidate_family in candidates
                      if candidate_family == lane["family"]), None)
        if clash is not None:
            if not args.allow_same_family:
                raise ValueError(f"{args.role} family {clash} matches the implementer's; choose another model "
                                 "family or pass --allow-same-family (recorded)")
            append(path, "warning", message=f"{args.role} {args.worker_id} shares family {clash} "
                                            f"with lane {args.lane}")
    append(path, "worker.registered", lane=args.lane, role=args.role, worker_id=args.worker_id,
           engine=engine, model=model, family=family)
    print(f"Registered {args.role} {args.worker_id} ({engine} {model or 'default'}) on lane {args.lane}")
    return 0


def command_prompt(args):
    path, state = load_state(args.run)
    expected = decide(state)
    if expected["action"] != args.kind or (args.kind != "review" and expected["lane"] != args.lane):
        if not args.force:
            raise ValueError(f"the arbiter does not expect a {args.kind} prompt for lane {args.lane or '-'} now "
                             f"(next action: {expected['action']} {expected.get('lane') or ''}); "
                             "run claive-orch next")
        append(path, "warning", message=f"forced {args.kind} prompt for lane {args.lane} "
                                         f"while next was {expected['action']}")
    text = build_prompt(state, args.kind, args.lane)
    lane = state["lanes"].get(args.lane) if args.lane else None
    round_number = lane_rounds(lane) + (0 if args.kind == "implement" else 1) if lane else 0
    target = Path(args.out) if args.out else path / "prompts" / f"{args.lane or 'all'}-{args.kind}-r{round_number}.md"
    target.write_text(text)
    print(target)
    return 0


def command_verify(args):
    path, state = load_state(args.run)
    config = state["config"]
    lane = state["lanes"].get(args.lane)
    if lane is None:
        raise ValueError(f"unknown lane: {args.lane}")
    expected = decide(state)
    if expected["action"] not in {"verify", "correct", "critique"} or expected["lane"] != args.lane:
        if not args.force:
            raise ValueError(f"the arbiter does not expect verification of lane {args.lane} now "
                             f"(next action: {expected['action']} {expected.get('lane') or ''})")
    if expected["action"] == "critique":
        raise ValueError("record the critique before verifying the correction")
    for worker in lane["workers"]:
        if worker["role"] == "implementer":
            job, record = worker_record(worker["worker_id"])
            if worker_busy(job, record):
                raise ValueError(f"implementer {worker['worker_id']} is still running; wait for it first")
    round_number = len(lane["verifications"])
    locals_ = lane_local_paths(lane, config)
    result = verify_with_cache(config, lane["path"], path / "verify" / f"{args.lane}-r{round_number}.txt", path)
    result["round"] = round_number
    best = lane["best"] or state["base"]
    refining = config["arm"] in REFINING
    # Git work happens before any event is written: a failing reset or commit must not leave a
    # recorded round without its checkpoint.
    if refining and best is not None and verification_key(result) < verification_key(best):
        # Keep the rejected candidate reachable before resetting (the first round has no
        # earlier checkpoint of its own to fall back to).
        git(lane["path"], "add", "-A", *stage_pathspecs(locals_))
        git(lane["path"], *NO_SIGN, "commit", "-q", "--no-verify", "--allow-empty", "-m",
            f"claive-orch {config['run_id']} lane {args.lane} round {round_number} (rejected): {describe(result)}",
            env=GIT_IDENTITY)
        rejected = git(lane["path"], "rev-parse", "HEAD")
        rejected_ref = f"refs/claive-orch/{config['run_id']}/{args.lane}/rejected-r{round_number}"
        git(lane["path"], "update-ref", rejected_ref, rejected)
        git(lane["path"], "reset", "-q", "--hard", lane["best_commit"])
        clean_args = ["clean", "-fdq"]
        for entry in locals_:
            clean_args += ["-e", f"/{entry}"]
        git(lane["path"], *clean_args)
        append(path, "verification.completed", lane=args.lane, result=result)
        append(path, "checkpoint.reverted", lane=args.lane, round=round_number, result=result,
               to_commit=lane["best_commit"], best=best, rejected_ref=rejected_ref)
        print(f"Lane {args.lane} round {round_number}: {describe(result)} is worse than {describe(best)}; "
              f"reverted to {lane['best_commit'][:12]} (candidate kept at {rejected_ref})")
    else:
        # Local setup/cache outputs and verifier byproducts are never committed; a worker
        # change inside a local path is therefore also left uncommitted (documented limitation).
        git(lane["path"], "add", "-A", *stage_pathspecs(locals_))
        git(lane["path"], *NO_SIGN, "commit", "-q", "--no-verify", "--allow-empty", "-m",
            f"claive-orch {config['run_id']} lane {args.lane} round {round_number}: {describe(result)}",
            env=GIT_IDENTITY)
        commit = git(lane["path"], "rev-parse", "HEAD")
        improved = best is None or verification_key(result) > verification_key(best)
        stat = git(lane["path"], "diff", "--shortstat", lane["base_commit"], commit, check=False)
        lines = sum(int(n) for n in re.findall(r"(\d+) (?:insertion|deletion)", stat))
        append(path, "verification.completed", lane=args.lane, result=result)
        append(path, "checkpoint.accepted", lane=args.lane, round=round_number, result=result,
               commit=commit, improved=improved, diff_lines=lines)
        print(f"Lane {args.lane} round {round_number}: {describe(result)}; checkpoint {commit[:12]}"
              + ("" if improved else " (no improvement)"))
    print(f"Verifier output: {result['output']}")
    return 0


def command_critique(args):
    path, state = load_state(args.run)
    lane = state["lanes"].get(args.lane)
    if lane is None:
        raise ValueError(f"unknown lane: {args.lane}")
    if state["config"]["arm"] == "B0":
        raise ValueError("arm B0 is the critic-less ablation; critiques are not allowed")
    expected = decide(state)
    if expected["action"] != "critique" or expected["lane"] != args.lane:
        raise ValueError(f"the arbiter does not expect a critique of lane {args.lane} now "
                         f"(next action: {expected['action']})")
    model = family = fallbacks = None
    if args.worker_id:
        _, record = worker_record(args.worker_id)
        model = record.get("model") or (record.get("launch") or {}).get("model")
        family = model_family(model, record.get("engine"))
        fallbacks = record.get("fallbacks")
        if not any(w["worker_id"] == args.worker_id and w["role"] == "critic" for w in lane["workers"]):
            raise ValueError(f"register the critic first: claive-orch worker {args.run} {args.lane} "
                             f"--role critic --worker-id {args.worker_id}")
        text = worker_answer(args.worker_id)
    else:
        text = Path(args.file).read_text()
    try:
        critique = validate_critique(extract_json(text))
    except ValueError as error:
        critique = {"no_concrete_defect": True, "approach_sound": True, "defects": [],
                    "error": f"unparseable critique: {error}"}
    critique.update(lane=args.lane, round=expected["round"], worker_id=args.worker_id,
                    model=model, family=family, post_pass=bool(expected.get("post_pass")))
    if fallbacks:
        critique["fallbacks"] = fallbacks
    append(path, "critique.completed", **critique)
    summary = "no concrete defect" if critique["no_concrete_defect"] else f"{len(critique['defects'])} defect(s)"
    print(f"Critique for lane {args.lane} round {expected['round']}: {summary}"
          + (f" [error: {critique['error']}]" if critique.get("error") else ""))
    return 0


def command_review(args):
    path, state = load_state(args.run)
    expected = decide(state)
    if expected["action"] != "review":
        raise ValueError(f"the arbiter does not expect a review now (next action: {expected['action']})")
    if args.worker_id:
        text = worker_answer(args.worker_id)
        try:
            raw = extract_json(text)
        except ValueError:
            raw = {}
        prefer, reason = raw.get("prefer"), str(raw.get("reason", ""))[:2000]
    else:
        prefer, reason = args.prefer, args.reason or ""
    if prefer not in set(state["lane_order"]) | {"none"}:
        prefer = "none"
    append(path, "review.completed", prefer=prefer, reason=reason, worker_id=args.worker_id)
    print(f"Review: prefer {prefer}")
    return 0


def command_rescore(args):
    path, state = load_state(args.run)
    config = state["config"]
    reason = getattr(args, "reason", None)
    if reason is None or not str(reason).strip():
        raise ValueError("--reason is required for rescore")
    if state["finished"]:
        raise ValueError(f"run {args.run} is already finished; rescore needs a running run")
    targets = [name for name in state["lane_order"] if state["lanes"][name]["verifications"]]
    for name in targets:
        lane = state["lanes"][name]
        locals_ = lane_local_paths(lane, config)
        if dirty_outside_local(lane["path"], locals_):
            raise ValueError(f"lane {name} has uncommitted changes outside local paths; "
                             "commit or discard them before rescore")
        for worker in lane["workers"]:
            if worker["role"] == "implementer":
                job, record = worker_record(worker["worker_id"])
                if worker_busy(job, record):
                    raise ValueError(f"lane {name} has uncommitted work: implementer "
                                     f"{worker['worker_id']} is still running")
    prior = [e for e in events(path) if e["type"] == "verification.rescored"]
    for name in targets:
        lane = state["lanes"][name]
        old = lane["verifications"][-1]
        count = sum(1 for e in prior if e["data"].get("lane") == name) + 1
        while (path / "verify" / f"{name}-rescore-{count}.txt").exists():
            count += 1
        result = verify_with_cache(config, lane["path"], path / "verify" / f"{name}-rescore-{count}.txt", path)
        result["round"] = old.get("round", len(lane["verifications"]) - 1)
        append(path, "verification.rescored", lane=name, reason=str(reason), result=result)
        prior.append({"type": "verification.rescored", "data": {"lane": name}})
        print(f"Lane {name} rescored: {describe(old)} -> {describe(result)}")
    return 0


def command_reject(args):
    path, state = load_state(args.run)
    lane = state["lanes"].get(args.lane)
    if lane is None:
        raise ValueError(f"unknown lane: {args.lane}")
    reason = getattr(args, "reason", None)
    if reason is None or not str(reason).strip():
        raise ValueError("--reason is required for reject")
    pending_round = lane_rounds(lane) + 1
    pending = [c for c in lane["critiques"] if c["round"] == pending_round]
    if not pending:
        raise ValueError(f"lane {args.lane} has no pending critique for round {pending_round}")
    critique = pending[-1]
    original = critique.get("_original_defects", critique.get("defects") or [])
    raw = getattr(args, "defect", None) or []
    numbers = []
    for group in raw:
        items = group if isinstance(group, list) else [group]
        numbers.extend(items)
    if not numbers:
        raise ValueError("--defect is required for reject")
    defects = []
    for number in numbers:
        try:
            index = int(number)
        except (TypeError, ValueError):
            raise ValueError(f"defect numbers are 1-based: {number}")
        if index < 1 or index > len(original):
            raise ValueError(f"defect {index} is out of range (1-{len(original)})")
        if index not in defects:
            defects.append(index)
    append(path, "critique.rejected", lane=args.lane, round=pending_round, defects=defects,
           reason=str(reason))
    print(f"Rejected defect(s) {','.join(str(n) for n in defects)} for lane {args.lane} round {pending_round}")
    return 0


GUIDE = {
    "add_lane": "claive-orch lane {run} {lane} --engine <muse|pi> --model <id> [--strategy TEXT]",
    "implement": ("P=$(claive-orch prompt {run} implement {lane}); launch an implementer with "
                  "--workspace {path} --prompt-file \"$P\"; claive-orch worker {run} {lane} --role implementer "
                  "--worker-id ID; claive wait ID; claive-orch verify {run} {lane}"),
    "verify": "claive wait <implementer>; claive-orch verify {run} {lane}",
    "critique": ("P=$(claive-orch prompt {run} critique {lane}); launch a read-only critic of a different model "
                 "family with --workspace {path} --read-only --prompt-file \"$P\"; claive-orch worker {run} {lane} "
                 "--role critic --worker-id CID; claive wait CID; claive-orch critique {run} {lane} "
                 "--worker-id CID; claive close CID"),
    "correct": ("P=$(claive-orch prompt {run} correct {lane}); claive followup {implementer} "
                "--prompt-file \"$P\"; claive wait {implementer}; claive-orch verify {run} {lane}"),
    "review": ("P=$(claive-orch prompt {run} review); launch a read-only reviewer; claive wait RID; "
               "claive-orch review {run} --worker-id RID"),
    "finish": ("claive-orch finish {run}; inspect `git -C {repo} diff {base} {commit}`; "
               "claive-orch integrate {run} if appropriate; claive-orch usage {run}; close workers; "
               "claive-orch cleanup {run} --branches"),
}


def command_next(args):
    path, state = load_state(args.run)
    decision = decide(state, now=time.time())
    lane = state["lanes"].get(decision.get("lane")) or {}
    implementer = next((w["worker_id"] for w in reversed(lane.get("workers", [])) if w["role"] == "implementer"),
                       "<implementer>")
    config = state["config"]
    guide = GUIDE.get(decision["action"], "").format(
        run=args.run, lane=decision.get("lane"), path=lane.get("path", ""), implementer=implementer,
        repo=config["repo"], base=config["base"][:12], commit=(decision.get("commit") or "")[:12])
    decision["how"] = guide
    history = [e for e in events(path) if e["type"] == "arbiter.action"]
    signature = {k: decision.get(k) for k in ("action", "lane", "round", "outcome")}
    if not history or {k: history[-1]["data"].get(k) for k in signature} != signature:
        append(path, "arbiter.action", **{k: v for k, v in decision.items() if k != "how"})
    if args.json:
        print(json.dumps(decision, indent=2))
    else:
        where = f" lane {decision['lane']}" if decision.get("lane") else ""
        rnd = f" round {decision['round']}" if decision.get("round") else ""
        print(f"NEXT: {decision['action']}{where}{rnd}" +
              (f" -> outcome {decision['outcome']}" if decision.get("outcome") else ""))
        print(f"Why:  {decision['reason']}")
        if guide:
            print(f"How:  {guide}")
    return 0


def command_finish(args):
    path, state = load_state(args.run)
    decision = decide(state, now=time.time())
    if decision["action"] == "done":
        print(f"Run already finished: {decision['outcome']}")
        return 0
    if decision["action"] != "finish" and not args.abort:
        raise ValueError(f"the arbiter's next action is {decision['action']}, not finish "
                         "(use --abort to stop early; recorded as aborted)")
    if decision["action"] != "finish":
        decision = finish(state, "aborted", args.reason or "stopped by the orchestrator")
    append(path, "run.finished", **{k: v for k, v in decision.items() if k != "action"})
    print(f"Run {args.run} finished: {decision['outcome']} ({decision['reason']})")
    if decision.get("commit"):
        print(f"Best: lane {decision['lane']} branch {decision['branch']} commit {decision['commit'][:12]}")
    return 0


def collect_usage(path, state):
    """Snapshot provider token usage for the run's workers; record and return it."""
    collected = {}
    for name in state["lane_order"]:
        for worker in state["lanes"][name]["workers"]:
            try:
                _, record = worker_record(worker["worker_id"])
                usage = get_engine(record.get("engine", DEFAULT_ENGINE)).session_usage(record)
                collected[worker["worker_id"]] = {"role": worker["role"], "lane": name,
                                                  "model": worker.get("model"), **usage}
            except (ValueError, OSError, subprocess.SubprocessError) as error:
                collected[worker["worker_id"]] = {"role": worker["role"], "lane": name,
                                                  "model": worker.get("model"), "error": str(error)[:300]}
    append(path, "usage.collected", workers=collected)
    return collected


def command_usage(args):
    path, state = load_state(args.run)
    collected = collect_usage(path, state)
    print(json.dumps(collected, indent=2) if args.json else
          f"Collected usage for {len(collected)} worker(s); see claive-orch report {args.run}")
    return 0


def summarize(run_id):
    path, state = load_state(run_id)
    config = state["config"]
    trail = [e["data"] for e in events(path) if e["type"] == "arbiter.action"]
    tokens = {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0}
    unknown_workers = 0
    has_totals = False
    for usage in state["usage"].values():
        totals = usage.get("totals") or {}
        valid = [key for key in tokens if isinstance(totals.get(key), int)]
        if "error" in usage or not totals or not valid:
            unknown_workers += 1
        if valid:
            has_totals = True
        for key in valid:
            tokens[key] += totals[key]
    end = state["finished"] and next((e["time"] for e in reversed(events(path)) if e["type"] == "run.finished"), None)
    lanes = {}
    for name in state["lane_order"]:
        lane = state["lanes"][name]
        lanes[name] = {"engine": lane["engine"], "model": lane.get("model"), "family": lane["family"],
                       "strategy": lane.get("strategy"), "branch": lane["branch"],
                       "rounds": lane_rounds(lane), "trajectory": [v["score"] for v in lane["verifications"]],
                       "passed": [v["passed"] for v in lane["verifications"]],
                       "best": lane["best"] and lane["best"]["score"], "best_commit": lane["best_commit"],
                       "critiques": len(lane["critiques"]),
                       "critic_families": sorted({c["family"] for c in lane["critiques"] if c.get("family")}),
                       "workers": [{k: w[k] for k in ("role", "worker_id", "model")} for w in lane["workers"]]}
    first = state["lanes"].get("a", {}).get("verifications") or []
    return {"run_id": run_id, "experiment": config.get("experiment"), "category": config.get("category"),
            "task_id": config["task_id"],
            "repeat": config.get("repeat"), "arm": config["arm"], "rounds_limit": config["rounds"],
            "base": state["base"] and state["base"]["score"],
            "outcome": state["finished"]["outcome"] if state["finished"] else "running",
            "winner": state["finished"].get("lane") if state["finished"] else None,
            "integrated": bool(state.get("integrated")),
            "l0_passed": bool(first and first[0]["passed"]),
            "wall_seconds": round((end or time.time()) - state["started_at"], 1),
            "tokens": tokens if has_totals else None, "tokens_unknown_workers": unknown_workers,
            "lanes": lanes, "review": state["review"], "warnings": state["warnings"],
            "trail": [f"{t['action']} {t.get('lane') or ''}: {t['reason']}" for t in trail]}


def format_tokens(summary):
    tokens, unknown = summary["tokens"], summary.get("tokens_unknown_workers", 0)
    if tokens is not None:
        return (f"tokens: in {tokens['input_tokens']}, out {tokens['output_tokens']}, "
                f"cached {tokens['cached_tokens']} ({unknown} worker(s) unknown)")
    if unknown:
        return f"tokens: unknown ({unknown} worker(s) without usage)"
    return "tokens: not collected"


def command_report(args):
    path, state = load_state(args.run)
    if state["finished"] and not any(e["type"] == "usage.collected" for e in events(path)):
        collect_usage(path, state)
    summary = summarize(args.run)
    if args.json:
        print(json.dumps(summary, indent=2))
        return 0
    print(f"Run {summary['run_id']} | task {summary['task_id']} | arm {summary['arm']} | "
          f"outcome {summary['outcome']} | winner {summary['winner'] or '-'} | {summary['wall_seconds']}s")
    print(f"Base score: {summary['base']} | {format_tokens(summary)}")
    for name, lane in summary["lanes"].items():
        print(f"  lane {name}: {lane['engine']} {lane['model'] or ''} [{lane['family']}] "
              f"rounds {lane['rounds']} trajectory {lane['trajectory']} best {lane['best']} "
              f"critiques {lane['critiques']} {lane['critic_families']}")
    for warning in summary["warnings"]:
        print(f"  warning: {warning}")
    print("Decisions:")
    for line in summary["trail"]:
        print(f"  - {line}")
    return 0


def all_runs(experiment=None):
    results = []
    for path in sorted(runs_root().iterdir()):
        if (path / "events.jsonl").is_file():
            try:
                summary = summarize(path.name)
            except (ValueError, KeyError, OSError):
                continue
            if experiment is None or summary["experiment"] == experiment:
                results.append(summary)
    return results


def command_list(args):
    for run in all_runs(args.experiment):
        print(f"{run['run_id']} {run['arm']:<3} {run['outcome']:<10} task {run['task_id']} "
              f"repeat {run['repeat']} {run['experiment'] or ''} cat {run.get('category') or '-'}")
    return 0


def _all_histories(experiment=None):
    """Event histories of every run, optionally filtered by experiment."""
    try:
        paths = sorted(runs_root().iterdir())
    except OSError:
        return []
    histories = []
    for path in paths:
        if not (path / "events.jsonl").is_file():
            continue
        try:
            history = events(path)
            state = fold(history)
        except (OSError, ValueError, KeyError):
            continue
        if experiment is not None and (state.get("config") or {}).get("experiment") != experiment:
            continue
        histories.append(history)
    return histories


def command_stats(args):
    histories = _all_histories(args.experiment)
    stats = collect_stats(histories)
    if args.json:
        print(json.dumps(stats, indent=2))
        return 0
    print("Critics:")
    print(f"  {'model':<32} {'crit':>5} {'improved':>8} {'helpful':>8}")
    for model in sorted(stats["critics"]):
        row = stats["critics"][model]
        helpful = f"{row['helpful_rate']:.3f}" if row["helpful_rate"] is not None else "-"
        print(f"  {model:<32} {row['critiques']:>5} {row['improved']:>8} {helpful:>8}")
    print("Implementers:")
    print(f"  {'implementer':<42} {'lanes':>5} {'l0':>5} {'passed':>6}")
    for key in sorted(stats["implementers"]):
        row = stats["implementers"][key]
        print(f"  {key:<42} {row['lanes']:>5} {row['l0_passed']:>5} {row['passed']:>6}")
    print("Categories:")
    print(f"  {'category':<16} {'runs':>5} {'verified':>8} {'l0':>5} {'recovered':>9}")
    for cat in sorted(stats["categories"]):
        row = stats["categories"][cat]
        print(f"  {cat:<16} {row['runs']:>5} {row['verified']:>8} {row['l0_passed']:>5} "
              f"{row['recovered']:>9}")
    return 0


def command_pick(args):
    path, state = load_state(args.run)
    config = state.get("config") or {}
    if config.get("experiment"):
        raise ValueError("pick is disabled in experiment runs: the protocol fixes models per batch")
    stats = collect_stats(_all_histories())
    role = args.role
    if role == "implementer":
        choice = implementer_choice(_engine_available("muse"), _engine_available("pi"),
                                    muse_quota_block(), _configured_engine())
        append(path, "pick.made", role="implementer", lane=None, choice=choice)
        if args.json:
            print(json.dumps(choice, indent=2))
        else:
            print(f"Implementer: {choice['engine']} {choice['model']} ({choice['reason']})")
            print(f"Next: claive-orch lane {args.run} a --engine {choice['engine']} "
                  f"--model {choice['model']} (use reasoning effort {choice['reasoning_effort']})")
        return 0
    if role == "critic":
        lane_name = args.lane or "a"
        lane = state["lanes"].get(lane_name)
        if lane is None:
            raise ValueError(f"unknown lane: {lane_name}")
        family = lane.get("family") or model_family(lane.get("model"), lane.get("engine"))
        round_number = len(lane.get("critiques") or []) + 1
        choice = pick_critic(stats["critics"], family, f"{args.run}:{lane_name}:{round_number}")
        append(path, "pick.made", role="critic", lane=lane_name, choice=choice)
        if args.json:
            print(json.dumps(choice, indent=2))
        else:
            fallbacks = ",".join(choice["fallbacks"])
            print(f"Critic: {choice['model']} ({choice['mode']})")
            command = (f"claive start --engine pi --provider opencode2api --model {choice['model']} "
                       "--reasoning-effort max --read-only --turn-timeout 900")
            if fallbacks:
                command += f" --fallback-models {fallbacks}"
            command += (f" --workspace {lane['path']} --prompt-file \"$P\" "
                        f"--label {args.run}-{lane_name}-critic")
            print(f"Next: {command}")
        return 0
    if role == "breadth":
        lane_a = state["lanes"].get("a")
        if lane_a is None:
            raise ValueError("lane a is missing")
        family = lane_a.get("family") or model_family(lane_a.get("model"), lane_a.get("engine"))
        families = {c.get("family") for c in (lane_a.get("critiques") or []) if c.get("family")}
        choice = breadth_choice(stats["implementers"], family, families)
        append(path, "pick.made", role="breadth", lane="b", choice=choice)
        if args.json:
            print(json.dumps(choice, indent=2))
        else:
            print(f"Breadth: {choice['engine']} {choice['model']}")
            print(f"Next: claive-orch lane {args.run} b --engine pi --model {choice['model']} "
                  "--strategy \"<a materially different approach>\"")
        return 0
    raise ValueError(f"unknown pick role: {role}")


def command_compare(args):
    runs = [run for run in all_runs(args.experiment) if run["outcome"] not in {"running", "invalid"}]
    arms = {}
    for run in runs:
        arms.setdefault(run["arm"], []).append(run)
    table = {}
    for arm, items in sorted(arms.items()):
        verified = sum(run["outcome"] == "verified" for run in items)
        l0_failed = [run for run in items if not run["l0_passed"]]
        recovered = sum(run["outcome"] == "verified" for run in l0_failed)
        known = [run["tokens"] for run in items if run["tokens"]]
        table[arm] = {"runs": len(items), "verified": verified,
                      "rate": round(verified / len(items), 3), "ci95": [round(x, 3) for x in wilson(verified, len(items))],
                      "recovered_after_l0_fail": f"{recovered}/{len(l0_failed)}",
                      "breadth_wins": sum(run["winner"] == "b" for run in items if run["arm"] == "D"),
                      "median_seconds": sorted(run["wall_seconds"] for run in items)[len(items) // 2],
                      "median_tokens": (sorted(t["input_tokens"] + t["output_tokens"] for t in known)[len(known) // 2]
                                        if known else None),
                      "tokens_known": f"{len(known)}/{len(items)}"}
    tasks = {}
    for run in runs:
        cell = tasks.setdefault(run["task_id"], {}).setdefault(run["arm"], [0, 0])
        cell[0] += run["outcome"] == "verified"
        cell[1] += 1
    if args.json:
        print(json.dumps({"arms": table, "tasks": tasks}, indent=2))
        return 0
    print(f"Experiment {args.experiment}: {len(runs)} finished runs")
    print("| arm | runs | verified | rate | 95% CI | recovered after L0 fail | median s | median tokens |")
    print("|---|---|---|---|---|---|---|---|")
    for arm, row in table.items():
        print(f"| {arm} | {row['runs']} | {row['verified']} | {row['rate']} | {row['ci95']} | "
              f"{row['recovered_after_l0_fail']} | {row['median_seconds']} | {row['median_tokens']} "
              f"({row['tokens_known']}) |")
    names = sorted(table)
    print("\nPer task (verified/runs):\n")
    print("| task | " + " | ".join(names) + " |")
    print("|---|" + "---|" * len(names))
    for task, cells in sorted(tasks.items()):
        print(f"| {task} | " + " | ".join(f"{cells[a][0]}/{cells[a][1]}" if a in cells else "-" for a in names) + " |")
    return 0


def command_cleanup(args):
    path, state = load_state(args.run)
    if not state["finished"] and not args.force:
        raise ValueError("run is not finished; finish it first or pass --force")
    for name in state["lane_order"]:
        lane = state["lanes"][name]
        if Path(lane["path"]).exists():
            locals_ = lane_local_paths(lane, state["config"])
            dirty = dirty_outside_local(lane["path"], locals_)
            if dirty and not args.force:
                raise ValueError(f"lane {name} has uncheckpointed changes; pass --force to discard them")
            git(state["config"]["repo"], "worktree", "remove", "--force", lane["path"])
            print(f"Removed worktree for lane {name}; branch {lane['branch']} keeps every checkpoint")
    git(state["config"]["repo"], "worktree", "prune", check=False)
    if args.branches:
        winner = (state["finished"] or {}).get("lane") if state["finished"] else None
        integrated = bool(state.get("integrated"))
        for name in state["lane_order"]:
            lane = state["lanes"][name]
            branch = lane["branch"]
            if name == winner and not integrated and not args.force:
                print(f"Kept {branch} (winner not integrated; use --force)")
                continue
            process = subprocess.run(["git", "-C", str(state["config"]["repo"]), "branch", "-D", branch],
                                     capture_output=True, text=True)
            if process.returncode == 0:
                print(f"Deleted branch {branch}")
    return 0


def flatten_paths(raw):
    flattened = []
    for group in raw or []:
        flattened.extend(group if isinstance(group, list) else [group])
    return [str(p).strip() for p in flattened if str(p).strip()]


def command_integrate(args):
    path, state = load_state(args.run)
    config = state["config"]
    if not state["finished"]:
        raise ValueError(f"run {args.run} is not finished; finish it first")
    if state.get("integrated"):
        raise ValueError(f"run {args.run} was already integrated")
    outcome = state["finished"].get("outcome")
    commit = state["finished"].get("commit")
    if not commit:
        raise ValueError(f"run {args.run} has no winning commit; not verified")
    if outcome != "verified":
        raise ValueError(f"run {args.run} outcome is {outcome}, not verified")
    repo = config["repo"]
    base = config["base"]
    include = flatten_paths(getattr(args, "paths", None))
    exclude = flatten_paths(getattr(args, "exclude", None))
    spec = list(include) + [f":(exclude){p}" for p in exclude]
    diff_extra = ["--", *spec] if spec else []

    def filtered_names():
        output = git(repo, "diff", "--no-renames", base, commit, "--name-only", *diff_extra, check=False)
        return [line.strip() for line in output.splitlines() if line.strip()]

    # Bytes, unstripped: trailing whitespace and CRLF line endings are part of the patch.
    patch = subprocess.run(["git", "-C", str(repo), "diff", "--no-renames", "--binary", base, commit,
                            *diff_extra], capture_output=True).stdout
    if not patch.strip():
        if spec:
            raise ValueError("nothing to integrate for the selected paths")
    if patch.strip():
        first = subprocess.run(["git", "-C", str(repo), "apply"], input=patch, capture_output=True)
        if first.returncode != 0:
            # --3way can fail after writing conflict markers and index stages; snapshot the
            # touched files so a failure leaves the checkout exactly as it was.
            touched = filtered_names()
            snapshot = {name: ((Path(repo) / name).read_bytes() if (Path(repo) / name).is_file() else None)
                        for name in touched}
            staged = set(git(repo, "diff", "--cached", "--name-only", "--", *touched, check=False).splitlines()
                         if touched else [])
            second = subprocess.run(["git", "-C", str(repo), "apply", "--3way"], input=patch,
                                    capture_output=True)
            if second.returncode == 0:
                names = filtered_names()
                if names:
                    subprocess.run(["git", "-C", str(repo), "reset", "-q", "--", *names],
                                   capture_output=True, text=True)
            else:
                for name, content in snapshot.items():
                    target = Path(repo) / name
                    if content is None:
                        target.unlink(missing_ok=True)
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(content)
                if touched:
                    git(repo, "reset", "-q", "--", *touched, check=False)
                if staged:
                    git(repo, "add", "--", *sorted(staged), check=False)
                output = b"".join(part or b"" for part in (second.stdout, second.stderr,
                                                            first.stdout, first.stderr)).decode(errors="replace")
                names = filtered_names()
                conflicting = [name for name in names if name and name in output]
                if not conflicting:
                    conflicting = names or ["(unknown)"]
                for name in conflicting:
                    print(f"Conflict: {name}")
                return 1
        for applied in filtered_names():
            print(f"Applied: {applied}")
    result = None
    if not args.no_verify:
        result = run_verifier(config, repo, path / "verify" / "integrate.txt")
        print(f"Verifier in {repo}: {describe(result)}")
    append(path, "run.integrated", commit=commit, result=result, paths=include, exclude=exclude)
    close_idle_reusable_workers(state)
    if result is not None and not result["passed"]:
        return 1
    return 0


def command_prune(args):
    repo = args.repo
    output = git(repo, "branch", "--format=%(refname:short)", check=False)
    branches = sorted(branch.strip() for branch in output.splitlines()
                      if branch.strip().startswith("orch/") and len(branch.strip().split("/")) == 3)
    for branch in branches:
        _, run_id, lane_name = branch.split("/")
        try:
            _, state = load_state(run_id)
        except ValueError:
            print(f"Kept {branch} (unknown run)")
            continue
        if not state["finished"]:
            print(f"Kept {branch} (run not finished)")
            continue
        lane = state["lanes"].get(lane_name)
        if lane is not None and Path(lane["path"]).exists():
            print(f"Kept {branch} (worktree exists; run cleanup)")
            continue
        winner = (state["finished"] or {}).get("lane")
        if lane_name == winner and not state.get("integrated"):
            print(f"Kept {branch} (winner not integrated)")
            continue
        if not args.apply:
            print(f"Would delete {branch}")
        else:
            process = subprocess.run(["git", "-C", str(repo), "branch", "-D", branch],
                                     capture_output=True, text=True)
            if process.returncode == 0:
                print(f"Deleted branch {branch}")
            else:
                print(f"warning: could not delete {branch}: "
                      f"{((process.stderr or process.stdout) or '').strip()[:300]}")
    return 0


def parser():
    result = argparse.ArgumentParser(prog="claive-orch", description=__doc__.splitlines()[0])
    commands = result.add_subparsers(dest="action", required=True)
    init = commands.add_parser("init", help="start a run for one task under one arm")
    init.add_argument("--repo", required=True)
    init.add_argument("--task-file", required=True)
    init.add_argument("--verify", required=True, help="shell command run in the candidate workspace")
    init.add_argument("--arm", default="B", help="A, R, R', B, B0 or D (default B)")
    init.add_argument("--rounds", type=int, default=2, help="refinement rounds D (default 2, max 3)")
    init.add_argument("--base", help="base revision (default HEAD)")
    init.add_argument("--verify-timeout", type=int, default=1800)
    init.add_argument("--verify-memory", metavar="SIZE",
                      help="resident memory cap for the verifier and its children, e.g. 2G; over it the "
                      "verifier is killed and the round scores as an error. Workers are told to run their "
                      "checks under claive-memcap SIZE")
    init.add_argument("--score-regex", help="regex with named groups passed and failed or total")
    init.add_argument("--acceptance-dir", help="held-out checks stored outside the repo, copied in at verify")
    init.add_argument("--worker-verify", help="visible check command shown to workers (default: --verify, "
                      "or none when --acceptance-dir is set, so hidden check names do not leak)")
    init.add_argument("--task-id")
    init.add_argument("--experiment")
    init.add_argument("--repeat", type=int)
    init.add_argument("--max-minutes", type=float)
    init.add_argument("--label")
    init.add_argument("--skip-base-check", action="store_true")
    init.add_argument("--allow-passing-base", action="store_true")
    init.add_argument("--post-pass-critic", action="store_true",
                      help="after a pass, run one post-pass critic review (arms B and D only)")
    init.add_argument("--category", choices=list(CATEGORIES), default=None)
    init.add_argument("--setup", help="shell command run with bash -c in each new worktree "
                      "(CLAIVE_ORCH_REPO/CLAIVE_ORCH_LANE in env; keep its files gitignored)")
    init.add_argument("--cache-key", help="shell command printing the cache key for build outputs "
                      "(with --cache-path; the verifier should skip its build when the cached outputs exist)")
    init.add_argument("--cache-path", action="append", nargs="+", metavar="PATH",
                      help="repo-relative build output to cache, repeatable "
                      "(with --cache-key; the verifier should skip its build when the cached outputs exist)")
    lane = commands.add_parser("lane", help="add a candidate lane (git worktree at the base revision)")
    lane.add_argument("run")
    lane.add_argument("name")
    lane.add_argument("--engine", help="worker engine (default: the claive config default_engine, else muse)")
    lane.add_argument("--model")
    lane.add_argument("--strategy", help="deliberate strategy instruction for this candidate")
    worker = commands.add_parser("worker", help="register a claive worker with a lane")
    worker.add_argument("run")
    worker.add_argument("lane")
    worker.add_argument("--role", required=True, choices=["implementer", "critic", "reviewer"])
    worker.add_argument("--worker-id", required=True)
    worker.add_argument("--allow-same-family", action="store_true")
    prompt = commands.add_parser("prompt", help="write the prompt file for the next step and print its path")
    prompt.add_argument("run")
    prompt.add_argument("kind", choices=["implement", "critique", "correct", "review"])
    prompt.add_argument("lane", nargs="?")
    prompt.add_argument("--out")
    prompt.add_argument("--force", action="store_true", help="write a prompt the arbiter did not ask for (recorded)")
    verify = commands.add_parser("verify", help="run the verifier; checkpoint or revert")
    verify.add_argument("run")
    verify.add_argument("lane")
    verify.add_argument("--force", action="store_true", help="verify outside the expected sequence")
    critique = commands.add_parser("critique", help="record a critic's answer")
    critique.add_argument("run")
    critique.add_argument("lane")
    source = critique.add_mutually_exclusive_group(required=True)
    source.add_argument("--worker-id")
    source.add_argument("--file")
    review = commands.add_parser("review", help="record the tie-break review")
    review.add_argument("run")
    source = review.add_mutually_exclusive_group(required=True)
    source.add_argument("--worker-id")
    source.add_argument("--prefer", choices=["a", "b", "none"])
    review.add_argument("--reason")
    rescore = commands.add_parser("rescore", help="re-verify lanes after fixing the verifier or held-out "
                                  "checks without using a round")
    rescore.add_argument("run")
    rescore.add_argument("--reason", required=True, help="why the lanes are rescored (required)")
    reject = commands.add_parser("reject", help="override critic suggestions before the correction prompt")
    reject.add_argument("run")
    reject.add_argument("lane")
    reject.add_argument("--defect", action="append", nargs="+", metavar="N", required=True,
                        help="1-based defect number to reject (repeatable)")
    reject.add_argument("--reason", required=True, help="why the suggestions are rejected (required)")
    for name, text in (("next", "print the single legal next action"), ("report", "summarize one run")):
        command = commands.add_parser(name, help=text)
        command.add_argument("run")
        command.add_argument("--json", action="store_true")
    done = commands.add_parser("finish", help="record the arbiter's finish decision")
    done.add_argument("run")
    done.add_argument("--abort", action="store_true")
    done.add_argument("--reason")
    usage = commands.add_parser("usage", help="snapshot provider token usage of the run's workers")
    usage.add_argument("run")
    usage.add_argument("--json", action="store_true")
    listing = commands.add_parser("list", help="list runs")
    listing.add_argument("--experiment")
    stats = commands.add_parser("stats", help="aggregate critic, implementer and category stats")
    stats.add_argument("--experiment")
    stats.add_argument("--json", action="store_true")
    pick = commands.add_parser("pick", help="pick the next worker from collected stats")
    pick.add_argument("run")
    pick.add_argument("role", choices=["implementer", "critic", "breadth"])
    pick.add_argument("--lane")
    pick.add_argument("--json", action="store_true")
    compare = commands.add_parser("compare", help="per-arm results for an experiment")
    compare.add_argument("--experiment", required=True)
    compare.add_argument("--json", action="store_true")
    cleanup = commands.add_parser("cleanup", help="remove lane worktrees; add --branches to delete lane branches")
    cleanup.add_argument("run")
    cleanup.add_argument("--force", action="store_true")
    cleanup.add_argument("--branches", action="store_true",
                         help="also delete the run's lane branches (winner kept unless integrated or --force)")
    integrate = commands.add_parser("integrate",
                                    help="apply the winning commit to the repo checkout without staging")
    integrate.add_argument("run")
    integrate.add_argument("--no-verify", action="store_true",
                           help="skip running the verifier in the repo checkout")
    integrate.add_argument("--paths", action="append", nargs="+", metavar="PATH",
                           help="limit the applied diff to these repo-relative pathspecs")
    integrate.add_argument("--exclude", action="append", nargs="+", metavar="PATH",
                           help="drop these repo-relative paths from the applied diff (:(exclude)P)")
    prune = commands.add_parser("prune", help="list or delete stale orch/<run>/<lane> branches")
    prune.add_argument("--repo", required=True, help="repository whose local orch branches are inspected")
    prune.add_argument("--apply", action="store_true", help="delete deletable branches (default is a dry run)")
    commands.add_parser("arms", help="list experiment arms")
    return result


def main():
    args = parser().parse_args()
    handlers = {"init": command_init, "lane": command_lane, "worker": command_worker,
                "prompt": command_prompt, "verify": command_verify, "critique": command_critique,
                "review": command_review, "rescore": command_rescore, "reject": command_reject,
                "next": command_next, "finish": command_finish,
                "usage": command_usage, "report": command_report, "list": command_list,
                "stats": command_stats, "pick": command_pick,
                "compare": command_compare, "cleanup": command_cleanup, "integrate": command_integrate,
                "prune": command_prune}
    if args.action == "arms":
        for name, text in ARMS.items():
            print(f"{name:<3} {text}")
        return 0
    return handlers[args.action](args)


def entrypoint():
    try:
        return main()
    except ValueError as error:
        print(f"claive-orch: {error}", file=sys.stderr)
        return 2
