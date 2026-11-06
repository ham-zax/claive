# Engine configuration

Every worker engine — `claude`, `codex`, `agy`, `muse`, `pi` — accepts the same
keys in the config file. A value the file does not set falls back to the engine's
declared default, which is the value Claive used to compile in. Nothing about an
engine's executable, provider, model, effort, environment or prompt limit is
unreachable from the file.

## The file

The config file is YAML or JSON. Claive looks for it in this order:

1. `$CLAIVE_CONFIG`, when set.
2. `config.yaml`, then `config.yml`, then `config.json` in
   `${XDG_CONFIG_HOME:-~/.config}/claive/`.

YAML is the preferred format. JSON is still accepted, so a host that already has
`config.json` needs no change. When two files exist, the first one in that order
wins and `claive config check` names the shadowed file.

```sh
claive config path      # the active file
claive config check     # parse and validate it
claive config show      # print it
claive config init      # write a commented YAML template with every key
claive config migrate   # convert config.json to config.yaml, keeping a backup
```

An empty or missing file means every declared default applies. YAML reads `on`,
`off`, `yes` and `no` as booleans, so quote a value that must stay a string. YAML
needs PyYAML; `claive config check` names the install command when it is missing.
JSON needs only the Python standard library.

## Temporary overrides

`--set` on `run`, `start` or `open` overrides any key for that worker without
editing the file:

```sh
claive run --set '{engines: {pi: {default_model_efforts: {deepseek-v4-1-flash-1017: low}}}}' ...
claive run --set '{engines: {codex: {disabled_model_efforts: {gpt-6.1-sol: [max]}}}}' ...
```

The value is a YAML or JSON object. It deep-merges over the file, a dedicated flag
still wins, and the worker record keeps it for follow-up turns. The file does not
change. An invalid merged config refuses the launch and names the override.

## The uniform keys

```yaml
engines:
  claude:
    binary: /opt/claude/bin/claude
    provider: bedrock
    env: {ANTHROPIC_LOG: debug}
    prompt_limit: 200000
    default_model: claude-sonnet-5-5
    default_model_efforts:
      claude-sonnet-5-5: high
    models:
      claude-sonnet-5-5: {efforts: [high, xhigh]}
    disabled_models:
      claude-opus-5-5: reserved for interactive work
    disabled_model_efforts:
      claude-sonnet-5-5: [xhigh]
    extra_args: ["--strict-mcp-config"]
    enabled: true
```

| key | type | meaning |
| --- | --- | --- |
| `binary` | absolute path | the engine executable |
| `provider` | nonempty name | the provider label the engine is launched with |
| `env` | `{NAME: value}` | extra child environment, merged over the engine's own |
| `prompt_limit` | positive integer | maximum prompt size in UTF-8 bytes |
| `default_model` | string | model when neither a flag nor a role chooses |
| `default_model_efforts` | `{model: effort}` | effort for that model when neither a flag nor a role chooses |
| `models` | `{model: {efforts: [...]}}` | allowlist; narrows, never widens |
| `extra_models` | `{model: {efforts: [...]}}` | models this host knows the engine serves beyond Claive's built-in catalog |
| `disabled_models` | list or `{name: reason}` | refuse these models |
| `disabled_model_efforts` | `{model: [effort] or {effort: reason}}` | refuse these efforts for that model only |
| `extra_args` | list of strings | appended to the command; owned flags refused |
| `fallback_models` | list of model names | models tried after a fallbackable failure; Pi accepts `provider/model` |
| `providers` | `{name: {enabled: bool, reason: str}}` | refuse a provider label; for Pi it also drives the provider list and the default |
| `enabled` | boolean | refuse the engine outright |
| `reason` | string | why it is disabled, shown to the parent |

`models` is a model allowlist: when the key is present, a model you do not list is
refused. `disabled_model_efforts` only removes efforts from a model and never
changes which models may run.

Three keys are engine-specific and refused on any other engine:

| key | engine | meaning |
| --- | --- | --- |
| `user_config` | `codex` | load the user's Codex configuration |
| `budget` | `claude` | the compaction budget |
| `default_provider` | `pi` | the provider a bare model resolves to |

## Disabling and enabling

Every switch is a config key. `claive config check` refuses a contradiction, such
as a default model or provider that the same block disables.

```yaml
engines:
  muse:
    enabled: false
    reason: host policy: Pi, AGY and Codex only
  pi:
    default_provider: mistral
    providers:
      mistral: {enabled: true}
      opencode2api: {enabled: false, reason: gateway retired}
    disabled_models:
      space-bunny-free: too slow
    disabled_model_efforts:
      mimo-v2.6-flash-free: [max]
  claude:
    provider: bedrock
    providers:
      bedrock: {enabled: false, reason: quota is for interactive use}
```

| switch | key | effect |
| --- | --- | --- |
| harness | `engines.<name>.enabled: false` | refuses every launch and follow-up |
| model | `engines.<name>.disabled_models` | refuses the model, with the optional reason |
| reasoning effort | `engines.<name>.disabled_model_efforts.<model>` | refuses the effort for that model only, with the optional reason |
| provider | `engines.<name>.providers.<label>.enabled: false` | refuses the provider label, with the optional reason |
| only some models | `engines.<name>.models` | refuses every model not listed |

`enabled: true` is the default and is accepted explicitly, so a file can show the
intent. A disabled harness, model, effort or provider is refused before the engine
starts, and the message names the file and the reason.

