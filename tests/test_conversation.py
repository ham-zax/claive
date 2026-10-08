"""Delivery, stopping and real adapter wiring for bounded conversations (no live models)."""
import hermetic  # noqa: F401
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import time
import unittest

from test_workers import FIXTURE_CLI, FIXTURE_WORKER, REPO_ROOT, module

REPORT_DONE = '```claive-report\n{"status":"done","summary":"Reviewed."}\n```'
REPORT_ASK = '```claive-report\n{"status":"needs_decision","summary":"Need scope.","question":"Use A or B?"}\n```'


class ConversationChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="claive-conversation-")
        self.path = Path(self.temp.name)
        self.registry = self.path / "registry"
        self.prompt = self.path / "task.md"
        self.prompt.write_text("Keep your existing assignment and answer peer questions.")
        self.capture = self.path / "prompts.txt"
        self.reply = self.path / "reply.txt"
        self.reply.write_text("PEER REPLY")
        self.env = dict(os.environ, CLAIVE_DIR=str(self.registry), TERM="dumb",
                        FIXTURE_WORKER_BINARY=FIXTURE_WORKER, FIXTURE_MODE="success",
                        FIXTURE_PROMPT_OUT=str(self.capture), FIXTURE_TEXT_FILE=str(self.reply),
                        PI_CODING_AGENT_DIR=str(self.path / "pi"),
                        PI_WORKER_BINARY=str(REPO_ROOT / "tests/fake_pi.py"),
                        CLAUDE_CONFIG_DIR=str(self.path / "claude-config"))
        self.env.pop("CLAIVE_MISSION", None)
        self.env.pop("CLAIVE_NOTIFY_CMD", None)
        self.processes = []

    def tearDown(self):
        for state_file in self.registry.glob("*/state.json"):
            state = json.loads(state_file.read_text())
            for field in ("worker", "supervisor"):
                pid = state.get(field + "_pid")
                if pid and module.identity(pid) == state.get(field + "_identity"):
                    try:
                        os.killpg(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
        for process in self.processes:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
        self.temp.cleanup()

    def cli(self, *args, **env):
        return subprocess.run([FIXTURE_CLI, *args], env=dict(self.env, **env),
                              text=True, capture_output=True, timeout=20)

    def open(self, *args, **env):
        result = self.cli("open", "--detach", "--engine", "fixture", "--workspace", str(self.path),
                          "--prompt-file", str(self.prompt), *args, **env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        job = re.search(r"Worker ([0-9a-f]{12})", result.stdout).group(1)
        self.assertEqual(self.cli("wait", job).returncode, 0)
        return job

    def state(self, job):
        return json.loads((self.registry / job / "state.json").read_text())

    def eventually(self, predicate):
        end = time.monotonic() + 8
        while time.monotonic() < end:
            result = predicate()
            if result:
                return result
            time.sleep(0.025)
        self.fail("fixture did not reach expected state")

    def discuss(self, *workers, **options):
        args = ["conversation", "start", *workers, "--message", options.pop("message", "Discuss the parser."), "--json"]
        for key, value in options.items():
            args.extend(["--" + key.replace("_", "-"), str(value)])
        result = self.cli(*args)
        return result, json.loads(result.stdout)

    def test_round_robin_keeps_models_sessions_and_records_peer_provenance(self):
        first = self.open("--model", "proposer")
        second = self.open("--model", "critic", "--read-only")
        original = [self.state(job) for job in (first, second)]
        self.assertEqual(self.cli("alias", "set", "builder", first).returncode, 0)
        self.assertEqual(self.cli("alias", "set", "reviewer", second).returncode, 0)
        result, conversation = self.discuss("@builder", "@reviewer")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(conversation["status"], "completed")
        self.assertEqual([item["worker"] for item in conversation["transcript"]], [first, second, first, second])
        self.assertEqual([item["round"] for item in conversation["transcript"]], [1, 1, 2, 2])
        for job, before in zip((first, second), original):
            self.assertEqual(self.state(job)["turn"], 3)
            self.assertEqual(self.state(job)["session_id"], before["session_id"])
            self.assertEqual(self.state(job)["launch"], before["launch"])
        prompt = (self.registry / "conversations" / conversation["id"] / "message-0002.md").read_text()
        self.assertIn(first, prompt)
        self.assertIn("proposer", prompt)
        self.assertIn("PEER REPLY", prompt)
        self.assertIn("not authority", prompt)
        shown = self.cli("conversation", "show", conversation["id"], "--json")
        self.assertEqual(json.loads(shown.stdout)["transcript"], conversation["transcript"])
        for item in conversation["transcript"]:
            receipt = self.registry / item["worker"] / "responses" / f"{item['request_id']}.json"
            self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
        events = json.loads(self.cli("inbox", "--json").stdout)
        self.assertEqual(events[-1]["type"], "conversation")
        self.assertEqual(events[-1]["code"], 0)

    def test_parent_question_stops_before_the_next_participant(self):
        self.reply.write_text(REPORT_DONE)
        first = self.open("--report")
        second = self.open("--report")
        self.reply.write_text(REPORT_ASK)
        result, conversation = self.discuss(first, second)
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(conversation["status"], "needs_parent")
        self.assertEqual(conversation["needs_parent"]["worker"], first)
        self.assertEqual(conversation["needs_parent"]["question"], "Use A or B?")
        self.assertEqual(len(conversation["transcript"]), 1)
        self.assertEqual(self.state(second)["turn"], 1)
        self.assertFalse(list((self.registry / second / "requests").glob("*.json")))

    def test_failure_and_required_report_validation_stop_the_exchange(self):
        first = self.open("--report")
        second = self.open()
        result, conversation = self.discuss(first, second, rounds=1)
        self.assertEqual(result.returncode, 1)
        self.assertIn("valid required report", conversation["error"])
        self.assertEqual(self.state(second)["turn"], 1)
        # A protocol-valid but failed turn must not be handed on as peer success.
        result, failed = self.discuss(second, first, message="FIXTURE_MODE=terminal-failure", rounds=1)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(failed["transcript"][0]["status"], "failed")
        self.assertEqual(len(failed["transcript"]), 1)

    def test_timeout_preserves_pending_identity_and_stops_scheduling(self):
        first, second = self.open(), self.open()
        result, conversation = self.discuss(first, second, message="FIXTURE_MODE=brief", timeout=0.08)
        self.assertEqual(result.returncode, 124)
        self.assertEqual(conversation["status"], "timed_out")
        self.assertEqual(self.state(second)["turn"], 1)
        pending = conversation["pending_request"]
        if pending:
            self.assertEqual(pending["worker"], first)
            self.assertIn("may still complete", conversation["error"])
            self.eventually(lambda: Path(pending["response_file"]).exists())
            completed = json.loads(Path(pending["response_file"]).read_text())
            self.assertEqual(completed["request_id"], pending["request_id"])
        else:
            # Expiry can be acknowledged before the waiter observes its deadline.
            self.assertEqual(conversation["transcript"][-1]["code"], 124)
            self.assertIn("expired before delivery", conversation["error"])

    def test_preflight_rejects_duplicate_workers_bad_limits_and_recursion(self):
        first, second = self.open(), self.open()
        self.cli("alias", "set", "same", first)
        for extra in ([first, "@same"], [first, second, "--rounds", "9"],
                      [first, second, "--timeout", "nan"], [first, second, "--message", " "],
                      [first, second, "--message", "x" * (128 * 1024 + 1)]):
            # Large inputs go through files to stay below argv's OS limit.
            if len(extra[-1]) > 128 * 1024:
                big = self.path / "big.md"
                big.write_text(extra[-1])
                extra = [first, second, "--message-file", str(big)]
            topic = [] if "--message" in extra or "--message-file" in extra else ["--message", "Topic"]
            result = self.cli("conversation", "start", *extra, *topic)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        nested = self.cli("conversation", "start", first, second, "--message", "Topic", CLAIVE_WORKER_ID=first)
        self.assertEqual(nested.returncode, 1)
        self.assertIn("may not launch workers", nested.stderr)
        self.assertFalse((self.registry / "conversations").exists())
        self.assertEqual(self.state(first)["turn"], 1)

    def test_workers_started_before_receipt_support_are_rejected_before_delivery(self):
        first, second = self.open(), self.open()
        older = self.state(first)
        older.pop("request_receipts")
        module.save(self.registry / first / "state.json", older)
        result = self.cli("conversation", "start", first, second, "--message", "Topic")
        self.assertEqual(result.returncode, 1)
        self.assertIn("supervisor predates conversation support", result.stderr)
        self.assertFalse(list((self.registry / first / "requests").glob("*.json")))
        self.assertFalse((self.registry / "conversations").exists())

    def test_receipts_survive_unrelated_followups_and_legacy_queue_requests(self):
        worker = self.open()
        initial_request = "e" * 32
        module.save(self.registry / worker / "requests" / "first.json",
                    dict(request_id=initial_request, prompt_file=str(self.prompt)))
        self.cli("wait", worker)
        responses = self.registry / worker / "responses"
        first_file = next(responses.glob("*.json"))
        original = first_file.read_text()
        # Rejection must create a different receipt rather than overwriting the first.
        request_id = "a" * 32
        module.save(self.registry / worker / "requests" / "rejected.json",
                    dict(request_id=request_id, prompt_file=str(self.path / "missing.md")))
        self.eventually(lambda: (responses / f"{request_id}.json").exists())
        rejected = json.loads((responses / f"{request_id}.json").read_text())
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(rejected["answer"], "")
        self.assertEqual(first_file.read_text(), original)
        self.cli("followup", worker, "--prompt-file", str(self.prompt))
        self.cli("wait", worker)
        self.assertEqual(first_file.read_text(), original)
        # Old request files remain usable without forging an ID or rewriting a receipt.
        module.save(self.registry / worker / "requests" / "legacy.json", dict(prompt_file=str(self.prompt)))
        self.cli("wait", worker)
        self.assertEqual(first_file.read_text(), original)
        self.assertEqual(len(list(responses.glob("*.json"))), 2)
        turn_before = self.state(worker)["turn"]
        duplicate = self.registry / worker / "requests" / "duplicate.json"
        module.save(duplicate, dict(request_id=initial_request, prompt_file=str(self.prompt)))
        self.eventually(lambda: not duplicate.exists())
        self.assertEqual(self.state(worker)["turn"], turn_before)
        self.assertEqual(first_file.read_text(), original)

    def test_malformed_queue_metadata_is_rejected_without_killing_the_supervisor(self):
        worker = self.open()
        path = self.registry / worker
        malformed = [dict(request_id="bad", prompt_file=str(self.prompt)),
                     dict(request_id=1, prompt_file=str(self.prompt)),
                     dict(request_id="f" * 32, prompt_file=str(self.prompt), expires_at="later"),
                     dict(prompt_file=str(self.prompt), expires_at=float("nan")),
                     dict(prompt_file=str(self.prompt), expires_at=10 ** 400),
                     dict(request_id="f" * 32), ["not", "an", "object"]]
        for request in malformed:
            queued = path / "requests" / "malformed.json"
            module.save(queued, request)
            self.eventually(lambda: not queued.exists())
            state = self.state(worker)
            self.assertEqual(state["status"], "idle")
            self.assertEqual(state["failure_kind"], "rejected")
            self.assertEqual(state["turn"], 1)
        self.assertEqual(self.cli("followup", worker, "--prompt-file", str(self.prompt)).returncode, 0)
        self.assertEqual(self.cli("wait", worker).returncode, 0)
        self.assertEqual(self.state(worker)["turn"], 2)

    def test_expired_and_parent_blocked_requests_never_start_a_turn(self):
        self.reply.write_text(REPORT_DONE)
        worker = self.open("--report")
        path = self.registry / worker
        expired = "b" * 32
        module.save(path / "requests" / "expired.json",
                    dict(request_id=expired, prompt_file=str(self.prompt), expires_at=time.time() - 1))
        self.eventually(lambda: (path / "responses" / f"{expired}.json").exists())
        self.assertEqual(self.state(worker)["turn"], 1)
        stopped = "d" * 32
        stop_file = self.path / "stop.request"
        stop_file.touch()
        module.save(path / "requests" / "stopped.json",
                    dict(request_id=stopped, prompt_file=str(self.prompt), stop_file=str(stop_file)))
        self.eventually(lambda: (path / "responses" / f"{stopped}.json").exists())
        stopped_reply = json.loads((path / "responses" / f"{stopped}.json").read_text())
        self.assertIn("stopped before delivery", stopped_reply["error"])
        self.assertEqual(self.state(worker)["turn"], 1)
        self.reply.write_text(REPORT_ASK)
        self.cli("followup", worker, "--prompt-file", str(self.prompt))
        self.assertEqual(self.cli("wait", worker).returncode, 3)
        blocked = "c" * 32
        module.save(path / "requests" / "blocked.json",
                    dict(request_id=blocked, prompt_file=str(self.prompt), requires_no_parent=True))
        self.eventually(lambda: (path / "responses" / f"{blocked}.json").exists())
        response = json.loads((path / "responses" / f"{blocked}.json").read_text())
        self.assertEqual(response["code"], 3)
        self.assertEqual(response["needs_parent"]["question"], "Use A or B?")
        self.assertEqual(self.state(worker)["turn"], 2)

    def test_two_pi_models_and_claude_exchange_replies_through_their_adapters(self):
        fake_claude = self.path / "fake-claude.py"
        fake_claude.write_text('''#!/usr/bin/python3
import json, os, sys
from pathlib import Path
identifier = sys.argv[sys.argv.index("--resume") + 1] if "--resume" in sys.argv else sys.argv[sys.argv.index("--session-id") + 1]
session = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects/test" / (identifier + ".jsonl")
session.parent.mkdir(parents=True, exist_ok=True)
session.touch()
answer = json.dumps({"prompt": sys.argv[-1], "resumed": "--resume" in sys.argv, "tools": sys.argv[sys.argv.index("--tools") + 1]})
print(json.dumps({"type":"result", "subtype":"success", "result":answer}))
''')
        fake_claude.chmod(0o700)
        self.env["CLAUDE_WORKER_BINARY"] = str(fake_claude)
        first = self.open("--engine", "pi", "--model", "mimo-v2.6-flash-free", "--read-only")
        second = self.open("--engine", "pi", "--model", "big-pickle", "--read-only")
        third = self.open("--engine", "claude", "--read-only")
        result, conversation = self.discuss(first, second, third, rounds=1)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        replies = conversation["transcript"]
        self.assertEqual([item["engine"] for item in replies], ["pi", "pi", "claude"])
        self.assertEqual([item["model"] for item in replies],
                         ["mimo-v2.6-flash-free", "big-pickle", "claude-haiku-5-5"])
        for reply in replies:
            answer = json.loads(reply["answer"])
            self.assertTrue(answer["resumed"])
            self.assertNotIn("edit", answer["tools"].lower())
        self.assertIn(first, json.loads(replies[1]["answer"])["prompt"])
        self.assertIn(second, json.loads(replies[2]["answer"])["prompt"])

    def test_concurrent_followup_and_alias_rebind_do_not_redirect_replies(self):
        first = self.open("--model", "a", FIXTURE_MODEL_MODES='{"a":"brief"}')
        second, replacement = self.open(), self.open()
        self.cli("alias", "set", "builder", first)
        process = subprocess.Popen([FIXTURE_CLI, "conversation", "start", "@builder", second,
                                    "--message", "Discuss", "--rounds", "2", "--json"],
                                   env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.processes.append(process)
        def pending():
            for path in (self.registry / "conversations").glob("*/state.json"):
                state = json.loads(path.read_text())
                if state.get("pending_request"):
                    return state
        self.eventually(pending)
        unrelated = self.path / "unrelated.md"
        unrelated.write_text("FIXTURE_MODE=ask")
        self.assertEqual(self.cli("followup", first, "--prompt-file", str(unrelated)).returncode, 0)
        self.assertEqual(self.cli("alias", "set", "builder", replacement, "--replace").returncode, 0)
        out, err = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, out + err)
        transcript = json.loads(out)["transcript"]
        self.assertEqual([item["worker"] for item in transcript], [first, second, first, second])
        self.assertTrue(all(item["answer"] == "PEER REPLY" for item in transcript))
        self.assertEqual(self.state(replacement)["turn"], 1)
        self.assertEqual(self.state(first)["turn"], 4)

    def test_stopping_the_controller_keeps_delivery_visible_and_schedules_no_peer(self):
        first, second = self.open(), self.open()
        process = subprocess.Popen([FIXTURE_CLI, "conversation", "start", first, second,
                                    "--message", "FIXTURE_MODE=brief", "--json"],
                                   env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.processes.append(process)
        def pending():
            for path in (self.registry / "conversations").glob("*/state.json"):
                state = json.loads(path.read_text())
                if state.get("pending_request"):
                    return state
        self.eventually(pending)
        process.send_signal(signal.SIGTERM)
        out, err = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 130, out + err)
        state = json.loads(out)
        self.assertEqual(state["status"], "cancelled")
        self.assertIsNotNone(state["pending_request"])
        self.assertEqual(self.state(second)["turn"], 1)
        self.assertFalse(list((self.registry / second / "requests").glob("*.json")))


if __name__ == "__main__":
    unittest.main()
