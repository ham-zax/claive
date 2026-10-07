# Product

<!-- impeccable:product-schema 1 -->

Source: answers recorded in `site/HANDOFF.md` section 9 (2026-10-07). Fields marked *(inferred)* were not stated by the user directly.

## Platform

web

## Stack

Plain static HTML/CSS/JS in one file, `site/index.html`. No build step, no framework. *(Decided in the handoff; the user asked to continue it.)*

## Users

Developers first, junior developers second. They use coding agents (Claude Code, Codex, or similar) and want to know what claive is without already knowing the jargon.

## Product Purpose

claive is a referee for AI coding agents. One agent hands a coding job to cheaper workers, a worker from a different model family critiques the result, and each change is kept only if the user's tests say it got better. It is also an experiment: it asks whether verifier-gated refinement beats a single run and compute-matched resampling. There are no results yet.

## Positioning

A plain-code arbiter (not a model) decides each next step, and the user's own tests, not another model's opinion, decide what is kept. A worse round is reverted automatically. The project publishes its baselines and its own null-result policy.

## Capabilities and Constraints

- Linux with Python 3. The default engine needs the Muse binary. Pi is the second engine. Stdlib only.
- Public repository, MIT licence: `https://github.com/ham-zax/claive`. Author: Hamza (`ham-zax`).
- Undecided: numeric gate thresholds and calibration-task counts. An agent is tuning them. Describe the gates in words only.

## Brand Commitments

Voice: plain, dry, honest about being an experiment. *(inferred from the handoff)*

## Evidence on Hand

One shakedown run (`bd886bb32632`): tests 6/7, critic named the defect, same session fixed it, 7/7, 245 s. One task, no repeats, not evidence. **No benchmarks, win rates, cost figures or local-model support exist; none may be shown.** Paper numbers are unverified and stay off the page.

## Product Principles

1. Say what it is in one plain sentence before anything else.
2. Never claim a result. The experiment has none yet.
3. Every number on the page traces to a file in the repo.
4. Gloss each term once (parent, worker, critic, verifier, arbiter), then use it.
5. The honest framing (an experiment, not a product) is the selling point.

## Accessibility & Inclusion

Phone width with no sideways scroll, light and dark, reduced motion respected, real text rather than images of text.
