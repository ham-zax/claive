# Implementation plan: Pi multi-provider support

* **Date:** 2026-11-05
* **Spec:** `docs/designs/pi-multiprovider.md` (approved 2026-11-05)
* **Branch:** `main`

## Goal

Remove Claive's single-provider pin on Pi so any provider Pi knows can run a
worker, with Claive owning policy and reporting, Pi owning definitions and
credentials, and one narrow safe writer for `models.json`.

Acceptance: `claive run --engine pi --provider mistral --model mistral-large-4`
completes a turn on this host; a disabled provider is refused with its reason;
`claive engines` and `claive doctor` report every provider with readiness;
`claive pi provider add|remove|list|check` works headlessly; the arbiter and
critic can route to a non-default provider.

## Architecture

Claive stays a supervisor. Pi owns provider definitions (`models.json`), the
built-in catalog, and credentials (`auth.json`, environment variables). Claive
owns policy (`engines.pi.providers`, `default_provider`), model identity
(`provider/model`), discovery for reporting, and a strictly additive writer for
`models.json`.

## Tech stack

Python 3.12, `unittest`, no new dependencies. Existing modules:
`bin/claivelib/{config,provider,cli,orchestration}.py` and
`bin/claivelib/engines/pi.py`.

## Baseline and authority refs

- `docs/designs/pi-multiprovider.md` — approved design, owns the decisions.
- `docs/pi-engine.md` — current Pi behavior and limits.
- `docs/design-worker-freedom.md` — supervisor-not-sandbox principle.
- `bin/claivelib/engine.py` — `WorkerEngine` contract.
- `tests/fake_pi.py`, `tests/test_pi_engine.py`, `tests/test_provider.py` — existing Pi coverage.

## Compatibility boundary

- Existing `providers.opencode2api.base_url` configs keep working.
- `--provider opencode2api` and bare `--model` keep working.
- Existing worker records keep their `launch.provider`; follow-ups use it.
- Absent policy means previous behavior. No migration.
- Retired: the `opencode2api`-only refusal in `pi.py:138` and the
  `providers may only hold opencode2api` validation in `config.py:138`.

## TDD route

`TDD Route: auto` → **strict**. Authority: contract change, persistence
(`models.json` writer), shared/core modules, and producer/consumer surfaces.
Each behavior task writes a failing test first, then the minimum change.

## Verification

- `python3 -m unittest discover -s tests -p 'test_*.py'` — full suite green.
- Live: `claive run --engine pi --provider mistral --model mistral-large-4`.
- `claive engines`, `claive doctor`, `claive pi provider list` on this host.
- `shellcheck install.sh bin/claive*` unchanged.

---

## Phase 1 — Lift the restriction

### Task 1.1 — Provider-aware `models.json` reads

Files: `bin/claivelib/provider.py`, `tests/test_provider.py`

- `read_models(directory=None, name=PROVIDER)` returns the named entry.
- `override_url(name=PROVIDER)` reads `providers.<name>.base_url`.
- `overlay_agent_dir(state_root, names=None)` rewrites every overridden entry.
- Keep `PROVIDER = "opencode2api"` as the compatibility default.
- Tests: named read, missing provider, multiple overrides in one overlay,
  self-loop guard still refuses.

### Task 1.2 — Config accepts any provider name

Files: `bin/claivelib/config.py`, `tests/test_config.py`

- `providers` validation accepts any `[a-z][a-z0-9_.-]*` name with `base_url`.
- Loopback restriction unchanged.
- Tests: arbitrary name accepted, non-loopback refused, bad shape refused.

### Task 1.3 — Model identity

Files: `bin/claivelib/engines/pi.py`, `bin/claivelib/cli.py`, `tests/test_pi_engine.py`

- Parse `provider/model` at the CLI boundary into `(provider, model)`.
- Refuse a `--provider` that disagrees with a `provider/model`.
- Persist the resolved pair in `launch.provider` and `launch.model`.
- Tests: slash form, bare form, conflict refusal, persistence.

### Task 1.4 — Remove the provider refusal

Files: `bin/claivelib/engines/pi.py`, `tests/test_pi_engine.py`

- `validate_turn` accepts any non-empty provider name.
- `resolve_launch` defaults to the effective default provider.
- `_settings` writes the actual provider, not the constant.
- Tests: mistral accepted, empty provider refused, settings write uses the
  selected provider.

---

## Phase 2 — Provider policy and reporting

### Task 2.1 — Policy surface

Files: `bin/claivelib/config.py`, `tests/test_config.py`

- `pi_provider_policy(name)` returns the entry or `{}`.
- `check_pi_provider(name)` refuses a disabled provider with its reason.
- `pi_default_provider()` resolves policy → Pi settings → `opencode2api`.
- Validation: unknown keys, non-boolean `enabled`, non-string `default_provider`.
- Tests: disabled refusal with reason, default resolution order, validation errors.

### Task 2.2 — Discovery

Files: `bin/claivelib/provider.py`, `tests/test_provider.py`

- `catalog_models()` parses `pi --list-models` into `{provider: [model]}`.
- `auth_check(name)` parses `pi auth check --provider NAME --json`.
- `list_providers()` merges `models.json`, catalog, and readiness.
- Parser returns `{}` on an unrecognized table; never raises.
- Tests: captured table fixture, malformed table, empty output, readiness parse.

### Task 2.3 — Reporting

Files: `bin/claivelib/cli.py`, `tests/test_features.py`

- `claive engines` shows Pi providers with enabled state and readiness.
- `claive doctor` checks every enabled provider instead of one.
- Tests: provider rows present, disabled reason shown, doctor names each provider.

---

## Phase 3 — Provider commands

### Task 3.1 — Safe writer

Files: `bin/claivelib/provider.py`, `tests/test_provider.py`

- `write_models(data)` — atomic replace, `0600`, timestamped backup.
- `add_provider(name, base_url, api, api_key_env, models, replace=False)`.
- `remove_provider(name, force=False)`.
- Refuse built-in provider ids, missing models, existing provider without
  `--replace`, and a file changed since read.
- Tests: atomicity, permissions, backup, each refusal, environment-reference form.

### Task 3.2 — Command group

Files: `bin/claivelib/cli.py`, `tests/test_features.py`

- `claive pi provider list|add|remove|check`.
- `check` reports auth readiness and endpoint probe.
- Tests: list output, add then list, remove, check on a ready provider.

---

## Phase 4 — Orchestration routing

### Task 4.1 — Default provider resolution

Files: `bin/claivelib/orchestration.py`, `tests/test_orch_picks.py`

- Replace the four hardcoded `provider: "opencode2api"` sites with
  `config.pi_default_provider()`.
- Model identity becomes `provider/model` where a provider is explicit.
- Tests: default provider honored, disabled provider excluded, fallback keeps
  its provider.

---

## Risks

| risk | mitigation |
|---|---|
| `pi --list-models` table changes | parser degrades to `{}`; reporting only, never launch |
| writer clobbers a concurrent Pi edit | re-read and compare before write; refuse on change |
| policy names a provider Pi lacks | accepted, reported by doctor, refused at launch with guidance |
| existing configs break | absent policy preserves behavior; regression tests |

## Retirement

The `opencode2api`-only refusal and the `providers may only hold opencode2api`
validation are deleted, not retained behind a flag. `docs/pi-engine.md` and the
README lose the restriction text in the same change.