To disable an effort on one model, name that model and the effort:

```yaml
engines:
  codex:
    disabled_model_efforts:
      gpt-6.1-sol: [max]
      gpt-6.1-terra: {ultra: too slow}
```

Other models keep every effort. `claive config check` refuses an effort the model
does not support, and refuses a list that leaves the model no effort at all.
`default_model_efforts` must not name an effort that `disabled_model_efforts`
disables for the same model.

## Declared defaults

These are what apply when the file says nothing. `claive engine config` prints
them with `default` as the source.

| engine | binary | provider | model | effort | prompt limit |
| --- | --- | --- | --- | --- | --- |
| `claude` | `~/.local/bin/claude` | `anthropic` | `claude-haiku-5-5` | model default | none |
| `codex` | `~/.local/bin/codex` | `openai` | `gpt-6.1-terra` | per model | none |
| `agy` | `~/.local/bin/agy` | `antigravity` | `gemini-3.8-flash-high` | per model | 100000 |
| `muse` | `~/.local/bin/muse` | `meta` | `muse-spark-1.3-contributor` | model default | none |
| `pi` | `~/.local/bin/pi` | `default_provider` | engine default | model default | none |

A model default means Claive passes no effort flag and the engine uses its own
value for that model. `default_model_efforts` overrides it for one model. Codex and
AGY declare a per-model effort in their catalogs, so they always pass one.

Precedence for the binary and provider is: command-line flag, then the config
file, then the engine's `*_WORKER_BINARY` environment variable, then the built-in
path. The environment variables keep working, so an existing host needs no change.

## Fallbacks

`fallback_models` is the ordered list tried after a fallbackable failure: timeout,
quota, worker or protocol. A Pi entry may name `provider/model` to switch provider.
The command-line `--fallback-models` overrides the list.

```yaml
engines:
  pi:
    default_provider: opencode2api
    default_model: deepseek-v4.1-flash-free
    default_model_efforts:
      deepseek-v4.1-flash-free: max
      deepseek-v4-1-flash-1017: max
    fallback_models:
      - opencode2api/deepseek-v4.1-flash-free
      - opencode2api/deepseek-v4.1-flash-free
      - mistral/deepseek-v4-1-flash-1017
```

The first two entries retry the primary. The third switches to Mistral. `run` and
`start` use the list. Pi `open` workers ignore it; only AGY `open` workers support
fallbacks.

## Model tiers

Every engine has three tiers. `claive models [NAME] [--json]` prints them.

| tier | what it is | how it runs |
| --- | --- | --- |
| **default** | `engines.<name>.default_model`, else the engine's declared default | runs when no `--model` is given |
| **allowed** | `engines.<name>.models` when present, else the engine's declared catalog | runs freely, and can be rerun |
| **extra** | what the catalog knows but the policy does not allow | refused until you opt in |

An extra model is refused with the exact way to allow it:

```text
claive: model claude-opus-5-5 on worker engine claude is not in the models allowlist in
~/.config/claive/config.yaml (allowed: claude-haiku-5-5); pass --allow-model claude-opus-5-5
to run it once, or add it to engines.claude.models to allow it permanently
```

`--allow-model MODEL` is repeatable and applies to `run`, `start` and `open`. The
permission is recorded on the launch, so a follow-up keeps it instead of being
refused. Add the model to `engines.<name>.models` to allow it permanently.

A model the engine's catalog has never heard of is refused the same way, naming
`engines.<name>.extra_models` instead. That is how a host adopts a model released
after this Claive build without waiting for a code change:

```json
{"engines": {"claude": {"extra_models": {"claude-opus-6": {"efforts": ["high", "xhigh"]}}}}}
```

`extra_models` extends the catalog; `models` still narrows it. An engine whose
catalog is open (Pi takes any model) ignores `extra_models`, because there is
nothing to extend.

## Inspecting the result

```sh
claive engine config claude          # one engine, human readable
claive engine config --json          # every engine, machine readable
claive engines --json                # launch policy plus each engine's config block
```

Each value carries its source:

```text
claude:
  binary = /opt/claude/bin/claude (config, executable)
  default_model = claude-haiku-5-5 (default)
  env = {"ANTHROPIC_LOG": "debug"} (config)
  prompt_limit = 200000 (config)
  provider = bedrock (config)
```

`claive doctor` checks the configured binary, not the built-in path, so a host
that installs an engine elsewhere reports healthy once the file names it.

## Headless hosts

Every command above is non-interactive and needs no TTY. `--json` is stable for
scripting. Claive never prompts for credentials and never stores them: Pi keeps
`auth.json`, Claude and Codex keep their own stores. Configure an engine's
credentials with that engine's own tooling, then point Claive at the executable.

## Validation

An invalid value is a config error, and Claive then refuses every launch rather
than guessing. `claive config check` runs the same checks before a launch:

- The file must be a mapping. A duplicate YAML key is refused, not silently
  overwritten.
- `binary` must be absolute or start with `~`. A relative path is refused because
  a worker's `PATH` is not the parent's.
- `provider` must be nonempty and contain no spaces. `default_provider` must be a
  lowercase provider name.
- `provider` and `default_provider` must not name a provider that the same block
  disables.
- `default_model_efforts` must name an effort the model supports, must stay inside
  the `models` allowlist, and must not be disabled for that model.
- `env` names must be valid environment variable names, and values must be strings.
- `prompt_limit` must be a positive integer.
- `extra_args` may not contain a flag Claive owns for that engine.
