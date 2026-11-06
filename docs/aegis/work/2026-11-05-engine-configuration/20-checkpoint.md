# Current checkpoint

Implemented the uniform engine configuration across all five engines.

- `config.py`: `binary`, `provider`, `env`, `prompt_limit` added to `ENGINE_KEYS`
  and validated for every engine; `UNIFORM_ENGINE_KEYS`; `ENV_NAME`;
  `engine_setting`, `engine_env`, `engine_effective`; docstring updated.
- `engine.py`: base `defaults()` and `child_env_for_turn` now merges
  `engines.<name>.env` over the engine's own environment.
- `engines/__init__.py`: `engine_binary(name)` resolves the configured executable.
- Each engine declares `defaults()` and reads `config.engine_setting` for its
  binary and provider. AGY reads `prompt_limit` from the config in
  `validate_prompt`. Codex's web turn keeps `CODEX_HOME` and gains the configured
  environment.
- `cli.py`: `engines --json` carries a `config` block per engine; new
  `claive engine config [NAME] [--json]`; doctor checks the configured binary.
- `orchestration.py`: readiness uses `engine_binary`.

Docs: `docs/designs/engine-configuration.md` (design),
`docs/engine-configuration.md` (operator guide), `docs/command-reference.md`,
README, and the plan at `docs/aegis/plans/2026-11-05-engine-configuration.md`.

## Model tiers

The user asked for three tiers rather than one flat allowlist, and for the catalog
to stop being hard-coded.

- `config.supported_models(name, engine)` merges the engine's declared catalog with
  `engines.<name>.extra_models`. An open catalog (Pi) stays open.
- `config.model_tiers(name, engine)` returns the default, the allowed set and the
  extra set. Allowed is the allowlist when present, else the declared catalog.
- `config.check_engine(..., allow=())` refuses an extra model with both ways to
  opt in: `--allow-model MODEL` for one worker, or an entry in
  `engines.<name>.models` for good. A model the catalog has never heard of is
  refused the same way, naming `engines.<name>.extra_models`.
- `--allow-model` is recorded on the launch record and carried by
  `TurnRequest.allowed_models`, so a follow-up keeps the permission.
- `claive models [NAME] [--json]` prints the three tiers.
- Codex, AGY and Muse enforce the merged catalog instead of their module constants.

## Verification so far

- `tests.test_engine_config`: 39 tests, OK.
- `tests.test_model_tiers`: 24 tests, OK.
- `test_provider` + `test_engine_config`: 45 tests, OK.
- 19-module regression set (engine policy, all five engines, isolation, orch,
  fallback, budget, steering): 403 tests, one failure, corrected below.
- Live: `claive models` prints the three tiers; an allowlist moves the rest into
  `extra`; a tier-3 model is refused with the flag and the config edit; an unknown
  model is refused naming `extra_models`; `claive engine config` reports sources.
- `uvx ruff check --select F bin/claivelib tests`: all checks pass.

## Corrections found during verification

`doctor` crashed with no output when the config was invalid. The new
`engine_binary` call reads the config inside the engine loop, and the loop only
caught `ImportError` and `KeyError`, so the `ValueError` escaped to `main` before
any JSON was printed. The loop now catches `ValueError` and reports
"configuration error; see the config check", leaving the root cause to the
existing required `config` check. `test_provider` passes again.

`engines.claude.provider` and `engines.codex.provider` were accepted by the config
but refused by the engine: both `validate_turn` methods compared against the module
constant. Both now compare against `config.engine_setting(name, "provider", ...)`.
`test_model_tiers` covers both.

The refusal message sorted the allowed names, which broke the existing
`test_engine_policy` contract on insertion order. The message keeps the configured
order.

## Open

`just check` is running. Do not edit source while it runs. Then reinstall the
runtime, since the installed `~/.local/bin/claive` predates both this work and the
Pi multi-provider work.

Commit scope: this task's source, tests, docs and work record, plus the Pi
multi-provider work. Stage the `cli.py` hunks that belong to these tasks and leave
the user's turn-environment edit unstaged. Do not push without explicit approval.
