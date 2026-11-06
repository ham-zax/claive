# Intent

The user asked whether every switch is reachable from one config file, preferably
YAML: disable a harness, a model, a reasoning effort, a provider, and enable them
again. If not, write a plan and start.

Answer: the switches existed, but only in JSON, and the provider switch was Pi-only.
The plan is `docs/aegis/plans/2026-11-06-yaml-config.md`.

Scope: the file format, a uniform provider policy, the `claive config` commands,
tests and docs.

Not in scope: a CLI that rewrites the file. A rewrite would destroy comments and
ordering, so the file stays the source of truth and `config init` supplies the
commented template.
