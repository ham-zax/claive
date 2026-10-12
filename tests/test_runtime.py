import hermetic  # noqa: F401  (must run before claivelib reads the environment)
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "bin"))
from claivelib import runtime

GOOD_CODEX = (
    'import os\n'
    'ignore_user_config = os.environ.get("CLAIVE_CODEX_USE_USER_CONFIG", "").lower()'
    ' not in {"1", "true", "yes"}\n'
    'shared = ["--json"] + (["--ignore-user-config"] if ignore_user_config else [])'
    ' + ["--ignore-rules"]\n'
)

PACKAGE_FILES = {
    "__init__.py": "",
    "cli.py": "print('cli')\n",
    "engines/__init__.py": "",
}

LAUNCHERS = {
    "claive": "#!/usr/bin/python3\nprint('claive')\n",
    "claive-worker": "#!/usr/bin/env bash\nexit 0\n",
}


class RuntimeChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="runtime-check-")
        self.root = Path(self.temp.name)
        self.source = self.root / "src"
        self.installed_bin = self.root / "bin"
        self.installed = self.installed_bin / "claivelib"
        files = dict(PACKAGE_FILES)
        files["engines/codex.py"] = GOOD_CODEX
        for base, extra in ((self.source / "bin" / "claivelib", files),
                            (self.installed, files)):
            for name, text in extra.items():
                path = base / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
        for base in (self.source / "bin", self.installed_bin):
            for name, text in LAUNCHERS.items():
                path = base / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
        record = runtime.build_provenance(self.source)
        (self.installed / runtime.PROVENANCE_FILENAME).write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def drift(self, source=None):
        return runtime.check_drift(self.source if source is None else source, self.installed)

    def test_clean_install_has_no_drift_and_passes_smoke(self):
        # Caches and the generated provenance must not count as drift.
        cache = self.installed / "__pycache__"
        cache.mkdir(exist_ok=True)
        (cache / "cli.cpython-314.pyc").write_bytes(b"\0")
        result = self.drift()
        self.assertEqual(result["status"], "clean")
        self.assertTrue(result["ok"])
        self.assertEqual((result["changed"], result["deleted"], result["extra"]), ([], [], []))
        self.assertTrue(runtime.smoke_check(self.installed)["ok"])
        # Without an explicit source, the provenance path is reused read-only.
        inferred = runtime.check_drift(None, self.installed)
        self.assertEqual(inferred["status"], "clean")

    def test_changed_file_is_reported(self):
        (self.installed / "cli.py").write_text("print('patched')\n", encoding="utf-8")
        result = self.drift()
        self.assertEqual(result["status"], "drifted")
        self.assertFalse(result["ok"])
        self.assertEqual(result["changed"], ["cli.py"])
        self.assertEqual((result["deleted"], result["extra"]), ([], []))

    def test_deleted_file_is_reported(self):
        (self.installed / "cli.py").unlink()
        result = self.drift()
        self.assertEqual(result["status"], "drifted")
        self.assertFalse(result["ok"])
        self.assertEqual(result["deleted"], ["cli.py"])

    def test_extra_file_is_reported(self):
        (self.installed / "extra_tool.py").write_text("print('extra')\n", encoding="utf-8")
        result = self.drift()
        self.assertEqual(result["status"], "drifted")
        self.assertFalse(result["ok"])
        self.assertEqual(result["extra"], ["extra_tool.py"])

    def test_changed_launcher_is_reported(self):
        (self.installed_bin / "claive").write_text("#!/usr/bin/python3\nprint('patched')\n",
                                                   encoding="utf-8")
        result = self.drift()
        self.assertEqual(result["status"], "drifted")
        self.assertFalse(result["ok"])
        self.assertEqual(result["changed"], ["bin/claive"])

    def test_missing_provenance_warns_without_crashing(self):
        (self.installed / runtime.PROVENANCE_FILENAME).unlink()
        self.assertIsNone(runtime.load_provenance(self.installed))
        smoke = runtime.smoke_check(self.installed)
        self.assertFalse(smoke["ok"])
        self.assertTrue(any(runtime.PROVENANCE_FILENAME in failure for failure in smoke["failures"]))
        # Live content still matches, so drift stays clean without provenance.
        self.assertEqual(self.drift()["status"], "clean")
        checks = runtime.doctor_checks(str(self.source), self.installed)
        self.assertEqual([item["name"] for item in checks],
                         ["runtime_provenance", "runtime_drift", "runtime_smoke"])
        self.assertTrue(all(item["required"] is False for item in checks))
        self.assertFalse(checks[0]["ok"])

    def test_smoke_rejects_unconditional_codex(self):
        (self.installed / "engines" / "codex.py").write_text(
            'shared = ["--json", "--ignore-user-config", "--ignore-rules"]\n', encoding="utf-8")
        smoke = runtime.smoke_check(self.installed)
        self.assertFalse(smoke["ok"])
        self.assertTrue(any("opt-in" in failure for failure in smoke["failures"]))
        self.assertIn("--ignore-rules", (self.installed / "engines" / "codex.py").read_text())
        self.assertEqual(self.drift()["changed"], ["engines/codex.py"])

    def test_missing_source_is_advisory(self):
        result = runtime.check_drift(self.root / "no-such-checkout", self.installed)
        self.assertEqual(result["status"], "unknown")
        self.assertTrue(result["ok"])

    def test_malformed_provenance_never_crashes(self):
        for payload in ("[]", "true", "42", '"just a string"'):
            with self.subTest(payload=payload):
                (self.installed / runtime.PROVENANCE_FILENAME).write_text(
                    payload + "\n", encoding="utf-8")
                self.assertIsNone(runtime.load_provenance(self.installed))
                smoke = runtime.smoke_check(self.installed)
                self.assertFalse(smoke["ok"])
                drift = runtime.check_drift(self.root / "no-such-checkout", self.installed)
                self.assertEqual(drift["status"], "unknown")
                checks = runtime.doctor_checks(self.root / "no-such-checkout", self.installed)
                self.assertEqual([item["name"] for item in checks],
                                 ["runtime_provenance", "runtime_drift", "runtime_smoke"])
                self.assertFalse(checks[0]["ok"])
        # A dict with wrong value types still loads, but smoke flags the
        # malformed fingerprint maps instead of crashing on them.
        (self.installed / runtime.PROVENANCE_FILENAME).write_text(
            '{"source_dir": 7, "files": [], "launchers": "x"}\n', encoding="utf-8")
        smoke = runtime.smoke_check(self.installed)
        self.assertFalse(smoke["ok"])
        self.assertTrue(any("malformed" in failure for failure in smoke["failures"]))
        drift = runtime.check_drift(self.root / "no-such-checkout", self.installed)
        self.assertEqual(drift["status"], "unknown")

    def test_tampered_install_fails_smoke_without_source(self):
        # Provenance fingerprints catch a hand-edited install even when the
        # source checkout is unavailable, where drift can only report unknown.
        (self.installed / "cli.py").write_text("print('patched')\n", encoding="utf-8")
        drift = runtime.check_drift(self.root / "no-such-checkout", self.installed)
        self.assertEqual(drift["status"], "unknown")
        smoke = runtime.smoke_check(self.installed)
        self.assertFalse(smoke["ok"])
        self.assertTrue(any("cli.py" in failure and runtime.PROVENANCE_FILENAME in failure
                            for failure in smoke["failures"]))
        checks = runtime.doctor_checks(self.root / "no-such-checkout", self.installed)
        smoke_check = next(item for item in checks if item["name"] == "runtime_smoke")
        self.assertFalse(smoke_check["ok"])

    def test_extra_file_fails_smoke_without_source(self):
        (self.installed / "extra_tool.py").write_text("print('extra')\n", encoding="utf-8")
        smoke = runtime.smoke_check(self.installed)
        self.assertFalse(smoke["ok"])
        self.assertTrue(any("extra_tool.py" in failure for failure in smoke["failures"]))

    def scratch_env(self, tag):
        scratch = self.root / tag
        homes = {}
        for key, env_var in (("bin", "CLAIVE_BIN_DIR"), ("codex", "CODEX_HOME"),
                             ("claude", "CLAUDE_HOME"), ("state", "XDG_STATE_HOME"),
                             ("pi-agent", "PI_CODING_AGENT_DIR")):
            homes[env_var] = str(scratch / key)
        return scratch, dict(os.environ, **homes)

    def test_installer_runtime_only_skips_divergent_managed_skill(self):
        # Skillshare owns skill destinations: a divergent managed skill link
        # must fail the default install yet never block a runtime-only repair.
        scratch, env = self.scratch_env("installer-skills")
        bin_dir = scratch / "bin"
        managed_source = scratch / "managed-src"
        managed_source.mkdir(parents=True)
        (managed_source / "SKILL.md").write_text("divergent managed skill\n", encoding="utf-8")
        link_parent = scratch / "codex" / "skills"
        link_parent.mkdir(parents=True)
        (link_parent / "claive-routing-codex").symlink_to(
            managed_source, target_is_directory=True)
        install = str(REPO / "install.sh")
        default = subprocess.run([install], env=env, capture_output=True, text=True)
        self.assertNotEqual(default.returncode, 0)
        self.assertIn("Managed skill link differs", default.stderr)
        self.assertFalse((bin_dir / "claive").exists())
        self.assertEqual((managed_source / "SKILL.md").read_text(encoding="utf-8"),
                         "divergent managed skill\n")
        repaired = subprocess.run([install, "--runtime-only"], env=env,
                                  capture_output=True, text=True)
        self.assertEqual(repaired.returncode, 0, repaired.stderr)
        installed = bin_dir / "claivelib"
        self.assertTrue((bin_dir / "claive").is_file())
        self.assertTrue((installed / "runtime.py").is_file())
        record = json.loads((installed / runtime.PROVENANCE_FILENAME).read_text(
            encoding="utf-8"))
        self.assertIsInstance(record, dict)
        self.assertIn("files", record)
        # Skill destinations are left untouched by the runtime-only repair.
        self.assertTrue((link_parent / "claive-routing-codex").is_symlink())
        self.assertEqual((managed_source / "SKILL.md").read_text(encoding="utf-8"),
                         "divergent managed skill\n")
        self.assertTrue(runtime.smoke_check(installed)["ok"])
        self.assertEqual(runtime.check_drift(REPO, installed)["status"], "clean")

    def test_installer_preserves_unrelated_matching_launcher_link(self):
        scratch, env = self.scratch_env("installer-unrelated-link")
        bin_dir = scratch / "bin"
        bin_dir.mkdir(parents=True)
        unrelated = scratch / "user-owned-launcher"
        unrelated.write_bytes((REPO / "bin" / "claive").read_bytes())
        (bin_dir / "claive").symlink_to(unrelated)
        result = subprocess.run([str(REPO / "install.sh"), "--runtime-only"], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((bin_dir / "claive").is_symlink())
        self.assertEqual((bin_dir / "claive").resolve(), unrelated)

    def test_installer_migrates_own_launcher_link_without_touching_source(self):
        scratch, env = self.scratch_env("installer-project-link")
        bin_dir = scratch / "bin"
        bin_dir.mkdir(parents=True)
        launcher = REPO / "bin" / "claive"
        before = launcher.read_bytes()
        (bin_dir / "claive").symlink_to(launcher)
        dry = subprocess.run([str(REPO / "install.sh"), "--runtime-only", "--dry-run"], env=env, capture_output=True, text=True)
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertTrue((bin_dir / "claive").is_symlink())
        done = subprocess.run([str(REPO / "install.sh"), "--runtime-only"], env=env, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertFalse((bin_dir / "claive").is_symlink())
        self.assertEqual(launcher.read_bytes(), before)
        backups = list((scratch / "state" / "claive-install" / "backups").glob("*/claive"))
        self.assertEqual(len(backups), 1)
        self.assertTrue(backups[0].is_symlink())
        run = subprocess.run([str(bin_dir / "claive"), "runtime", "--source-dir", str(REPO), "--json"], env=env, capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(json.loads(run.stdout)["drift"]["status"], "clean")

    def test_installer_scratch_clean_reinstall_dryrun_smoke_drift(self):
        scratch, env = self.scratch_env("installer-clean")
        bin_dir = scratch / "bin"
        install = str(REPO / "install.sh")
        dry = subprocess.run([install, "--runtime-only", "--dry-run"], env=env,
                             capture_output=True, text=True)
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertFalse((bin_dir / "claive").exists())
        self.assertFalse((bin_dir / "claivelib" / runtime.PROVENANCE_FILENAME).exists())
        done = subprocess.run([install, "--runtime-only"], env=env,
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        installed = bin_dir / "claivelib"
        self.assertTrue(runtime.smoke_check(installed)["ok"])
        drift = runtime.check_drift(REPO, installed)
        self.assertEqual(drift["status"], "clean")
        self.assertTrue(drift["ok"])
        # A tampered install fails smoke, then a reinstall backs it up and is clean.
        (installed / "cli.py").write_text("print('patched')\n", encoding="utf-8")
        self.assertFalse(runtime.smoke_check(installed)["ok"])
        repaired = subprocess.run([install, "--runtime-only"], env=env,
                                  capture_output=True, text=True)
        self.assertEqual(repaired.returncode, 0, repaired.stderr)
        backups = list((scratch / "state" / "claive-install" / "backups").glob("*/claivelib"))
        self.assertEqual(len(backups), 1)
        self.assertTrue(runtime.smoke_check(installed)["ok"])
        self.assertEqual(runtime.check_drift(REPO, installed)["status"], "clean")


if __name__ == "__main__":
    unittest.main()
