"""Terminal control stripping for human-facing output."""
import sys
from pathlib import Path
import unittest


REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "bin"))
from claivelib.ansi import strip_controls


class StripControlsChecks(unittest.TestCase):
    def test_removes_csi_and_osc_sequences(self):
        self.assertEqual(strip_controls("before\x1b[2Jafter"), "beforeafter")
        self.assertEqual(strip_controls("before\x1b]0;title\x07after"), "beforeafter")

    def test_removes_other_escape_sequences_and_string_payloads(self):
        self.assertEqual(strip_controls("a\x1b(Bb"), "ab")
        self.assertEqual(strip_controls("a\x1bPpayload\x1b\\b"), "ab")

    def test_removes_c0_del_and_c1_controls_but_keeps_newlines_and_tabs(self):
        controls = "".join(chr(code) for code in range(0x20)) + "\x7f\x80\x85\x9f"
        self.assertEqual(strip_controls(controls), "\t\n")
        self.assertEqual(strip_controls("A\x9b2JB"), "AB")

    def test_preserves_printable_unicode_exactly(self):
        value = "café नमस्ते 東京 🧑‍💻 e\u0301"
        self.assertEqual(strip_controls(value), value)


if __name__ == "__main__":
    unittest.main()
