# Current checkpoint

Confirmed the original publication-order race with a deterministic process
barrier: old code lets wait return before the response file exists. Reordered
the existing receipt write before the terminal state publication. The same
regression passes on repaired source.

Doctor now compares provider advertisements against configured Pi requirements,
not an obsolete preferred-model tuple. Required provider models fail health
when Pi is the default. Unused preferred models no longer produce false alarms.
Focused regression: 67 tests pass, 2 live skips, 114.020 seconds.
Full `just check` is running. Do not edit source during that check.

Host configuration backup:
`/home/ubuntu/.local/state/claive-audit/20261103-202626/config.json`.
Worker and oracle roles now explicitly use the host's existing Pi default,
`deepseek-v4.1-flash-free` at max. Both local and advertised catalogs contain it.
Source doctor returns ok with both model-registration and endpoint checks required.
Credential-bearing models.json remains untouched.

The installed runtime and service were already newer than the audit described.
Install the tested receipt/doctor patch with runtime-only installation after
checks pass. Recheck empty goal queue before restarting only claive-serve.
Compare the three pre-existing idle worker identities with the saved snapshot.
Then perform one bounded Pi worker smoke test using the corrected role.

README now describes per-host engine policy without contradictory host claims.
Do not add an allowlist or prune stored workers/worktrees. Those actions need
the user's workspace choices or explicit deletion scope.

Keep Codex web edits unchanged and outside this task's commit. Stage only the
CLI hunks between `/tmp/claive-audit-fix/cli.baseline.py` and current cli.py.
Stage only this task's tests, README edits, and work record. Prior standing
instructions authorize commit and push when the checked work is complete.
