# Plan: temporary config overrides with `--set`

## Question

The user calls "hybrid" the combination of a config file and flags that override it
for one launch. Today the dedicated flags cover engine, model, effort, provider,
role, team, read-only, web, isolation and timeouts. They do not cover `binary`,
`env`, `prompt_limit`, `extra_args`, `budget`, `user_config`, the provider policy,
the disabled policy or the launch timeouts.

## Design

`--set 'YAML'` (repeatable) takes a partial config object and deep-merges it over the
file for the life of the worker. Dedicated flags still win over `--set`.

- `config.set_overrides(*layers)` stores the merged overlay in the process.
- `config.load()` merges the overlay over the parsed file, then runs the same
  validation. An invalid merged config fails the command and names the override.
- `config.parse_override(text)` parses one value as YAML or JSON and requires an
  object.
- The launch record stores `config_overrides`, and the supervisor applies them at
  startup, so follow-up turns keep the same policy.
- The CLI prints the merged override to stderr, so the launch is auditable.

## Scope

Launch commands `run`, `start` and `open`. Not `followup`: the running supervisor
already carries the launch overrides. Not `serve`: it spawns separate workers.

## Tests

- Parse and merge, including nested keys and list replacement.
- `load()` sees the overlay, and validation still refuses a bad merged config.
- A launch with `--set` refuses a model the overlay disables, and the same launch
  without `--set` works.
- A dedicated flag wins over `--set`.
- The override is not written to the file.
- A follow-up keeps the launch override.

## Risks

- `config.load()` runs on every policy check. The overlay must be process-local and
  must not leak into a later command. Each CLI command is a new process, so the
  lifetime is one command plus its supervisor.
- The workspace allowlist and the disabled-engine policy become overridable. That is
  the requested behaviour: the parent is the operator, and the override is visible
  in the launch output and the state record.
