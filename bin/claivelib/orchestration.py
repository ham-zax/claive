"""Deterministic arbiter for verifier-gated refinement runs (claive-orch). No dependencies.

Models propose; this code decides. A frontier parent (Claude Code or Codex) launches
workers through claive and reports their results here. This module owns the run
history, verification, checkpoints, budgets, and the single legal next action.
See docs/experiment/06-skill-driven-implementation.md.
"""

import argparse
import fcntl
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid

from claivelib import cli as workers
from claivelib.engines import DEFAULT_ENGINE, get_engine

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
GIT_IDENTITY = {"GIT_AUTHOR_NAME": "claive-orch", "GIT_AUTHOR_EMAIL": "claive-orch@localhost",
                "GIT_COMMITTER_NAME": "claive-orch", "GIT_COMMITTER_EMAIL": "claive-orch@localhost"}
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
    # pytest summary: "=== 1 failed, 4 passed, 1 error in 0.1s ==="
    ("pytest", re.compile(r"=+ (.*?(?:passed|failed|error|errors).*?) in [\d.]+s")),
]


def parse_score(output, custom=None):
    """Return (passed, total) from test-runner output, or None when unrecognised."""
    if custom:
        match = None
        for match in re.finditer(custom, output, re.M):
            pass
        if match:
            groups = match.groupdict()
            passed = int(groups["passed"]) if groups.get("passed") else 0
            if groups.get("total"):
                return passed, int(groups["total"])
            return passed, passed + int(groups.get("failed") or 0)
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
             "finished": None, "usage": {}, "warnings": [], "started_at": None}
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
            lane["critiques"].append(data)
        elif kind == "review.completed":
            state["review"] = data
        elif kind == "usage.collected":
            state["usage"] = data["workers"]
        elif kind == "warning":
            state["warnings"].append(data["message"])
        elif kind == "run.finished":
            state["finished"] = data
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
    if forced:
        outcome = forced
    elif chosen["best"]["passed"]:
        outcome = "verified"
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


def run_verifier(config, workspace, output_file):
    """Run the configured verifier in a workspace; return a VerificationResult dict."""
    created = []
    started = time.time()
    try:
        if config.get("acceptance_dir"):
            created = copy_acceptance(config["acceptance_dir"], workspace)
        process = subprocess.run(["bash", "-c", config["verify"]], cwd=workspace, capture_output=True,
                                 text=True, timeout=config["verify_timeout"], stdin=subprocess.DEVNULL)
        output, code, status = process.stdout + process.stderr, process.returncode, None
    except subprocess.TimeoutExpired as error:
        output = (error.stdout or "") if isinstance(error.stdout, str) else ""
        code, status = None, "error"
        output += f"\n[claive-orch: verifier timed out after {config['verify_timeout']}s]"
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


def lane_diff(state, lane):
    workspace = lane["path"]
    git(workspace, "add", "-A", "--intent-to-add", check=False)
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
        return (f"# Critique a candidate solution\n\nYou are a critic. You do not edit files. "
                f"Find concrete defects that explain why verification fails.\n\n## Task\n\n{task}\n\n"
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
        if critique and critique[-1]["defects"]:
            defects = "\n".join(f"{i}. [{d['location']}] {d['description']} (evidence: {d['evidence']})"
                                for i, d in enumerate(critique[-1]["defects"], 1))
            body = f"An independent critic found these defects:\n\n{defects}\n"
        else:
            body = "Use the verifier output below to find and fix the failure.\n"
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
    if args.acceptance_dir:
        acceptance = Path(args.acceptance_dir)
        if not acceptance.is_absolute() or not acceptance.is_dir():
            raise ValueError("--acceptance-dir must be an existing absolute directory")
        if acceptance.resolve().is_relative_to(repo.resolve()):
            raise ValueError("--acceptance-dir must be outside the repository so workers cannot see it")
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
              "label": args.label or task.stem}
    append(path, "run.started", config=config)
    print(f"Run {run_id} | arm {args.arm} | base {base[:12]} | {path}")
    if args.skip_base_check:
        append(path, "warning", message="base verification skipped; the verifier is not known to fail at base")
    else:
        base_dir = path / "base"
        git(repo, "worktree", "add", "--detach", str(base_dir), base)
        try:
            result = run_verifier(config, base_dir, path / "verify" / "base.txt")
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
    append(path, "lane.added", lane=name, engine=args.engine, model=args.model, family=family,
           strategy=args.strategy, branch=branch, path=str(workspace), base_commit=config["base"])
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
    elif family == lane["family"]:
        if not args.allow_same_family:
            raise ValueError(f"{args.role} family {family} matches the implementer's; choose another model "
                             "family or pass --allow-same-family (recorded)")
        append(path, "warning", message=f"{args.role} {args.worker_id} shares family {family} with lane {args.lane}")
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
    result = run_verifier(config, lane["path"], path / "verify" / f"{args.lane}-r{round_number}.txt")
    result["round"] = round_number
    append(path, "verification.completed", lane=args.lane, result=result)
    best = lane["best"] or state["base"]
    refining = config["arm"] in REFINING
    if refining and best is not None and verification_key(result) < verification_key(best):
        git(lane["path"], "reset", "-q", "--hard", lane["best_commit"])
        git(lane["path"], "clean", "-fdq")
        append(path, "checkpoint.reverted", lane=args.lane, round=round_number, result=result,
               to_commit=lane["best_commit"], best=best)
        print(f"Lane {args.lane} round {round_number}: {describe(result)} is worse than {describe(best)}; "
              f"reverted to {lane['best_commit'][:12]}")
    else:
        git(lane["path"], "add", "-A")
        git(lane["path"], "commit", "-q", "--no-verify", "--allow-empty", "-m",
            f"claive-orch {config['run_id']} lane {args.lane} round {round_number}: {describe(result)}",
            env=GIT_IDENTITY)
        commit = git(lane["path"], "rev-parse", "HEAD")
        improved = best is None or verification_key(result) > verification_key(best)
        stat = git(lane["path"], "diff", "--shortstat", lane["base_commit"], commit, check=False)
        lines = sum(int(n) for n in re.findall(r"(\d+) (?:insertion|deletion)", stat))
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
    model = family = None
    if args.worker_id:
        _, record = worker_record(args.worker_id)
        model = record.get("model") or (record.get("launch") or {}).get("model")
        family = model_family(model, record.get("engine"))
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
                    model=model, family=family)
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
    "finish": ("claive-orch finish {run}; inspect `git -C {repo} diff {base} {commit}`; integrate only "
               "if appropriate; claive-orch usage {run}; close workers; claive-orch cleanup {run}"),
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


