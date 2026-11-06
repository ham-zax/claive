# ChatGPT Web in Claive (opt-in Codex model)

The `codex-chatgpt-web` project runs Codex against a local Responses proxy backed by a ChatGPT browser session. This is an **unofficial browser integration**, not the OpenAI API. Follow your account's terms, limits, policies, and approvals; do not use it to evade usage restrictions. The account's eligible models may differ from Claive's configured choices.

## Isolation

Claive's native Codex model path is unchanged. Only models in the `chatgpt-web` namespace get a different `CODEX_HOME` and load its user configuration. The alias `chatgpt-web` means `chatgpt-web/gpt-6-sol` (high effort by default). Supported named Web models and efforts are listed in `claive engines --json`. The Web path uses Codex workspace sandboxing by default; the native path retains its existing sandbox policy.

The local installation has these entry points:

- `codex` — normal, unchanged native Codex profile (`~/.codex`).
- `codex-chatgpt` — native Codex executable with an isolated Web profile.
- `claive-chatgpt-web` — the `codex-chatgpt-web` setup and doctor CLI, using the isolated profile.
- `codex-web-gpt` — the browser/launcher UI, using the same isolated profile.

Isolated directories (mode `0700`):

- `~/.local/share/claive/codex-web-home` — Web Codex configuration, authentication and task sessions.
- `~/.local/share/claive/chatgpt-web-core` — bridge configuration, browser state and service metadata.
- `~/.local/share/claive/chatgpt-web-launcher` — launcher-specific UI/browser profile.

For a different Codex profile, set `CLAIVE_CODEX_WEB_HOME` consistently for both the GUI/CLI setup and the Claive worker supervisor. Do **not** point it at `~/.codex`: the adapter rejects that to protect the native profile. Do not copy browser cookies or credentials between these profiles.

## Finish browser-backed setup

These steps require an actual GUI session (or remote desktop/X11 forwarding) and the account holder's interaction. A headless SSH shell with no `DISPLAY` is insufficient for ChatGPT sign-in.

1. Start `codex-web-gpt` from a GUI session on the ARM machine. Sign in to ChatGPT in its embedded browser. Use its Setup/Install into Codex workflow. Do not install into the primary `~/.codex`: this launcher is preconfigured for `~/.local/share/claive/codex-web-home`.
2. Choose **Browser-only** for the first smoke test. This can exercise model requests but **does not provide local shell/file tools**.
3. For a coding worker, complete the launcher's **Full harness** MCP setup, including an authorized OpenAI tunnel, the `Codex Native2` connector on ChatGPT, and the tool permissions. The connector needs explicit account-holder setup. Only approve tools and folders you intend to expose.
4. If Codex's separate Web profile requires Codex authentication for native-model passthrough, run `codex-chatgpt login` using the normal account login flow. The normal `~/.codex/auth.json` should not be copied automatically.
5. Run `claive-chatgpt-web doctor --json` and verify the bridge, browser, tunnel and connector are healthy before launching Web workers.

The GUI and bridge are optional for ordinary Codex; do not run the bridge during native-only work.

## Try models

Normal Codex remains unchanged:

```shell
codex -m gpt-6.1-terra
```

After the Web setup and MCP verification, the same Codex CLI can use the isolated profile:

```shell
codex-chatgpt -m chatgpt-web/gpt-6-sol
```

After the bridge doctor is healthy, Claive can select the new model:

```shell
claive open --engine codex --model chatgpt-web --workspace "$PWD" --prompt-file /absolute/path/to/task.md --read-only
```

For writing tasks, omit `--read-only` only when you have reviewed the model's tools, permissions, and workspace. Web models are deliberately sandboxed by default. An exceptional dangerous-full-access override requires `CLAIVE_CODEX_WEB_YOLO=1` and is not recommended.

When the Web model isn't configured, Claive refuses worker creation with an explicit setup error. Native Codex never depends on the local Web bridge.

## Diagnostics and rollback

- `claive-chatgpt-web doctor --json` reports bridge status, login and missing setup.
- `claive engines --json` lists the selectable models and reasoning-effort pairs.
- `codex --version` and `codex-chatgpt --version` should report the **same native Codex binary version**.
- Stopping the bridge affects only the opt-in Web profile. You can still run native `codex` and Claive's native Codex models.
- `codex-chatgpt-web route disconnect` should be invoked **only** under the isolated profile (`claive-chatgpt-web route disconnect`). It preserves your regular `~/.codex` configuration by design.

The native `~/.codex/config.toml` is not modified by these helper commands or by Claive model selection. The launcher setup does modify the separate Web profile's configuration and installs its model route and hooks there.
