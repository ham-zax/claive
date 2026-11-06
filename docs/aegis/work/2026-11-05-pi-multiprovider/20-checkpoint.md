# Current checkpoint

Implemented all four phases against the approved spec
(`docs/designs/pi-multiprovider.md`) and plan
(`docs/aegis/plans/2026-11-05-pi-multiprovider.md`).

- `provider.py` rewritten: `read_models`, `provider_names`, `overrides`,
  `override_url`, multi-provider `overlay_agent_dir`, defensive
  `catalog_models`, `auth_check`, `list_providers`, atomic `write_models`,
  `add_provider`, `remove_provider`, `APIS`.
- `config.py`: `ENGINE_KEYS`/`ENGINE_ONLY_KEYS`, provider-name validation,
  `pi_provider_policy`, `check_pi_provider`, `pi_default_provider`. Top-level
  `providers` accepts any name; non-loopback is still refused.
- `engines/pi.py`: the `opencode2api`-only refusal is retired. `validate_turn`
  consults policy; `resolve_launch` uses the resolved default provider.
- `cli.py`: `split_pi_model`, `pi_provider_rows`, Pi `providers` and
  `default_provider` in `engines --json`, advisory `pi:<name>` doctor checks,
  and the `pi provider list|add|remove|check` command group.
- `orchestration.py`: `pi_provider()` replaces four hardcoded `opencode2api`
  sites.
- Docs: new design spec and plan, `docs/pi-engine.md` providers section,
  README paragraph, `docs/command-reference.md` rows and `pi provider` section.

Focused verification is green: `test_pi_multiprovider` 35/35, and
`test_provider` + `test_pi_engine` + `test_pi_multiprovider` 47/47.

Live host checks pass: `pi provider list` reports deepseek, mistral, and
opencode2api with the default marked; a real Mistral turn returns
`LIVE MISTRAL OK`; `--model mistral/mistral-large-4` resolves identically; a
disabled provider is refused with its reason and exit 1; an unknown provider
check exits 1.

Open: the full suite is running. Do not edit source while it runs. Then
reinstall the runtime, since the installed `~/.local/bin/claive` predates the
`pi provider` command group.

Commit scope: this task's source, tests, docs, and work record only. Stage the
`cli.py` hunks that belong to this task and leave the user's turn-environment
edits unstaged. Do not push without explicit approval.
