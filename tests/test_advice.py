"""Launch fit reminders and the reasoning-heavy call note."""
import hermetic  # noqa: F401  (must run before claivelib reads the environment)
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
from claivelib import advice


class AdviceChecks(unittest.TestCase):
    def test_launch_notes_follow_missing_checks_timeout_and_reuse(self):
        bare = advice.launch_notes({"acceptance_contract": {"checks": []}}, advice.LARGE_BRIEF_BYTES)
        self.assertEqual([note.split(":")[0] for note in bare], ["Fit", "Bound", "Scope"])
        small = advice.launch_notes({"acceptance_contract": {"checks": []}}, advice.LARGE_BRIEF_BYTES - 1)
        self.assertEqual([note.split(":")[0] for note in small], ["Fit", "Bound"])
        bounded = advice.launch_notes({"acceptance_contract": {"checks": [{"name": "t"}]},
                                       "turn_timeout": 600, "reusable": True}, 10 ** 6)
        self.assertEqual(bounded, [])

    def test_heavy_call_note_counts_calls_at_or_above_threshold(self):
        limit = advice.HEAVY_CALL_OUTPUT_TOKENS
        self.assertIsNone(advice.heavy_call_note([{"output_tokens": limit - 1}, {}]))
        note = advice.heavy_call_note([{"output_tokens": 700}, {"output_tokens": limit}, {"output_tokens": 128000}])
        self.assertIn("2 of 3 model calls", note)
        self.assertIn("max 128000", note)


if __name__ == "__main__":
    unittest.main()
