# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Stack

Self-contained static HTML/CSS/JS in one file, `site/index.html`. No build step, no framework, zero external JavaScript dependencies.

## Users

Developers and multi-agent power users who run coding harnesses (Codex, Claude Code, Pi, AGY, Muse) and want to delegate, inspect, steer, verify, and integrate subagent work cleanly.

## Product Purpose

claive is a harness-neutral outer cloth and shared rope that wraps Codex, Claude Code, Pi, AGY, and Muse so any parent harness can drive, steer, question, verify, and connect any other harness as a subagent without stripping native tools, MCP servers, skills, or prompt caching.

## Core Capabilities Represented on the Site

1. **Multi-Harness Workers & Messaging**: `open`, `run`, `followup`, `@alias` specialists, `send`, `ask`/`reply`, live `steer`, bounded cross-engine `conversation` rounds, `batch`, `mission`, `inbox`.
2. **Ratatui Triage Dashboard (`claive dash` / `claive-tui`)**: Dark Cockpit Rust TUI, read-only by default, zero locks, `inotify`-driven, native incremental Rust `events.jsonl` tailing with secret redaction, 3-column wide split (`Workers | Activity | Briefing`), `--actions` with dual-driver hijack confirmation, and 5-row `--compact` tmux pane.
3. **Isolation & Evidence Gates**: Managed Git worktrees (`--worktree`, `--carry-dirty`, `--link-deps`), `systemd-run --user --scope` + subreaper containment, `claive-memcap`, atomic zero-shell `status.json`, and `claive verify` / `claive worktree apply --dry-run`.
4. **Flat-Schema MCP Stdio Server (`claive-mcp`)**: 10 scalar-schema tools for zero-shell orchestration.
5. **Verified Orchestration (`claive-orch`)**: Plain-code arbiter stepping L0 Direct -> L1 Cross-Family Refine (Git revert on regressions) -> L2 Selective Breadth on stall.
6. **Unattended Goal Queue (`claive serve` / `claive goal`)**: Durable server queue with budgets, workspace allowlists, backoff, and `systemd` user unit.

## Brand Commitments & Honesty

- Plain, grounded technical voice.
- Every CLI command, flag, engine detail, and MCP tool traces directly to repo files.
- No fabricated benchmarks, win rates, or cost-savings claims (TTC experiment is explicitly noted as deferred per `docs/experiment/README.md`).
- Zero personal paths, hostnames, or secrets anywhere in `site/index.html` or `site/PRODUCT.md`.

## Accessibility & Inclusion

- Responsive from 375 px phone width to 1600 px desktop with zero horizontal overflow.
- Light and dark themes (auto-detected via `prefers-color-scheme` with manual toggle).
- Respects `prefers-reduced-motion`, semantic landmarks, skip link, keyboard-accessible interactive tabs and controls.
