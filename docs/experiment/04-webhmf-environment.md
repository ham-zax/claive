# 04: Environment discovery (this workstation)

Phase 0.1 of `02-implementation-plan.md`. These are non-secret facts observed on
2026-10-06 on the user's WSL2 workstation (Ubuntu, about 7 GB RAM shared with
editors and other agents). Provider endpoints, keys, and auth caches are
deliberately not recorded. Re-check versions before each comparison batch and
copy them into the batch's notes.

## Versions

| Tool | Version |
|---|---|
| Muse Code (`muse`) | 1.4.3 (1.4.3-R5018.1) |
| Pi (`pi`) | 1.0.3 |
| Codex CLI (`codex`) | 0.160.0 (parent) |
| Claude Code (`claude`) | 2.1.291 (parent) |
| Python | 3.14.4 (`claive-orch` and `claive` use only the standard library) |
| Git | 2.53.0 |
| Node.js | v24.19.0 (not needed by this implementation) |

OpenCode was not evaluated as a worker. Pi reaches the OpenCode-compatible
models through the `opencode2api` provider instead.

## Integration surface

The original design's adapter layer is the existing `claive` manager. It
already normalises engine event streams into one job record, so it has a single
contract for every engine:

- `open` starts a reusable worker. Its first output line is `Worker <id> | label | path`.
- `wait ID` blocks until the turn ends. Exit 0 means completed, 130 cancelled, and anything else failed.
- `followup ID --prompt-file F` continues the **same session**. This is the
  session continuation that L1 correction needs.
- `close`, `cancel`, `logs`, `show --json`, `usage --json`.
- State lives under `${CLAIVE_DIR:-$XDG_STATE_HOME/claive}/<id>/`.

Engines are plugged in under `bin/claivelib/engines/`: `muse` (default) and
`pi`. Their full contracts are in the repository `README.md` and `docs/pi-engine.md`.

## Engines

### Muse (implementer workhorse)

- Pinned model `muse-spark-1.3-contributor`; a different `--model` is rejected.
  Default reasoning effort is high and the default step cap is 100 (`--max-model-steps`).
- Read-only mode maps to `--disable-write --disable-shell`.
- Supports `--output-schema` (structured final answer) and `--web`.
- Has a sandbox, and network access goes through its proxy only.
- `--worktree` creates a temporary worktree that is removed when the turn exits.
  `--worktree-existing ABS` runs in an existing worktree. `claive-orch` creates
  lane worktrees itself, so a lane worker uses `--workspace <lane path>`.
- Observed behaviour: follows instructions closely and is a reliable implementer.
  As a critic of its own family it adds little, so the critic is never muse-spark.
- Quota: Muse has a five-hour quota. Quota exhaustion is reported, and falling back
  to another engine needs the user's approval (see `skills/subagent-routing`).

### Pi through `opencode2api` (critics, diverse candidates)

Model IDs configured on 2026-10-06 (from `~/.pi/agent/models.json`, IDs only):

| Model ID | Family (as `claive-orch` computes it) |
|---|---|
| `big-pickle` | big-pickle |
| `ling-3.1-flash-free` | ling |
| `longcat-2.5-preview-free` | longcat |
| `mimo-v2.6-flash-free` | mimo |
| `muse-spark-1.3-contributor-free` | muse-spark (same family as the Muse engine) |
| `muse-spark-1.3-free` | muse-spark (same family as the Muse engine) |
| `nemotron-3-ultra-free` | nemotron |
| `nemotron-3.5-lightning-free` | nemotron |
| `space-bunny-free` | space-bunny (currently Pi's global default) |

There are seven families. Several Pi workers can run at once on different
models, limited by gateway rate limits and local RAM. Each one is a separate
`pi` process.

Constraints that shape the skills:

- **Always pass `--model`.** An explicit `--model` also overwrites Pi's global
  `defaultModel`, which every later Pi worker inherits. Omitting it gives
  whatever model was last selected.
- No `--max-model-steps`, `--output-schema`, `--web`, or worktree flags. Use
  `--workspace <lane path>` for isolation.
- `--read-only` gives the tools `read,grep,find,ls` and **no bash**. A Pi critic
  cannot run `git diff` or the tests, so `claive-orch prompt ... critique` pastes
  the diff and the verifier output into the prompt.
- There is no structured-output flag. Critics are asked for one fenced JSON block,
  and `claive-orch critique` tolerates malformed output: it records an error and
  treats the answer as "no concrete defect".
- No OS sandbox. Tool selection and working directory are not an isolation boundary.
- The provider is a remote gateway. Free models can rate-limit or disappear. Record
  failures. Do not silently switch the model inside a comparison.

## Usage reporting

`claive usage ID --json` returns `model_calls`,
`totals{input_tokens, cached_tokens, output_tokens}` and `cache_hit_ratio`, from
persisted session records, for both engines. Missing counters stay unknown and
are never reported as zero. There is no dollar cost. **The compute unit for
matching is therefore total tokens (input + output), with wall-clock as a
secondary unit** (see `05-experiment-protocol.md`). `claive-orch usage RUN`
snapshots every worker registered with a run.

## Nested subagents

- Pi workers get only the built-in tools (`bash,edit,write` plus read tools) with
  extensions disabled, so they have no subagent tool.
- Muse: whether nested delegation can occur is not verified. Worker prompts from
  `claive-orch` forbid delegation, and the run report records only the workers
  registered with the run.

## Jev

No Jev API, SDK, or credentials are available here. Arms C and E are therefore
not implemented (see `06-skill-driven-implementation.md`).

## Workstation constraints

- About 7 GB RAM: run verifiers one at a time and keep at most two or three
  concurrent workers. Every concurrent worker runs its own process.
- No passwordless sudo.
- Keep corpus repositories on the Linux filesystem, not `/mnt/c`.
