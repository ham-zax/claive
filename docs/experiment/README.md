# TTC experiment: deferred

The test-time-compute experiment for `claive-orch` is intentionally deferred.
Its research notes, corpus, calibration files, protocol, and dedicated skill
were removed from the active repository so everyday orchestration has one clear
workflow and one maintained source of truth.

`claive-orch` remains available for ordinary verifier-gated coding work. Its
arms and ladder are implementation capabilities; they do not mean that an
experiment is running or that experiment results exist.

When the experiment is resumed, do it as a separate, explicit change. Before
the first run, settle and commit the research scope, task corpus, calibration
method, model and compute budgets, gates, data-recording format, and retention
boundary. Reintroduce the protocol and skill together, update the active
documentation and tests, and verify the complete workflow before collecting
results.

Until then, do not use this note as an experiment protocol and do not infer
experimental conclusions from normal `claive-orch` runs.
