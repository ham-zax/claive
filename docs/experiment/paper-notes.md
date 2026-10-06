# Paper notes: *Refining Over Resampling*

Bilal et al., *Refining Over Resampling: Test-Time Self-Correction for LLM
Reasoning*, arXiv [2608.05643](https://arxiv.org/abs/2608.05643). These notes
are a summary for this experiment, not a substitute for the paper. Check the
numbers against the PDF before citing them elsewhere.

## Method

- Sample N independent rollouts (N = 8, temperature τ = 0.7).
- Refine each rollout through D rounds (D = 4) of continuation → self-critique →
  self-correction. The same model plays each role, with role-conditioned prompts.
- Aggregate the refined final answers by plurality vote. There is no external
  verifier or reward model.
- Domain: math reasoning benchmarks with closed-form answers (AIME-24, AIME-25,
  AMC23, MATH500), with small open models (Qwen2.5 family, 1.5B and 7B).

## Findings this experiment uses

| Finding | Number | Consequence here |
|---|---|---|
| Resampling saturates | AIME-24 unique semantic clusters ≈1.2 at N=2 and ≈2.5 at N=32 | At most 2 candidates. The second must differ deliberately (engine, model family, or strategy). `claive-orch lane` rejects a second lane that differs only by sampling. |
| Refinement recovers wrong rollouts | Accuracy rises across refinement rounds | Depth is the first escalation: the L1 critique → correct → verify loop. |
| Explicit critique matters | Without the critique stage, Qwen2.5-1.5B drops on AIME25 from 6.67% to 0.0% and on MATH500 from 58.0% to 55.6% | The critic is a separate step with structured output (concrete defects with evidence). Arm B0 measures its value for coding. |
| Single-rollout correction is noisy | Models miss their own errors or make answers worse; voting suppresses the noise | With N ≤ 2 there is no vote, so verifier plus checkpoint revert suppresses the noise instead. The critic comes from a different model family than the implementer. |
| No monotonic best (N, D) | AMC23 scores the same at (5,4) and (10,4) | N and D are configuration. The default is D = 2, with a maximum of 3. |
| Gains shrink as models get stronger | η ≈ 17 points per 10³ TFLOPs for Qwen2.5-1.5B vs ≈1.16 for Qwen2.5-Math-7B (over RM@8) | Frontier agents may gain little. Every mechanism is compared against compute-matched resampling (arm R), and a null result is acceptable. |

## What does not transfer

- **Voting.** Code diffs are not votable answers. The executable verifier
  (tests, compiler, held-out checks) replaces the vote, and is stronger evidence.
- **Scale.** A paper rollout is one generation from a small model. Here a rollout
  is a whole agent session, so N ≤ 2 and D ≤ 3.
- **Same-model critique.** The paper critiques with the same model. Here the
  critic comes from a different family where possible, because an agent session
  carries its own blind spots. That is a deliberate deviation, and it can be tested
  against a same-family critic with `--allow-same-family`, which the run records.
- **Domain.** Whether any of this holds for multi-file coding is the question
  this experiment tests.

## Mapping to hypotheses

H1 (B > R at matched compute) is the paper's central claim transplanted to coding.
H3 (B > B0) is the critique ablation. H5/H6 (breadth only after a stall, diverse
not identical) follow from cluster saturation. H7 (evidence dominates opinion)
replaces voting with verification. See `00-rationale.md` for H1–H7.
