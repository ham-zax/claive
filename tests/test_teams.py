"""First-prompt layers, custom roles, and configured teams."""
import hermetic  # noqa: F401  (must run before claivelib reads the environment)
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
import unittest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "bin"))
from claivelib import cli

FIXTURE_CLI = str(REPO / "tests/fixture-workers")
FIXTURE_WORKER = str(REPO / "tests/fixture_worker.py")


class TeamChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="team-check-")
        self.root = Path(self.temp.name)
        self.registry = self.root / "registry"
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.prompt = self.root / "task.md"
        self.prompt.write_text("task body\n", encoding="utf-8")
        self.config = self.root / "config.json"
        self.env = dict(os.environ, CLAIVE_DIR=str(self.registry), CLAIVE_CONFIG=str(self.config),
                        TERM="dumb", FIXTURE_WORKER_BINARY=FIXTURE_WORKER, FIXTURE_MODE="success")
        self.processes = []

    def tearDown(self):
        if self.registry.exists():
            for state_file in self.registry.glob("*/state.json"):
                try:
                    state = json.loads(state_file.read_text())
                except (OSError, ValueError):
                    continue
                if state.get("reusable") and state.get("status") not in {"completed", "failed", "cancelled"}:
                    subprocess.run([FIXTURE_CLI, "close", state["id"]], env=self.env,
                                   capture_output=True, timeout=10)
                pid = state.get("supervisor_pid")
                if pid and cli.identity(pid) == state.get("supervisor_identity"):
                    try:
                        os.killpg(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
        for process in self.processes:
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)
        self.temp.cleanup()

    def command(self, *args, timeout=20):
        return subprocess.run([FIXTURE_CLI, *map(str, args)], env=self.env, text=True,
                              capture_output=True, timeout=timeout)

    def launch_args(self, *extra, action="run"):
        return (action, "--engine", "fixture", "--workspace", str(self.workspace),
                "--prompt-file", str(self.prompt), *map(str, extra))

    def launch(self, *extra, action="run"):
        result = self.command(*self.launch_args(*extra, action=action))
        if result.returncode:
            return result, None
        match = re.search(r"Worker ([0-9a-f]{12})", result.stdout)
        self.assertIsNotNone(match, result.stdout + result.stderr)
        state = json.loads((self.registry / match.group(1) / "state.json").read_text())
        return result, state

    def refuse(self, args, message):
        before = set(self.registry.glob("*/state.json")) if self.registry.exists() else set()
        result = self.command(*args)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(message, result.stderr)
        after = set(self.registry.glob("*/state.json")) if self.registry.exists() else set()
        self.assertEqual(after, before)
        return result

    def write_config(self, data):
        self.config.write_text(json.dumps(data), encoding="utf-8")

    def team_config(self, **team_options):
        self.write_config({
            "roles": {"worker": {"engine": "fixture", "read_only": True,
                                  "preamble": "ROLE LAYER"}},
            "teams": {"lop": {"members": {"builder": "worker", "reviewer": "reviewer"},
                                **team_options}},
        })

    def test_context_layers_state_and_all_refusals(self):
        first = self.root / "first-context.md"
        second = self.root / "second-context.md"
        first.write_bytes("first café\n".encode("utf-8"))
        second.write_text("second layer", encoding="utf-8")

        result, state = self.launch("--context-file", first, "--context-file", second)
        self.assertEqual(result.returncode, 0, result.stderr)
        prompt = Path(state["prompt_file"]).read_text(encoding="utf-8")
        positions = [prompt.index(text) for text in
                     ("[context: first-context.md]", "first café", "[context: second-context.md]",
                      "second layer", "task body")]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(state["context_files"], [
            dict(path=str(first.resolve()), sha256=hashlib.sha256(first.read_bytes()).hexdigest(),
                 bytes=len(first.read_bytes())),
            dict(path=str(second.resolve()), sha256=hashlib.sha256(second.read_bytes()).hexdigest(),
                 bytes=len(second.read_bytes())),
        ])

        empty = self.root / "empty.md"
        empty.write_bytes(b"")
        invalid = self.root / "invalid.md"
        invalid.write_bytes(b"\xff")
        large = self.root / "large.md"
        large.write_bytes(b"x" * (256 * 1024 + 1))
        relative = Path("relative-context.md")
        for path in (relative, self.root / "missing.md", self.workspace, empty):
            with self.subTest(path=path):
                self.refuse(self.launch_args("--context-file", path),
                            f"--context-file must be an existing nonempty absolute file: {path}")
        self.refuse(self.launch_args("--context-file", invalid), f"--context-file {invalid} is not UTF-8 text")
        self.refuse(self.launch_args("--context-file", large), f"--context-file {large} is larger than 256 KiB")
        self.refuse(self.launch_args("--context-file", first, "--context-file", first),
                    f"--context-file {first} is given twice")
        link = self.root / "first-context-link.md"
        link.symlink_to(first)
        self.refuse(self.launch_args("--context-file", first, "--context-file", link),
                    f"--context-file {link} is given twice")
        files = [self.root / f"context-{index}.md" for index in range(11)]
        for path in files:
            path.write_text("context", encoding="utf-8")
        options = [item for path in files for item in ("--context-file", str(path))]
        self.refuse(self.launch_args(*options), "at most 10 --context-file values")

    def test_no_extra_layers_preserves_prompt_file(self):
        result, state = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(state["prompt_file"], str(self.prompt))
        self.assertFalse((self.registry / state["id"] / "prompt-0001.md").exists())
        self.assertEqual(state["context_files"], [])

    def _open_worker(self, *extra):
        before = set(self.registry.glob("*/state.json")) if self.registry.exists() else set()
        process = subprocess.Popen([FIXTURE_CLI, *self.launch_args(*extra, action="open")],
                                   env=self.env, text=True, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, start_new_session=True)
        self.processes.append(process)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            new = set(self.registry.glob("*/state.json")) - before
            if new:
                state_file = next(iter(new))
                state = json.loads(state_file.read_text())
                if state.get("status") == "idle":
                    return process, state
            if process.poll() is not None:
                self.fail(f"fixture open exited: {process.communicate()}")
            time.sleep(0.025)
        self.fail("fixture reusable worker did not become idle")

    def test_fork_uses_its_own_context_layers(self):
        source_context = self.root / "source-context.md"
        fork_context = self.root / "fork-context.md"
        source_context.write_text("source only", encoding="utf-8")
        fork_context.write_text("fork only", encoding="utf-8")
        _process, source = self._open_worker("--context-file", source_context)

        result = self.command("fork", source["id"], "--prompt", "fork task",
                              "--context-file", fork_context)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        match = re.search(r"Worker ([0-9a-f]{12})", result.stdout)
        self.assertIsNotNone(match, result.stdout)
        state = json.loads((self.registry / match.group(1) / "state.json").read_text())
        self.assertEqual(state["context_files"], [
            dict(path=str(fork_context.resolve()), sha256=hashlib.sha256(fork_context.read_bytes()).hexdigest(),
                 bytes=len(fork_context.read_bytes()))])
        session = Path(state["launch"]["session_dir"]) / "session.json"
        deadline = time.monotonic() + 15
        messages = []
        while time.monotonic() < deadline:
            state = json.loads((self.registry / match.group(1) / "state.json").read_text())
            if session.is_file():
                messages = json.loads(session.read_text())["messages"]
                if state.get("status") == "idle" and "[context: fork-context.md]" in messages[-1]:
                    break
            time.sleep(0.025)
        self.assertTrue(messages, f"fork session was not written: {state}")
        self.assertIn("[context: fork-context.md]", messages[-1])
        self.assertIn("fork only", messages[-1])
        self.assertNotIn("source-context.md", messages[-1])

    def test_custom_role_prompt_file_is_dynamic_and_config_errors_are_refused(self):
        role_prompt = self.root / "role.md"
        role_prompt.write_text("CUSTOM ROLE ONE", encoding="utf-8")
        self.write_config({"roles": {"builder": {"engine": "fixture", "read_only": True,
                                                   "prompt_file": str(role_prompt)}},
                           "engines": {"fixture": {"default_model": "policy-model",
                                                     "default_reasoning_effort": "low"}}})
        result, first = self.launch("--role", "builder")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(first["engine"], "fixture")
        self.assertTrue(first["launch"]["read_only"])
        self.assertEqual(first["role"], "builder")
        self.assertEqual((first["model"], first["reasoning_effort"], first["max_model_steps"]),
                         ("policy-model", "low", None))
        first_prompt = Path(first["prompt_file"]).read_text(encoding="utf-8")
        self.assertIn("CUSTOM ROLE ONE", first_prompt)

        role_prompt.write_text("CUSTOM ROLE TWO", encoding="utf-8")
        _result, second = self.launch("--role", "builder")
        self.assertIn("CUSTOM ROLE TWO", Path(second["prompt_file"]).read_text(encoding="utf-8"))

        cases = (
            ({"roles": {"BadName": {}}}, "role name BadName is invalid"),
            ({"roles": {"builder": {"engine": "fixture", "read_only": True,
                                     "preamble": "inline", "prompt_file": str(role_prompt)}}},
             "roles.builder: use preamble or prompt_file, not both"),
            ({"roles": {"builder": {}}},
             "roles.builder is a new role and needs engine, read_only and preamble or prompt_file"),
            ({"roles": {"builder": {"engine": "fixture", "read_only": True,
                                     "prompt_file": "relative.md"}}},
             "roles.builder.prompt_file must be an absolute path"),
        )
        for data, message in cases:
            with self.subTest(message=message):
                self.write_config(data)
                self.refuse(self.launch_args("--role", "builder"), message)

        missing = self.root / "missing-role.md"
        self.write_config({"roles": {"builder": {"engine": "fixture", "read_only": True,
                                                   "prompt_file": str(missing)}}})
        self.refuse(self.launch_args("--role", "builder"),
                    f"role builder: prompt_file {missing} is missing, empty, not UTF-8 or larger than 256 KiB")

    def test_builtin_prompt_file_replaces_its_preamble(self):
        role_prompt = self.root / "reviewer.md"
        role_prompt.write_text("REPLACEMENT REVIEWER PROMPT", encoding="utf-8")
        self.write_config({"roles": {"reviewer": {"engine": "fixture", "read_only": True,
                                                    "prompt_file": str(role_prompt)}}})
        result, state = self.launch("--role", "reviewer")
        self.assertEqual(result.returncode, 0, result.stderr)
        prompt = Path(state["prompt_file"]).read_text(encoding="utf-8")
        self.assertIn("REPLACEMENT REVIEWER PROMPT", prompt)
        self.assertNotIn("You are a reviewer", prompt)

    def test_team_launch_prompt_order_alias_and_project_override(self):
        collaboration = self.root / "collaboration.md"
        project = self.root / "project.md"
        replacement = self.root / "replacement.md"
        context = self.root / "context.md"
        collaboration.write_text("COLLAB TEXT", encoding="utf-8")
        project.write_text("CONFIGURED PROJECT", encoding="utf-8")
        replacement.write_text("OVERRIDE PROJECT", encoding="utf-8")
        context.write_text("CONTEXT TEXT", encoding="utf-8")
        self.team_config(collaboration=str(collaboration), project=str(project))

        result, state = self.launch("--team", "lop", "--member", "builder",
                                    "--project-file", replacement, "--context-file", context)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(state["team"], {"name": "lop", "member": "builder", "role": "worker"})
        self.assertEqual(state["role"], "worker")
        self.assertEqual(state["engine"], "fixture")
        self.assertTrue(state["launch"]["read_only"])
        self.assertIn("@lop-builder -> " + state["id"], result.stderr)
        aliases = json.loads((self.registry / "aliases.json").read_text())
        self.assertEqual(aliases["lop-builder"], state["id"])

        prompt = Path(state["prompt_file"]).read_text(encoding="utf-8")
        ordered = ["ROLE LAYER", "[team: lop]", "You are member builder (role worker), alias @lop-builder.",
                   "- reviewer (role reviewer): @lop-reviewer", "[team collaboration: collaboration.md]",
                   "COLLAB TEXT", "[project: replacement.md]", "OVERRIDE PROJECT",
                   "[context: context.md]", "CONTEXT TEXT", "task body", "claive-report"]
        positions = [prompt.index(text) for text in ordered]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn("CONFIGURED PROJECT", prompt)

    def test_team_launch_refusals_and_ended_alias_replacement(self):
        self.team_config()
        cases = (
            (("--team", "lop"), "--team and --member go together"),
            (("--member", "builder"), "--team and --member go together"),
            (("--team", "lop", "--member", "builder", "--role", "worker"),
             "--team sets the role from the member; do not pass --role"),
            (("--team", "missing", "--member", "builder"), "unknown team: missing"),
            (("--team", "lop", "--member", "missing"), "team lop has no member missing; members: builder, reviewer"),
            (("--project-file", str(self.prompt)), "--project-file needs --team"),
        )
        for options, message in cases:
            with self.subTest(message=message):
                self.refuse(self.launch_args(*options), message)

        missing_collaboration = self.root / "missing-collaboration.md"
        self.team_config(collaboration=str(missing_collaboration))
        self.refuse(self.launch_args("--team", "lop", "--member", "builder"),
                    f"team lop collaboration must be an existing nonempty absolute file: {missing_collaboration}")
        missing_project = self.root / "missing-project.md"
        self.team_config(project=str(missing_project))
        self.refuse(self.launch_args("--team", "lop", "--member", "builder"),
                    f"team lop project must be an existing nonempty absolute file: {missing_project}")
        self.team_config()

        self.registry.mkdir(parents=True, exist_ok=True)
        active_id = "111111111111"
        (self.registry / active_id).mkdir()
        (self.registry / active_id / "state.json").write_text(json.dumps({"status": "running"}))
        (self.registry / "aliases.json").write_text(json.dumps({"lop-builder": active_id}))
        self.refuse(self.launch_args("--team", "lop", "--member", "builder"),
                    f"@lop-builder is bound to active worker {active_id}; close it or run claive alias remove lop-builder")

        ended_id = "222222222222"
        (self.registry / ended_id).mkdir()
        (self.registry / ended_id / "state.json").write_text(json.dumps({"status": "completed"}))
        (self.registry / "aliases.json").write_text(json.dumps({"lop-builder": ended_id}))
        result, state = self.launch("--team", "lop", "--member", "builder")
        self.assertEqual(result.returncode, 0, result.stderr)
        rebound = json.loads((self.registry / "aliases.json").read_text())
        self.assertEqual(rebound["lop-builder"], state["id"])
        self.assertNotEqual(rebound["lop-builder"], ended_id)

    def test_team_config_validation_list_and_read_only_show(self):
        collaboration = self.root / "collaboration.md"
        project = self.root / "project.md"
        collaboration.write_text("collab", encoding="utf-8")
        project.write_text("project", encoding="utf-8")
        good = {"teams": {"lop": {"collaboration": str(collaboration), "project": str(project),
                                     "members": {"builder": "worker", "reviewer": "reviewer"}}}}
        invalid = (
            ({"teams": {"Bad": {"members": {"builder": "worker"}}}}, "teams.Bad"),
            ({"teams": {"lop": {"unknown": 1, "members": {"builder": "worker"}}}}, "teams.lop"),
            ({"teams": {"lop": {"members": {}}}}, "teams.lop.members"),
            ({"teams": {"lop": {"project": "relative.md", "members": {"builder": "worker"}}}},
             "teams.lop.project"),
            ({"teams": {"lop": {"members": {"Builder": "worker"}}}}, "teams.lop.members.Builder"),
            ({"teams": {"lop": {"members": {"builder": "unknown"}}}}, "teams.lop.members.builder: unknown role"),
            ({"teams": {"a" * 32: {"members": {"b" * 32: "worker"}}}}, "alias"),
        )
        for data, message in invalid:
            with self.subTest(message=message):
                self.write_config(data)
                result = self.command("team", "list")
                self.assertEqual(result.returncode, 1)
                self.assertIn("invalid claive config", result.stderr)
                self.assertIn(message, result.stderr)

        self.write_config(good)
        no_registry_show = self.command("team", "show", "lop")
        self.assertEqual(no_registry_show.returncode, 0, no_registry_show.stderr)
        self.assertFalse(self.registry.exists(), "team show must not create a registry directory")
        self.registry.mkdir(parents=True, exist_ok=True)
        worker_id = "333333333333"
        (self.registry / worker_id).mkdir()
        (self.registry / worker_id / "state.json").write_text(json.dumps({"status": "idle"}))
        (self.registry / "aliases.json").write_text(json.dumps({"lop-builder": worker_id}))
        before = {path.relative_to(self.registry): path.read_bytes()
                  for path in self.registry.rglob("*") if path.is_file()}

        human_list = self.command("team", "list")
        self.assertEqual(human_list.returncode, 0, human_list.stderr)
        self.assertIn("lop | members: 2 | collaboration: " + str(collaboration), human_list.stdout)
        json_list = self.command("team", "list", "--json")
        self.assertEqual(json.loads(json_list.stdout), [
            {"name": "lop", "member_count": 2, "collaboration": str(collaboration)}])
        human_show = self.command("team", "show", "lop")
        self.assertIn("builder | role worker | @lop-builder | worker 333333333333 (idle)", human_show.stdout)
        json_show = self.command("team", "show", "lop", "--json")
        shown = json.loads(json_show.stdout)
        self.assertEqual((shown["collaboration"], shown["project"]), (str(collaboration), str(project)))
        self.assertEqual(shown["members"][0], {"name": "builder", "role": "worker", "alias": "@lop-builder",
                                                "worker_id": worker_id, "status": "idle"})
        self.assertEqual(shown["members"][1]["status"], "none")
        after = {path.relative_to(self.registry): path.read_bytes()
                 for path in self.registry.rglob("*") if path.is_file()}
        self.assertEqual(after, before)

    def test_goal_add_refuses_unknown_role_at_add_time(self):
        result = self.command("goal", "add", "--title", "bad role", "--prompt-file", self.prompt,
                              "--workspace", self.workspace, "--role", "unconfigured")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("unknown role: unconfigured", result.stderr)
        self.assertFalse((self.registry / "goals").exists())


if __name__ == "__main__":
    unittest.main()
