# Plan: per-model effort defaults, no engine-wide default

## Problem

`engines.<name>.default_reasoning_effort` applies one effort to every model on the
engine. A flash model at `max` is not better than a frontier model at `medium`, so a
single value cannot be right. The model's own default should apply unless the user
configures that model.

## Design

- Remove `default_reasoning_effort`.
- Add `default_model_efforts: {model: effort}`.
- Resolution: `--reasoning-effort`, then the role, then
  `default_model_efforts.<model>`, then the engine's declared default for that model.
- A null result means no effort flag: the engine uses its own default.
- Built-in defaults: Codex and AGY keep per-model values. Pi, Claude and Muse
  declare none, because the provider knows the model better.
- The old key fails with the replacement in the message.

## Tests

- A per-model default applies to that model only.
- No configured default and no engine default omits the effort flag.
- A flag and a role still win.
- The old key names the replacement.
- Codex and AGY keep their per-model defaults.

## Host migration

Remove `engines.pi.default_reasoning_effort` and the role effort overrides, and add
`default_model_efforts: {deepseek-v4-1-flash-1017: max}`. The current DeepSeek
behaviour stays the same; other models use their own default.
