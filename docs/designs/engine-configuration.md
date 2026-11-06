# Uniform engine configuration

Status: approved for implementation. Supersedes the per-engine ad-hoc defaults.

## Problem

Pi gained a real configuration surface: provider policy, a default provider, a
narrow writer, and a reporting command. The other four engines did not. Each one
still hard-codes its executable path, provider label, model catalog, default
effort, prompt limit, and child environment inside its module. Three consequences:

1. **Unreachable settings.** A host that installs Claude somewhere other than
   `~/.local/bin/claude` can only change it through an environment variable, and
   only for the four engines that happen to read one. There is no file setting.
2. **Inconsistent surface.** `engines.pi.providers` exists; `engines.claude` has
   no equivalent for anything. A parent reading `claive engines --json` cannot
   tell which values came from the config and which are compiled in.
3. **Stale readiness.** `doctor` and orchestration readiness call
   `module.default_binary()` directly, so they check the built-in path even when
   the config points elsewhere. A correctly configured host reports unhealthy.

## Decision

One schema, five engines. Every value Claive hard-codes for an engine becomes a
declared default that `config.json` can override, resolved through one function
and reported with its source.

### Uniform keys

Accepted by every engine, validated the same way:

| key | type | meaning |
| --- | --- | --- |
| `enabled` | bool | refuse the engine outright |
| `reason` | str | why it is disabled, shown to the parent |
| `binary` | absolute path | the engine executable |
| `provider` | nonempty str | the provider label the engine is launched with |
| `env` | `{name: value}` | extra child environment, merged over the engine's own |
| `prompt_limit` | positive int | maximum prompt size in UTF-8 bytes |
| `default_model` | str | model when neither a flag nor a role chooses |
| `default_model_efforts` | `{model: effort}` | effort for that model when neither a flag nor a role chooses |
| `disabled_models` | list or `{name: reason}` | refuse these models |
| `disabled_model_efforts` | `{model: [effort] or {effort: reason}}` | refuse these efforts for that model only |
| `models` | `{model: {efforts: [...]}}` | allowlist; narrows, never widens |
| `extra_models` | `{model: {efforts: [...]}}` | models this host knows the engine serves beyond Claive's built-in catalog |
| `extra_args` | list of str | appended to the command; owned flags refused |

Engine-specific keys stay engine-specific, and the existing `ENGINE_ONLY_KEYS`
guard keeps reporting them precisely:

| key | engine | meaning |
| --- | --- | --- |
| `user_config` | codex | load the user's Codex configuration |
| `budget` | claude | the compaction budget |
| `providers` | pi | per-provider enable/disable policy |
| `default_provider` | pi | the provider a bare model resolves to |

### Declared defaults

Each engine module exposes `defaults()`, the single place its compiled-in values
live. These are the documented defaults, not hidden constants:

| engine | binary | provider | model | effort | prompt limit |
| --- | --- | --- | --- | --- | --- |
| claude | `~/.local/bin/claude` | `anthropic` | `claude-haiku-5-5` | `xhigh` | none |
| codex | `~/.local/bin/codex` | `openai` | `gpt-6.1-terra` | `xhigh` | none |
| agy | `~/.local/bin/agy` | `antigravity` | `gemini-3.8-flash-high` | per model | 100000 |
| muse | `~/.local/bin/muse` | `meta` | `muse-spark-1.3-contributor` | `xhigh` | none |
| pi | `~/.local/bin/pi` | `default_provider` | engine default | `max` | none |

The `*_WORKER_BINARY` environment variables keep working as the fallback inside
each engine's `default_binary()`, so existing hosts are unaffected. Precedence is
flag, then config, then environment, then built-in path.

### Model tiers

Three tiers, one rule each:

1. **Default** — `engines.<name>.default_model`, else the engine's declared
   default. Runs when no `--model` is given.
2. **Allowed** — `engines.<name>.models` when the config has an allowlist, else
   the engine's declared catalog. Runs freely and can be rerun.
3. **Extra** — what the catalog knows but the policy does not allow. Refused
   until the user opts in.

The refusal names both ways to opt in: `--allow-model MODEL` for one worker, or
an entry in `engines.<name>.models` for good. A model the catalog has never heard
of is refused the same way, naming `engines.<name>.extra_models`.

`--allow-model` is recorded on the launch record, so a follow-up keeps the
permission instead of being refused by the allowlist it was never in. That is why
`TurnRequest` carries `allowed_models`: every follow-up path rebuilds its request
from the launch, so the permission travels with the worker rather than with the
command line.

### Model catalogs

The built-in catalog is a default, not a ceiling. `extra_models` extends it, so a
host can use a model the engine released after this Claive build without waiting
for a code change. `models` still narrows the merged set, and the two never
conflict: `extra_models` says what the engine can serve, `models` says what this
host permits. Declaring a model in both is a config error.

An engine whose catalog is open (Pi takes any model) ignores `extra_models`,
because there is nothing to extend.

### Resolution

`config.engine_setting(name, key, default)` returns the configured value or the
declared default. `config.engine_env(name)` returns the configured child
environment. `config.engine_effective(name, engine)` returns every key with its
source, `config` or `default`, which is what the reporting surface prints.

Engines never read `os.environ` for their own binary again; they ask
`engine_setting`. Readiness checks ask the same function, so `doctor` and
orchestration agree with the launch path.

### Headless surface

Every command stays non-interactive and gains `--json`:

```sh
claive engines --json                 # every engine, effective config, sources
claive engine config claude           # one engine, human readable
claive engine config claude --json    # one engine, machine readable
```

`claive engine config` prints the effective value and its source for every key,
including the binary path and whether it exists and is executable. It never
prompts, never launches a worker, and never writes. A host can therefore audit
its whole engine configuration from a script.

### Compatibility

- A config with no `engines` block behaves exactly as before: every declared
  default equals the current constant.
- `*_WORKER_BINARY` keeps its current meaning.
- `extra_args` still refuses flags Claive owns.
- `engines.<name>.binary` must be absolute; a relative path is a config error,
  not a silent PATH lookup, because a worker's PATH is not the parent's.

### Non-goals

- Claive does not install, download, or authenticate engines. It configures the
  ones the host already has.
- Claive does not store credentials for any engine. Pi keeps `auth.json`; Claude
  and Codex keep their own stores.
- No new state owner. The config file remains the only policy source.
