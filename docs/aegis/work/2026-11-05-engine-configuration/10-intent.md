# Uniform engine configuration

The user asks that every harness Claive already supports get the same consistent,
clean treatment Pi received: everything configurable through the file, usable in
headless mode, and every hard-coded value demoted to a documented default.

Snapshot: main at cd11da6 plus the uncommitted Pi multi-provider work. Host
oci-hunter-arm, aarch64. Existing uncommitted user changes remain out of scope:
`bin/claivelib/engine.py` (child_env_for_turn), `bin/claivelib/engines/codex.py`
(web adapter), `docs/design-worker-freedom.md`, `concepts_brief.md`,
`docs/chatgpt-web.md`, `docs/designs/`, `scratch/`, `tests/test_codex_web.py`.

## What was actually hard-coded

Each engine module owned its executable path, provider label, model catalog,
default effort, prompt limit and child environment as module constants. Only four
of the five read an environment variable for the binary, and none read the config
file. `doctor` and orchestration readiness called `module.default_binary()`
directly, so they tested the built-in path even when a host had configured
another one.

## Decisions

- One schema, five engines. `binary`, `provider`, `env` and `prompt_limit` join
  the existing uniform keys, validated identically for every engine.
- Each engine declares `defaults()`. That is the single place its compiled-in
  values live, and it is what `claive engine config` reports as `default`.
- Precedence is flag, then config, then `*_WORKER_BINARY`, then the built-in
  path. Existing hosts need no change.
- `config.engine_setting`, `config.engine_env` and `config.engine_effective` are
  the only resolution path. Engines no longer read `os.environ` for their binary.
- Readiness asks `engines.engine_binary`, so doctor and orchestration agree with
  the launch path.
- `claive engine config [NAME] [--json]` is the headless audit surface: read-only,
  non-interactive, and it reports each value's source.
- Engine-specific keys stay engine-specific. `ENGINE_ONLY_KEYS` still refuses
  `budget` on anything but Claude, `user_config` on anything but Codex, and
  `providers`/`default_provider` on anything but Pi.

Change necessity: the Pi work proved the pattern, and leaving four engines
unreachable from the file would make the configuration surface inconsistent in
exactly the way the user objected to. Architecture review: no new state owner.
The config file remains the only policy source; each engine module remains the
only owner of its own defaults.

TDD route: off. Regression checks required, including the existing Codex web
assertions that pin `child_env_for_turn`, because that method is on the launch
path for every worker.
