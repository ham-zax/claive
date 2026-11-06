# Deployment and receipt audit follow-up

The user provides an operational audit and recommended repairs. Check current
host facts before applying old findings. Repair confirmed health failures and
receipt ordering. Preserve independent edits and live workers.

Snapshot: main at d681b0c, host oci-hunter-arm, aarch64. Existing user changes:
cli.py turn-specific child environment, engine.py child_env_for_turn, Codex web
adapter changes, docs/design-worker-freedom.md, concepts_brief.md, docs/chatgpt-web.md,
docs/designs/, scratch/, and tests/test_codex_web.py. Baseline copies of the three
modified Python modules are in /tmp/claive-audit-fix. Do not commit those edits.

Current facts supersede parts of the audit. Installed runtime matches the dirty
checkout at d681b0c. Service start: 2026-11-03 19:57:05 UTC. No queued goals.
Three idle Codex web workers remain active and must survive service maintenance.
Pi 1.5.0 defaults to opencode2api/deepseek-v4.1-flash-free at max. Its local and
advertised catalogs agree on nine models. Worker and oracle role defaults refer
to unavailable Muse/space-bunny model IDs. No workspace allowlist exists.

Change necessity: publish the durable response before the public completed
state. Add a deterministic process barrier test at that observer boundary.
Doctor must compare the live provider against actual configured Pi needs,
not a stale preferred-model roster. Keep the existing state and receipt owners.
TDD route: off, regression checks required. Architecture review: owner-local
ordering repair and a health-check dependency correction, no new state owner.

Deployment: back up host configuration and change only the stale role model
pins to the current Pi default, at max. Preserve credentials and registrations.
After checks, use runtime-only installation for the receipt fix and restart
only the goal supervisor if its queue is still empty. Preserve worker identities.
Do not create a workspace allowlist or prune workers/worktrees without the
user's workspace decisions or deletion authorization.