def command_usage(args):
    path, state = load_state(args.run)
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
    print(json.dumps(collected, indent=2) if args.json else
          f"Collected usage for {len(collected)} worker(s); see claive-orch report {args.run}")
    return 0


def summarize(run_id):
    path, state = load_state(run_id)
    config = state["config"]
    trail = [e["data"] for e in events(path) if e["type"] == "arbiter.action"]
    tokens = {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0}
    unknown = False
    for usage in state["usage"].values():
        totals = usage.get("totals") or {}
        if "error" in usage or not totals:
            unknown = True
        for key in tokens:
            if isinstance(totals.get(key), int):
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
    return {"run_id": run_id, "experiment": config.get("experiment"), "task_id": config["task_id"],
            "repeat": config.get("repeat"), "arm": config["arm"], "rounds_limit": config["rounds"],
            "base": state["base"] and state["base"]["score"],
            "outcome": state["finished"]["outcome"] if state["finished"] else "running",
            "winner": state["finished"].get("lane") if state["finished"] else None,
            "l0_passed": bool(first and first[0]["passed"]),
            "wall_seconds": round((end or time.time()) - state["started_at"], 1),
            "tokens": None if unknown or not state["usage"] else tokens,
            "lanes": lanes, "review": state["review"], "warnings": state["warnings"],
            "trail": [f"{t['action']} {t.get('lane') or ''}: {t['reason']}" for t in trail]}


def command_report(args):
    summary = summarize(args.run)
    if args.json:
        print(json.dumps(summary, indent=2))
        return 0
    print(f"Run {summary['run_id']} | task {summary['task_id']} | arm {summary['arm']} | "
          f"outcome {summary['outcome']} | winner {summary['winner'] or '-'} | {summary['wall_seconds']}s")
    print(f"Base score: {summary['base']} | tokens: {summary['tokens'] or 'not collected/unknown'}")
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
              f"repeat {run['repeat']} {run['experiment'] or ''}")
    return 0


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
            dirty = git(lane["path"], "status", "--porcelain", check=False)
            if dirty and not args.force:
                raise ValueError(f"lane {name} has uncheckpointed changes; pass --force to discard them")
            git(state["config"]["repo"], "worktree", "remove", "--force", lane["path"])
            print(f"Removed worktree for lane {name}; branch {lane['branch']} keeps every checkpoint")
    git(state["config"]["repo"], "worktree", "prune", check=False)
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
    lane = commands.add_parser("lane", help="add a candidate lane (git worktree at the base revision)")
    lane.add_argument("run")
    lane.add_argument("name")
    lane.add_argument("--engine", default=DEFAULT_ENGINE)
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
    compare = commands.add_parser("compare", help="per-arm results for an experiment")
    compare.add_argument("--experiment", required=True)
    compare.add_argument("--json", action="store_true")
    cleanup = commands.add_parser("cleanup", help="remove lane worktrees; branches keep checkpoints")
    cleanup.add_argument("run")
    cleanup.add_argument("--force", action="store_true")
    commands.add_parser("arms", help="list experiment arms")
    return result


def main():
    args = parser().parse_args()
    handlers = {"init": command_init, "lane": command_lane, "worker": command_worker,
                "prompt": command_prompt, "verify": command_verify, "critique": command_critique,
                "review": command_review, "next": command_next, "finish": command_finish,
                "usage": command_usage, "report": command_report, "list": command_list,
                "compare": command_compare, "cleanup": command_cleanup}
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
