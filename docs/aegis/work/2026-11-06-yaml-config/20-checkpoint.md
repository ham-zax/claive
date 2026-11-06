# Checkpoint

## Implemented

- `config.py`: YAML and JSON. Discovery is `CLAIVE_CONFIG`, then `config.yaml`,
  `config.yml`, `config.json`. A duplicate YAML key is refused. New
  `config_dir`, `config_files`, `config_target`, `config_format`, `parse_text`,
  `load_text`, `dump_yaml` and `TEMPLATE`. `load()` now reads and delegates.
- `providers` is uniform: every engine accepts `{name: {enabled, reason}}`, and
  `provider_policy` / `check_provider` replace the Pi-only functions. A
  `provider` or `default_provider` that names a disabled provider is a config
  error.
- `cli.py`: `claive config path|check|show|init|migrate`. `validate_turn` refuses
  a disabled provider on every launch path. `doctor` names the file and format.
- `engines/pi.py`: the Pi-only provider check moved to the shared call.
- Tests: `tests/test_config_file.py` (25 tests), provider cases in
  `test_engine_policy` and `test_pi_multiprovider`.
- Docs: `docs/engine-configuration.md` (YAML first, discovery, disable recipes),
  `docs/command-reference.md`, README, module docstring.

## Verification

- `just test`: 798 tests, OK (3 skipped).
- `uvx ruff check --select F bin/claivelib tests`: pass.
- Live on ARM: migrated `~/.config/claive/config.json` to `config.yaml` (backup
  `config.json.bak-20261106-073248`). `claive config check` reports ok with
  format yaml. `claive engines` and `claive engine config pi` read the file.
  One real Pi turn ran with the YAML defaults (`YAML CONFIG OK`). A disabled
  provider was refused with its reason and the YAML path.
- Reinstalled the runtime with `install.sh --runtime-only`; the installed
  `~/.local/bin/claive` now has `claive config`.

## Revision: per-model efforts

The user asked that an effort be disabled per model, never for the whole engine.

- `disabled_reasoning_efforts` is removed. `disabled_model_efforts` maps a model to
  a list of efforts or `{effort: reason}`.
- `check_engine` refuses the effort for that model only. `claive config check`
  refuses an effort the model does not support, and a list that leaves the model no
  effort at all.
- The old key fails with the replacement in the message.
- `claive engines --json` reports `disabled_model_efforts`; the human line shows
  "no effort max for MODEL".

## Revision: per-model effort defaults

The user rejected one effort for every model. `default_reasoning_effort` is removed.

- `default_model_efforts: {model: effort}` replaces it.
- Resolution is flag, then role, then `default_model_efforts.<model>`, then the
  engine's declared default for that model.
- A null result passes no effort flag. Pi, Claude and Muse declare none. Codex and
  AGY keep per-model defaults.
- The old key fails with the replacement in the message.
- Host config: removed the engine default and the role efforts, and added
  `default_model_efforts: {deepseek-v4-1-flash-1017: max}`. The live turn still
  reports `max` for DeepSeek.

## Temporary overrides

The user calls "hybrid" the file plus flags that override it. `--set` was added
because `binary`, `env`, `prompt_limit`, `extra_args`, `budget`, `user_config`, the
provider policy, the disabled policy and the launch timeouts had no flag.

- `config.set_overrides`, `overrides`, `parse_override` and `deep_merge`.
- `load()` merges the overlay over the file and validates the result.
- `--set` on `run`, `start` and `open`; the state records `config_overrides`.
- The supervisor and the worker-targeted commands apply the stored overrides.
- The launch prints the merged override to stderr.

Verification: focused tests pass. A live Pi turn with
`--set '{engines: {pi: {default_reasoning_effort: low}}}'` reported `low` in its
session line. A merged config that disabled the default model was refused with
"(with --set overrides)".

## Open

- The working tree still holds the earlier AGY and Pi multi-provider changes.
  Commit scope must be chosen before a push.
- A CLI that rewrites the file stays out of scope: it would destroy comments.
