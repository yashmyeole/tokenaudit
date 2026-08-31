This addresses the prompt-construction half of #750 (zero prompt cache hit
rate): dynamic values embedded in system prompts, and the debate agents'
f-string prompts that never share a prefix. It does not add a caching
layer — the prompts are restructured so the providers' built-in implicit
prefix caching works on its own.

**The change is a reorder, not a rewrite.** 9 files; the wording of every
instruction is preserved; only section order changes:

- Debate prompts (bull/bear researchers, three risk debaters): the shared
  context (analyst reports, trader decision) comes first, the append-only
  debate history second, and the role-specific instructions last. Within
  each debate group the leading block is byte-identical, so every turn
  extends the previous turn's prefix — the same shape that makes chat
  loops cache well.
- Analyst system templates: the volatile `Today's date is ...` /
  instrument sentence moves to the end of the system message, so analyst
  prefixes are byte-stable across trade dates and tickers (relevant for
  back-to-back runs and backtests).

**Measured effect** (controlled before/after, 3 matched config pairs on
gpt-5.4-mini, runs ≥15 min apart so one run's provider cache can't leak
into the next; design committed before the data was collected):

| Config | Baseline hit rate | Patched | Delta |
|---|---|---|---|
| NVDA 2026-08-14 | 9.7% | 28.4% | +18.8 pts |
| AAPL 2026-08-14 | 4.6% | 34.1% | +29.5 pts |
| NVDA 2026-08-21 | 8.3% | 34.6% | +26.3 pts |

Mean hit rate 7.5% → 32.4% (4.3×), roughly a 25% cut in input cost at
OpenAI's 0.1× cached-read pricing. The gain lands exactly where the
mechanism predicts: the second speaker in each debate (bear researcher,
conservative and neutral risk analysts) goes from 0 to ~6.9k cached tokens
per call. Two untraced control runs (no instrumentation at all, usage read
from response bodies) came in at 8.6% and 3.4%, in line with the traced
baselines and nowhere near the patched arm — the measurement setup isn't
producing the effect. Full experiment — traces, analysis code, prediction-before-data
history, threats to validity:
https://github.com/yashmyeole/tokenaudit/tree/main/experiments/tradingagents-cache-fix
(write-up: https://github.com/yashmyeole/tokenaudit/blob/main/docs/case-studies/tradingagents.md).

**What this deliberately does not do** (possible follow-ups): no
`prompt_cache_key` routing hints (kept out to measure one variable), no
changes to the manager/trader prompts, and no application-level caching
layer from #750's first suggestion. Analysts' first calls also remain
below the 2048-token cacheable minimum — more headroom exists.

**On behavior:** the models see the same information in a different order
(context before role). Decisions from all experiment runs are captured
alongside the traces; this PR measures cost, not decision quality — happy
to run any eval the maintainers would find convincing, or to split the
analyst and debate changes into separate PRs if that's easier to review.
