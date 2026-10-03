# Muse prompt caching reference

Checked 2026-10-02 with installed Muse 1.4.2-R4684.1.

## Meta documentation

- [Prompt caching](https://dev.meta.ai/docs/prompt-caching): the service automatically matches leading token prefixes. Process lifetime is not a cache control. Entries can be evicted; keeping a worker idle cannot guarantee retention. Cache-key and Responses retention options exist at API level, but this Muse exec CLI exposes neither.
- [Cookbook](https://dev.meta.ai/docs/cookbook/prompt-caching): preserve stable instructions and put changing task details last. An optional API cache key should identify a shared prefix, rather than being randomized per worker session.
- [Token usage](https://dev.meta.ai/docs/token-counting#usage): read cached tokens as a subset of reported input. Usage is accounting data; cumulative input across model calls is not current context occupancy.

Khiip captures in Hamza's vault:

| Source | Capture ID | Markdown path under `/home/hamza/khiip-vault` |
| --- | --- | --- |
| Guide | `01M3YHF5EKPDQ2TK5HP2Q1DTA7` | `captures/web/prompt-caching.md` |
| Cookbook | `01M3YHF7F0XC08EG00AVPCZ6DF` | `captures/web/prompt-caching-2.md` |
| Usage | `01M3YHF8QQBE6VE8N158AZPJG0` | `captures/web/token-counting.md` |

The direct Open Web Search fetch retained usage/billing caveats that Khiip's extracted Markdown omitted. Prefer the original pages for those details.

## Local observations

- Two logged `muse exec --session-id UUID` calls preserve one durable session and both turns, even though each exec process exits. Live follow-up recalled a marker from the first turn without receiving it again.
- The reusable launcher keeps its supervisor in a managed Codex terminal, sequentially launching those calls. It is idle between turns and closes explicitly. This retains conversation history, not an open model connection.
- Live contributor model check: `high` first turn, `xhigh` follow-up, same session; 15,729 / 31,475 input tokens cached on turn one and 14,577 / 31,800 on turn two. Positive counts prove that Muse exec can receive cache hits across process restarts and this effort change. They do not guarantee future hit rates.
- `muse export --session UUID --out FILE --redacted` exposes `model_completed.usage` in session events. `codex-workers usage` sums those once, avoiding repeated goal-attribution records.
- `--session-id` requires retained logging. `--no-session-log` is incompatible with reusable workers; deliberately unlogged single turns omit a session ID.
- No discount percentage, billing amount, contributor quota impact, or guaranteed retention duration was established by this check.
