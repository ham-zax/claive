# Full hermetic suite (about 4 minutes; run one heavy job at a time).
test:
    python3 -m unittest discover -s tests -p 'test_*.py'

# One or more test files, e.g. `just test-one tests/test_ask.py tests/test_peek.py`.
test-one +files:
    python3 -m unittest {{files}} -v

# Syntax and shell checks, fast enough for every commit.
lint:
    python3 -m compileall -q bin/claivelib tests
    uvx ruff check --select F bin/claivelib tests
    shellcheck install.sh bin/claive-worker bin/claive-codex

check: lint test
