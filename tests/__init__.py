"""Let `python3 -m unittest tests/test_x.py` find `hermetic` and `claivelib` without PYTHONPATH."""
from pathlib import Path
import sys

for _path in (Path(__file__).resolve().parent, Path(__file__).resolve().parents[1] / "bin"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))
