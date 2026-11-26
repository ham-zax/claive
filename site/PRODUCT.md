# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Stack

Static HTML/CSS (`site/index.html`) paired with a zero-framework Web Components bundle (`site/src/app.js` -> `site/assets/app.js`) built on Vercel UI SDK (`@ui-sdk/web` + `@ui-sdk/core`), including its `createAgent()` / WebMCP bridge (`window.claiveAgent`). Works offline and on any static host with no runtime CDN dependency.

## Users

Developers and multi-agent power users who run coding harnesses (Codex, Claude Code, Pi, AGY, Muse) and want to delegate, inspect, steer, verify, and integrate subagent work cleanly.

## Product Purpose

claive is a harness-neutral outer cloth and shared rope that wraps Codex, Claude Code, Pi, AGY, and Muse so any parent harness can drive, steer, question, verify, and connect any other harness as a subagent without stripping native tools, MCP servers, skills, or prompt caching.

## Core Capabilities Represented on the Site

1. **Multi-Harness Workers & Messaging**: `open`, `run`, `followup`, `@alias` specialists, `send`, `ask`/`reply`, live `steer`, bounded cross-engine `conversation` rounds, `batch`, `mission`, `inbox`.
2. **Ratatui Triage Dashboard (`claive dash` / `claive-tui`)**: Calm Dark Cockpit Rust TUI, read-only by default, zero locks, `inotify`-driven, native incremental Rust `events.jsonl` tailing with secret redaction, 3-column wide split (`Workers | Activity | Briefing`), human `LAUNCHED` / `RUNTIME` columns, `--actions` with dual-driver hijack confirmation, and 5-row `--compact` tmux pane.
3. **Isolation & Evidence Gates**: Managed Git worktrees (`--worktree`, `--carry-dirty`, `--link-deps`), `systemd-run --user --scope` + subreaper containment, `claive-memcap`, atomic zero-shell `status.json`, and `claive verify` / `claive worktree apply --dry-run`.
4. **Flat-Schema MCP Stdio Server (`claive-mcp`)**: 10 scalar-schema tools for zero-shell orchestration.
5. **Verified Orchestration (`claive-orch`)**: Plain-code arbiter stepping L0 Direct -> L1 Cross-Family Refine (Git revert on regressions) -> L2 Selective Breadth on stall.
6. **Unattended Goal Queue (`claive serve` / `claive goal`)**: Durable server queue with budgets, workspace allowlists, backoff, and `systemd` user unit.

## Interactive Architecture (`@ui-sdk/web`)

- `<ui-listbox>`: Workers list with arrow-key roving focus and typeahead.
- `<ui-tabs>`: Cockpit Inspector tabs, 5-harness adapter switcher, and 6-tab quickstart.
- `<ui-segmented-control>`: Sliding-indicator Activity filter (`Actions / Milestones / All`) and Log view switcher (`Summary / Prompt / Stderr / Supervisor`).
- `<ui-accordion multiple>`: Expandable paired action -> outcome rows.
- `<ui-toggle>`: Theme toggle and `[t] Actionable only` filter toggle.
- `<ui-dialog>` + `<ui-button data-agent-confirm>`: Worktree apply dry-run preview modal with human-in-the-loop agent confirmation gate.
- `createAgent()` + `registerTools()`: Live agent snapshot/act/query bridge exposed on `window.claiveAgent` and WebMCP.

## Accessibility & Honesty

- Responsive from 375 px phone width to 1600 px desktop with zero horizontal overflow.
- Light and dark themes, respects `prefers-reduced-motion`, full WAI-ARIA + keyboard navigation via `@ui-sdk/web`.
- Every CLI command, flag, engine detail, and MCP tool traces directly to repo files; no fabricated benchmarks (TTC experiment explicitly noted as deferred per `docs/experiment/README.md`).
