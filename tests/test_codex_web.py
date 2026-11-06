"""Opt-in ChatGPT Web routing must never change the native Codex engine."""
import hermetic  # noqa: F401
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
from claivelib.engine import TurnRequest
from claivelib.engines.codex import CodexEngine, CODEX_MODEL, CODEX_WEB_ALIAS


class CodexWebChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="claive-codex-web-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "codex-web-home"
        self.home.mkdir()
        self.native = self.root / "native-home"
        self.native.mkdir()
        self.prompt = self.root / "prompt.md"
        self.prompt.write_text("Describe the test fixtures.")
        self.binary = self.root / "codex"
        self.binary.write_text("#!/bin/sh\n")
        self.binary.chmod(0o755)
        patcher = patch.dict(os.environ, {"CLAIVE_CODEX_WEB_HOME": str(self.home),
                                          "CODEX_HOME": str(self.native), "CLAIVE_CODEX_WEB_YOLO": "0"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.engine = CodexEngine()

    def request(self, model=CODEX_WEB_ALIAS, effort="high", steering=False, read_only=False):
        return TurnRequest(binary=str(self.binary), workspace=str(self.root),
                           prompt_file=str(self.prompt), session_id="01a1184e-d220-7891-95de-8dee4b56367e",
                           provider="openai", model=model, reasoning_effort=effort,
                           max_model_steps=None, read_only=read_only, web=False,
                           output_schema=None, session_logging=True, isolation={}, steering=steering)

    def test_models_are_opt_in_without_changing_native_default(self):
        self.assertEqual(self.engine.default_model, CODEX_MODEL)
        self.assertEqual(self.engine.default_effort_for(CODEX_MODEL), "xhigh")
        self.assertEqual(self.engine.default_effort_for(CODEX_WEB_ALIAS), "high")
        self.assertEqual(self.engine.default_effort_for("chatgpt-web/gpt-6-sol-instant"), "low")
        self.assertEqual(self.engine.default_effort_for("chatgpt-web/gpt-6-pro"), "max")
        self.assertIn(CODEX_WEB_ALIAS, self.engine.supported_models())
        self.assertEqual(self.engine.child_env_for_turn(self.request(model=CODEX_MODEL)), {})
        self.assertEqual(self.engine.child_env_for_turn(self.request()), {"CODEX_HOME": str(self.home)})

    def test_web_alias_resolves_to_native_codex_cli_with_isolated_config(self):
        command = self.engine.build_command(self.request())
        self.assertEqual(command[0], str(self.binary))
        self.assertEqual(command[command.index("-m") + 1], "chatgpt-web/gpt-6-sol")
        self.assertNotIn("--ignore-user-config", command)
        self.assertIn('sandbox_mode="workspace-write"', command)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)
        native = self.engine.build_command(self.request(model=CODEX_MODEL, effort="xhigh"))
        self.assertIn("--ignore-user-config", native)
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", native)
        steered = self.engine.build_command(self.request(steering=True))
        self.assertIn("--user-config", steered)
        self.assertEqual(steered[steered.index("--model") + 1], "chatgpt-web/gpt-6-sol")
        self.assertIn("--sandbox", steered)

    def test_explicit_web_yolo_opt_in_and_read_only_guard(self):
        with patch.dict(os.environ, {"CLAIVE_CODEX_WEB_YOLO": "1"}):
            self.assertIn("--dangerously-bypass-approvals-and-sandbox",
                          self.engine.build_command(self.request()))
            self.assertNotIn("--dangerously-bypass-approvals-and-sandbox",
                             self.engine.build_command(self.request(read_only=True)))

    def test_setup_required_before_launch(self):
        launch = dict(self.engine.resolve_launch(model=CODEX_WEB_ALIAS), binary=str(self.binary))
        with self.assertRaisesRegex(ValueError, "setup is incomplete"):
            self.engine.validate_launch(launch)
        (self.home / "config.toml").write_text('openai_base_url = "https://example.com/v1"\n')
        with self.assertRaisesRegex(ValueError, "loopback Responses proxy"):
            self.engine.validate_launch(launch)
        (self.home / "config.toml").write_text('openai_base_url = "http://127.0.0.1:17841/v1"\n')
        self.engine.validate_launch(launch)
        with self.assertRaisesRegex(ValueError, "locked"):
            self.engine.validate_turn(self.request(model=CODEX_WEB_ALIAS, effort="max"))

    def test_web_retain_uses_only_isolated_home(self):
        request = self.request()
        ident = request.session_id
        for home in (self.home, self.native):
            record = home / "sessions" / "2026" / "11" / "03" / f"rollout-2026-11-03T00-00-00-{ident}.jsonl"
            record.parent.mkdir(parents=True)
            record.write_text("")
        web = self.engine.build_command(request)
        self.assertIn("resume", web)
        (self.home / "sessions" / "2026" / "11" / "03" / f"rollout-2026-11-03T00-00-00-{ident}.jsonl").unlink()
        web = self.engine.build_command(request)
        self.assertNotIn("resume", web)
        native = self.engine.build_command(self.request(model=CODEX_MODEL, effort="xhigh"))
        self.assertIn("resume", native)


if __name__ == "__main__":
    unittest.main()
