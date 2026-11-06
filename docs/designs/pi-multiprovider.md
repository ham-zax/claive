# Pi multi-provider support

* **Author:** Ahmed Hamza
* **Date:** 2026-11-05
* **Status:** Approved 2026-11-05. Implementation follows this document.

## Intent

Pi is a multi-provider harness. `models.json` holds a `providers` map, Pi ships a
built-in catalog, `pi auth` reports per-provider readiness, and `--model
provider/id` selects across providers. Claive's adapter pins all of that to one
entry, `opencode2api`, in 23 places across five runtime modules.

The result is that a host with a working Mistral provider cannot use it:

```sh
pi auth check --provider mistral --json
{"status":"ready","provider":"mistral","authType":"api_key"}
pi -p "Reply with exactly: MISTRAL OK" --provider mistral --model mistral-large-4
# → MISTRAL OK, api "mistral-conversations", usage and cost reported
```

Claive refuses that same turn before launch. This design removes the pin without
turning Claive into a second provider configuration store.

The governing principle is the existing one: claive is a supervisor, not a second
sandbox. It starts, tracks, steers, verifies and records workers. Pi owns its
providers, credentials and wire protocols. Claive owns policy and reporting.

## Current state

Hardcoded sites, all verified in this checkout:

| site | what it pins |
|---|---|
| `engines/pi.py:113` | `provider or "opencode2api"` default |
| `engines/pi.py:138` | `validate_turn` rejects every other provider |
| `engines/pi.py:_settings` | writes `defaultProvider="opencode2api"` |
| `provider.py:24` | `PROVIDER = "opencode2api"` |
| `provider.py:read_models` | reads only that entry |
| `provider.py:override_url` | loopback override for that provider only |
| `provider.py:overlay_agent_dir` | overlay rewrites that entry only |
| `config.py:138` | `providers may only hold opencode2api` |
| `cli.py:2226,2240` | remembered model only if `defaultProvider == opencode2api` |
| `cli.py:2183` | quota fallback hardcoded to that provider |
| `cli.py:2350-2380` | doctor checks one provider |
| `orchestration.py:534,545,650,1886` | arbiter and critic always launch `--provider opencode2api` |

What Pi provides, and whether Claive can read it:

| capability | mechanism | machine-readable |
|---|---|---|
| multiple providers | `models.json` `providers` map | yes |
| built-in catalog | compiled in (mistral, deepseek, …) | only via `pi --list-models` table |
| provider/model identity | `--model provider/id` | yes |
| readiness | `pi auth check --provider X --json` | yes, clean JSON |
| credentials | `auth.json` + environment variables | per-provider file |
| default selection | `settings.json` `defaultProvider`/`defaultModel` | yes |
| add provider | edit `models.json` or an extension | no CLI; `pi config` is a TUI |
| disable provider | remove from `models.json` | no concept |

## Decision: ownership boundary

**Pi owns what exists. Claive owns what workers may use.**

- Pi remains the source of truth for provider definitions, endpoints, models and
  credentials. Claive never stores a credential and never invents a provider.
- Claive owns policy: which providers are enabled, why one is disabled, which
  models and efforts are allowed, and which provider is the default.
- Claive provides one narrow writer, `claive pi provider`, because Pi has no
  non-interactive way to add a provider and headless hosts cannot run the TUI.
  The writer edits `models.json` only, never `auth.json`.

Rejected alternatives:

- **Policy only.** Smallest change, but leaves headless hosts unable to add a
  provider without hand-editing JSON. The user asked for add and disable.
- **Claive owns provider config.** Extending `config.json` `providers` to full
  definitions duplicates Pi's schema, moves credentials next to Claive config,
  and drifts from Pi's own store. Rejected.

## Design

### Model identity

Pi accepts `--model provider/id`. Claive adopts that as the canonical Pi model
string.

- `provider/model` names both explicitly.
- A bare `model` resolves against the effective default provider.
- `--provider NAME` remains an explicit override. When both `--provider` and a
  `provider/model` are given and disagree, the turn is refused before launch.
- The resolved pair is persisted in `launch.provider` and `launch.model`, so a
  follow-up keeps the provider it started with.

`launch.model` stores the bare model id and `launch.provider` stores the provider,
matching the existing record shape. The `provider/model` form is accepted at the
CLI boundary and normalized before persistence.

### Provider policy

New config surface under the existing engine policy:

```json
{
  "engines": {
    "pi": {
      "default_provider": "mistral",
      "providers": {
        "opencode2api": {"enabled": false, "reason": "gateway retired"},
        "mistral": {"enabled": true}
      }
    }
  }
}
```

- `default_provider` selects the provider for a bare model. Absent, it falls back
  to Pi's `settings.json` `defaultProvider`, then to `opencode2api` for
  compatibility with existing records.
- `providers.<name>.enabled: false` refuses a launch or follow-up with the
  configured reason, in the same shape as `check_engine` refusals.
- `providers.<name>.reason` is optional and reported verbatim.
- A policy entry for a provider Pi does not know is accepted, because the
  provider may be added later. `doctor` reports it as not configured.

