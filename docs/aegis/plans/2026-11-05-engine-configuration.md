# Plan: uniform engine configuration

Design: `docs/designs/engine-configuration.md`.

## Phase 1 — schema and resolution

1. `config.py`
   - Add `binary`, `provider`, `env`, `prompt_limit` to `ENGINE_KEYS`.
   - Validate: `binary` absolute; `provider` nonempty; `env` maps valid names to
     strings; `prompt_limit` a positive int.
   - Add `engine_setting(name, key, default)`, `engine_env(name)`, and
     `engine_effective(name, engine)`.
   - Extend the module docstring with the uniform table.
2. `engine.py`
   - Base `defaults()` returning the engine-neutral keys.
   - `child_env_for_turn` merges `config.engine_env(name)` over `child_env()`.
3. Each engine module
   - Declare `defaults()` with binary, provider, model, effort, prompt limit.
   - `resolve_launch` reads `config.engine_setting` for binary and provider.
   - AGY reads `prompt_limit` from config in `validate_prompt`.

## Phase 2 — reporting and headless surface

4. `cli.py`
   - `engines_report` adds a `config` block per engine: effective value and
     source for every declared key.
   - New `claive engine config <name> [--json]`, non-interactive, read-only.
   - `doctor` and the readiness checks use the configured binary, not
     `module.default_binary()`.
5. `orchestration.py` readiness uses the configured binary.

## Phase 3 — tests and docs

6. `tests/test_engine_config.py`: schema validation, precedence, env merge,
   prompt limit, effective reporting, and the CLI surface.
7. `docs/engine-configuration.md` (operator guide), `docs/command-reference.md`,
   README, and the Aegis work record.

## Verification

- Focused: `test_engine_config`, `test_engine_policy`, `test_claude_engine`,
  `test_codex_web`, `test_pi_engine`, `test_pi_multiprovider`, `test_provider`.
- Full suite once at the end.
- Live: `claive engines --json` and `claive engine config <name>` on this host,
  plus one real turn per engine that is installed here.

## Risks

- `child_env_for_turn` is on the launch path; a merge bug changes every worker's
  environment. Covered by the existing Codex web assertions plus new ones.
- `doctor` currently checks `default_binary()`; switching it to the configured
  binary could turn a passing host red if a config names a bad path. That is the
  correct outcome, and the message must name the config file.
- `prompt_limit` is AGY-only today; the key is uniform but only AGY enforces it.
  Documented rather than silently ignored.
