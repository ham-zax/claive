# Plan: YAML config file and complete enable/disable control

Design: `docs/designs/engine-configuration.md` (engine keys) and
`docs/engine-configuration.md` (operator guide).

## What already works

The JSON file already covers most of the request:

| Want | Key | Enforced |
| --- | --- | --- |
| disable a harness | `engines.<name>.enabled: false` + `reason` | every launch and follow-up |
| disable a model | `engines.<name>.disabled_models` (list or `{name: reason}`) | every launch and follow-up |
| disable a reasoning effort for one model | `engines.<name>.disabled_model_efforts.<model>` | every launch and follow-up |
| allow only some models/efforts | `engines.<name>.models.<model>.efforts` | every launch and follow-up |
| disable a Pi provider | `engines.pi.providers.<name>.enabled: false` | every Pi turn |

## Revision 2026-11-06

The user asked that a reasoning effort be disabled per model, never for the whole
engine. `disabled_reasoning_efforts` is removed. `disabled_model_efforts` replaces
it, and the old key fails with the replacement in the message.

## Gaps

1. The file must be JSON. The user wants YAML.
2. Provider enable/disable exists only for Pi. Claude, Codex, AGY and Muse accept a
   `provider` label but cannot refuse it.
3. No command validates the file, names the active file, scaffolds one, or converts
   an existing JSON file.
4. No single document lists every key and every disable recipe.

## Phase 1 — the file

`config.py`

- `config_path()` discovers `CLAIVE_CONFIG`, else `config.yaml`, `config.yml`,
  `config.json` in `~/.config/claive/`. With no file it names `config.yaml`.
- `config_files()` returns the files in discovery order, so a shadowed JSON file is
  visible instead of silently ignored.
- `config_format(path)` chooses by suffix; an unknown suffix sniffs JSON by its
  first non-space character, else YAML.
- YAML uses `yaml.safe_load` through a loader that refuses duplicate keys. A
  missing PyYAML refuses a YAML file with the install command; JSON keeps working
  with the standard library alone.
- An empty file means `{}`. A non-mapping is refused with the path.
- `load()` splits into `load()` (read) and `load_text(text, path)` (parse and
  validate), so `config init` validates its template before writing it.
- Every existing validator stays unchanged: both formats produce the same dict.

## Phase 2 — provider policy for every engine

- `providers` leaves `ENGINE_ONLY_KEYS`; `_validate_pi_providers` becomes
  `_validate_provider_policy` and runs for every engine.
- `config.provider_policy(name, provider)` and `config.check_provider(name, provider)`
  replace `pi_provider_policy` and `check_pi_provider`.
- `cli.validate_turn` calls `check_provider` after the engine's own rules, so every
  launch, follow-up, steer and fork is covered by one call site.
- Validation refuses a `provider` or `default_provider` that names a disabled
  provider.
- Pi keeps the richer meaning: the same block drives its provider listing and its
  default provider.

## Phase 3 — the command surface

- `claive config path` — the active file, one line, for scripts.
- `claive config check [--json]` — parse, validate, and run the cross-checks the
  launch path runs; exit 1 with the exact error.
- `claive config show [--json]` — the active file as text, or as JSON.
- `claive config init [--force]` — write a commented YAML template with every key;
  refuse to overwrite; validate the template before writing.
- `claive config migrate [--force]` — write `config.yaml` from the active file,
  then rename the old JSON to `config.json.bak-<timestamp>`.
- `doctor` names the active file and its format.

## Phase 4 — tests and docs

- `tests/test_config_file.py`: discovery order, both formats, duplicate keys,
  missing PyYAML, empty and non-mapping files, every command, template validity,
  migrate round-trip.
- `tests/test_engine_policy.py`: provider disable on all five engines, its reason,
  explicit enable, and the default-provider conflict.
- `docs/engine-configuration.md`: YAML first, the full file, recipes for disabling
  a harness, a model, an effort and a provider.
- `docs/command-reference.md`, README, and the module docstring.

## Verification

- Focused: `test_config_file`, `test_engine_config`, `test_engine_policy`,
  `test_model_tiers`, `test_provider`, `test_pi_engine`, `test_pi_multiprovider`.
- Full suite once at the end.
- Live: migrate this host's config, `claive config check`, `claive engines`,
  `claive engine config pi`, and one real Pi turn.

## Risks

- `config_path()` is in every refusal message. Discovery must stay deterministic
  and name the file it used.
- YAML 1.1 reads `on`, `off`, `yes` and `no` as booleans. The docs say to quote a
  value that must stay a string.
- `cli.validate_turn` is on every launch path. The provider check reads
  `request.provider`, which the engine has already resolved, so it cannot reject a
  provider the engine chose itself.
- PyYAML is not a declared dependency. YAML stays optional; JSON needs nothing.
