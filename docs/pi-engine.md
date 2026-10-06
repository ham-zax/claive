# Pi engine

The Pi adapter targets the locally verified Pi 1.0.0 JSON protocol. Muse remains
the default engine; selecting Pi never changes or falls back to another engine.

```sh
claive-worker --engine pi \
  --workspace /absolute/repository \
  --prompt-file /absolute/task.md \
  --provider opencode2api --model mimo-v2.6-flash-free --reasoning-effort max
```

Pi uses `opencode2api` and high thinking. The initial model is
`muse-spark-1.3-contributor-free`. When `--model` is omitted, new workers read
Pi's global `defaultModel` for this provider. An explicit `--model ID` is saved
after launch-option validation, so later agents inherit that selection, including
across worker registries and workspaces. A failed model request does not undo the
selection. Already-open workers keep their launch model; their follow-ups do not
reset the shared default. Settings are in `~/.pi/agent/settings.json`, or the directory
selected by `PI_CODING_AGENT_DIR`; unrelated settings are preserved. `PI_WORKER_BINARY`
selects an absolute executable; otherwise the adapter uses `~/.local/bin/pi`.
For now, `opencode2api` is the only accepted Pi provider; other providers are
rejected before launch or follow-up acceptance. Provider endpoints and credentials
remain in Pi's own configuration. Pi must already know the chosen model.
Thinking supports `off`, `minimal`,
`low`, `medium`, `high`, `xhigh`, and `max`; Pi may clamp it to model capabilities.

The existing `followup`, `effort`, `wait`, `close`, and `cancel` commands apply to
Pi workers. Unsupported effort or step-limit requests are rejected before queue
or policy changes. Errors discovered while constructing a queued command reject
that turn and keep the supervisor available.

## Options and isolation

- `--read-only` selects exactly `read,grep,find,ls`. Normal workers also enable
  `bash,edit,write`.
- Discovered extensions are disabled and project-local Pi resources are not
  approved. This makes the tool selection predictable. Context instructions
  such as AGENTS.md still load according to Pi's behavior.
- `--offline` disables Pi startup network operations, while model requests still
  reach the configured provider.
- Pi has no Muse filesystem/network sandbox. Tool selection and working directory
  do not create an OS isolation boundary.
- `--max-model-steps`, `--output-schema`, `--web`, and worktree-management options
  are rejected. Pi has no implicit step cap in this adapter. For an isolated lane,
  create a worktree separately and supply its path as `--workspace`.
- `--no-session-log` maps to Pi's `--no-session` for a single turn. Reusable workers
  still require retained sessions.

## Sessions and results

Each retained session has its own directory under the worker registry:
`sessions/pi/<session-id>`. Its absolute directory is persisted in `launch.session_dir`.
IDs may contain letters, numbers, `.`, `_`, and `-`, starting and ending with a
letter or number. A generated UUID is the default.

Pi 1.0.0 supports both `--session-id <id>` and `--session <path|id>`. The adapter
creates the exact ID on the first turn, then locates its session header and passes
the exact retained file with `--session` on follow-ups. Close/reopen with the same
worker registry, workspace and `--session-id` reuses that history. A session from
another workspace or duplicate files with the same ID are rejected.

Pi's `agent_end` can precede automatic retry or recovery. Only `agent_settled`
produces a normalized terminal result, using the authoritative final assistant
message. A final `stop` succeeds; error, abort, truncation, deferred work or a
missing answer outcome fail. This matters because Pi can exit zero in JSON mode
even when the final assistant message reports an error. The worker core continues
to require a successful process exit, terminal completion and valid JSONL.

`usage ID` reads persisted assistant-message counters without invoking Pi. It
counts those records once, excludes streaming snapshots, and keeps absent
counters unknown. Pi separates uncached input from cache reads/writes; the
adapter combines those into total input supplied for the generic cache ratio.
These totals describe retained session accounting, not current context size.

## Verification and captures

On 2026-10-03, Pi 1.0.0 successfully called the configured local `opencode2api`
gateway with `big-pickle`, resumed remembered information in a fresh process,
and used its read tool. A temporary local HTTP fixture established JSON-mode
failure with exit zero and SIGTERM cancellation with exit 143. No gateway or
global Pi configuration changes were required.

On 2026-10-04 Pi's `opencode2api` provider moved from the local gateway to the
ARM deployment (`https://89-168-87-96.sslip.io/v1` in `~/.pi/agent/models.json`).
The adapter needed no change because it passes only the provider name. On
2026-10-06 a read-only `claive run --engine pi` turn completed through that
server with `space-bunny-free`; no local gateway was listening.

`tests/fixtures/pi/*.jsonl` contains sanitized versions of those captured event
sequences. Prompts, identifiers, paths and message bodies are replaced; thinking
deltas are omitted. They retain the relevant native event order, completion
status and usage fields. Tests also cover retry settlement, tool warnings,
prompt delivery, retained-session reuse, unsupported-option rejection and
process-group cancellation through the worker manager.

```sh
python3 -m unittest discover -s tests -p 'test_*.py' -v
shellcheck install.sh bin/claive-worker bin/claive-codex
```
