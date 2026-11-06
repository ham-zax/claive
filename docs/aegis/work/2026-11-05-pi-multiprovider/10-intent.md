# Pi multi-provider support

The user asks Claive to grow around Pi: support multiple providers, disable a
provider, add a provider, and configure them. The approved scope is Option C,
all four phases, "robust and clean, follow best practices". The Vibe engine is
explicitly out of scope.

Snapshot: main at cd11da6, host oci-hunter-arm, aarch64. Pi 1.5.0. Existing
uncommitted user changes: `bin/claivelib/engine.py`,
`bin/claivelib/engines/codex.py`, `docs/design-worker-freedom.md`,
`concepts_brief.md`, `docs/chatgpt-web.md`, `docs/designs/`, `scratch/`, and
`tests/test_codex_web.py`. `bin/claivelib/cli.py` carries both user edits and
this task's edits. Do not commit the user's edits.

## Ownership boundary

Pi owns *what exists*: `models.json`, its built-in catalog, and `auth.json`.
Claive owns *policy*: enable/disable with a reason, an allowlist, and the
default provider. Claive adds exactly one narrow writer for `models.json` and
never stores or reads credentials.

## Decisions

- Model identity is `provider/model` for Pi. A bare `model` resolves against the
  effective default provider. A conflicting `--provider` plus `provider/model`
  is refused rather than silently resolved.
- Default provider resolution: `engines.pi.default_provider`, then Pi
  `settings.json` `defaultProvider`, then `opencode2api`.
- The loopback override generalizes from `opencode2api` to any provider name.
  It stays loopback-only and remains a redirect, not a definition.
- `remove` does not gate built-in ids behind `--force`. It deletes the entry and
  reports when Pi still serves the provider from its built-in catalog. Blocking
  the operator taught nothing; reporting the truth does, and the write is
  reversible from the backup.
- `pi --list-models` has no JSON output. Table parsing is the single fragile
  dependency and must degrade reporting, never block a turn.

Change necessity: the `opencode2api`-only refusal is retired, so the engine,
config, CLI, and orchestration must agree on one provider resolution rule.
Architecture review: no new state owner. Pi keeps the catalog; Claive keeps
policy; the writer is a bounded, backed-up, atomic edit of Pi's own file.

TDD route: off. Regression checks required. Verification must include a live
Mistral turn, because the whole point is a second real provider.