Validation rejects unknown keys, non-boolean `enabled`, and a non-string
`default_provider`.

### Provider discovery

Three sources, in order of authority:

1. `models.json` `providers` — user-defined providers. Authoritative.
2. `pi --list-models` — the only way to see Pi's built-in catalog. Parsed as a
   fixed-width table.
3. `pi auth check --provider NAME --json` — readiness.

Discovery is used by `claive engines`, `claive doctor`, and `claive pi provider
list`. It is **never** on the launch path: a launch validates the requested
provider against `models.json` and the policy, and lets Pi reject an unknown
built-in provider with its own error. Enumeration failure degrades reporting and
never blocks a turn.

The table parser is defensive: it requires the header row, tolerates extra
columns, and returns an empty list rather than raising when the shape is
unrecognized. A Pi upgrade that changes the table degrades reporting only.

### Provider commands

```sh
claive pi provider list
claive pi provider add mistral --base-url https://api.mistral.ai/v1 \
  --api openai-completions --api-key-env MISTRAL_API_KEY \
  --model mistral-large-4 --model devstral-latest
claive pi provider check mistral
claive pi provider remove mistral
```

Writer rules, all enforced:

- Atomic replace through a temporary file in the same directory, mode `0600`.
- A timestamped backup `models.json.bak-<epoch>` before the first write.
- Additive only: `add` refuses an existing provider without `--replace`.
- `remove` refuses a provider that is not in `models.json`. It does not gate
  built-in ids behind `--force`: it deletes the entry and then reports when Pi
  still serves that provider from its built-in catalog, so the operator learns
  the truth instead of being blocked. The write is reversible from the backup.
- `--api-key-env` stores `"$VAR"`, Pi's own environment-reference form. Claive
  never writes a literal key and never reads `auth.json`.
- `add` requires at least one `--model`, because a provider with no models is
  unusable and Pi would list nothing.
- Every write re-reads and re-validates the file first, so a concurrent Pi edit
  is not silently clobbered.

`check` reports auth readiness from `pi auth check --json` and, when the provider
has a `baseUrl`, an endpoint probe reusing the existing `provider.probe`.

### Loopback override

`config.json` `providers.<name>.base_url` generalizes from `opencode2api` to any
provider name. The loopback-only restriction stays. This is a redirect for a
provider Pi already knows, not a definition, so it does not overlap the writer.

The overlay agent directory rewrites only the overridden entries and links
everything else, preserving the existing self-loop guard.

### Orchestration routing

`orchestration.py` stops hardcoding `provider: "opencode2api"`. The arbiter and
critic resolve the effective default provider from policy, and their model
identity becomes `provider/model`. The pre-approved quota fallback keeps its
current model but resolves its provider the same way.

### Compatibility

- Existing configs with `providers.opencode2api.base_url` keep working unchanged.
- `--provider opencode2api` and bare `--model` keep working when the default
  provider is `opencode2api`.
- Existing worker records carry `launch.provider`; follow-ups keep using it.
- The `opencode2api`-only refusal is retired. `docs/pi-engine.md` and the README
  are updated in the same change.
- No migration is required: absent policy means the previous behavior.

## Failure modes

| condition | behavior |
|---|---|
| provider disabled by policy | refuse before launch, with the configured reason |
| provider unknown to Pi | refuse with the Pi error, or the policy hint when policy names it |
| provider not authenticated | refuse with `pi auth check --provider NAME` guidance |
| `--provider` disagrees with `provider/model` | refuse before launch |
| `models.json` unreadable | `doctor` warns; launch still attempts, Pi reports the real error |
| `pi --list-models` shape changed | enumeration returns empty; reporting degrades, turns still run |
| concurrent `models.json` edit | writer re-reads before writing; refuses on a changed file |

## Testing

- Policy validation: unknown keys, bad types, disabled provider refusal, reason text.
- Model identity: `provider/model`, bare model, conflicting `--provider`, persistence.
- Discovery: table parsing from a captured fixture, malformed table, empty catalog.
- Writer: atomic replace, `0600`, backup creation, `--replace` refusal, built-in
  refusal, environment-reference form, concurrent-change refusal.
- Doctor and `engines`: per-provider rows, disabled reason, readiness.
- Orchestration: default provider resolution, `provider/model` identity.
- Regression: every existing Pi test keeps passing with no policy configured.

## Non-goals

- No OAuth or login flow in Claive. Pi owns authentication.
- No credential storage or reading in Claive.
- No provider extensions or custom wire protocols.
- No automatic provider switching or failover between providers.
- No change to Pi's session, steering, fork or read-only behavior.

## Phases

| phase | scope | independently shippable |
|---|---|---|
| 1 | Lift the restriction, `provider/model` identity, validation | yes |
| 2 | Provider policy, `engines` and `doctor` reporting | yes |
| 3 | `claive pi provider list\|add\|remove\|check` and the safe writer | yes |
| 4 | Orchestration routing across providers | yes |
