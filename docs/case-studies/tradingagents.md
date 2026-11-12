# Case study: TradingAgents — 4.3× cache hit rate from reordering prompts

**Subject:** [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents)
(~98k★ multi-agent trading framework), commit `a33fd4c`, gpt-5.4-mini.
**Reported problem:** [issue #750](https://github.com/TauricResearch/TradingAgents/issues/750)
— near-zero prompt cache hit rate, an estimated 30–40% cost overhead.
**Result:** mean cache hit rate **7.5% → 32.4% (4.3×)**, mean input-bill
reduction **~24.6%**, from a patch that reorders prompt sections and changes
no wording. Full data: [experiment results](../../experiments/tradingagents-cache-fix/results.md).

Everything below is reproducible from this repository: the captured traces,
the diagnosis, the patch, and the experiment that measured it.

## 1. Diagnosis: where the money actually went

tokenaudit's posture is zero-integration: TradingAgents' code was not
modified to be diagnosed. Three unmodified runs were captured with
process-level OpenTelemetry instrumentation
([corpus/captured/](../../corpus/captured/)) and read back with
`tokenaudit diagnose`. Combined cache read rate: **~10.6%** (9.3% / 3.1% /
18.1% per run — run-to-run spread that matters later).

The per-request anatomy, from block digests and usage accounting alone (the
traces hold hashes and token counts, never prompt text):

- The four **tool-loop analysts cache fine.** Their second and third calls
  extend their own growing message history, and cached reads appear —
  nearly all of the 10.6% comes from here.
- The eight **single-shot debate prompts** — bull/bear researchers,
  research manager, trader, three risk debaters, portfolio manager — carry
  **~59k of run-01's 82.5k input tokens and receive zero cached reads.**
  Each stuffs the *same* four analyst reports (and, in the risk debate, the
  same trader plan and growing history) behind a *different opening role
  paragraph*. Prefix caches match from byte zero: shared content behind
  different openings is worth nothing.
- **Across runs**, even for the same ticker one week apart, only **1 of
  ~12** distinct system blocks is byte-identical. The four analyst system
  blocks have the *same length* but different hashes across runs — the
  fingerprint of an injected trade date (`"Today's date is {current_date}"`
  sits mid-prefix in all four analyst templates).

This maps directly onto two of the five causes hypothesized in #750:
dynamic values embedded in system prompts, and debate agents building
unique f-string prompts. The anatomy above turns those hypotheses into
measured, per-request attributions.

## 2. The fix: same words, different order

[`cache_friendly_prompts.patch`](../../experiments/tradingagents-cache-fix/cache_friendly_prompts.patch)
touches 9 files and preserves the wording of every instruction; only
section order changes.

- **Debate prompts** (bull/bear researchers, three risk debaters) become:
  shared reports (+ trader decision) first, append-only debate history
  second, role instructions and the opponent's last argument **last**. The
  shared block is byte-identical within each debate group — verified by
  hash at patch time — so each debate turn extends the previous turn's
  prefix exactly like a chat loop.
- **Analyst system templates**: the volatile date/instrument sentence moves
  from the middle to the end, so analyst prefixes are byte-stable across
  trade dates and tickers (a cross-run/backtest benefit; little within-run
  gain was predicted from this part, and none was claimed).

## 3. The experiment: prediction registered before data

The design, hypothesis, and predicted effect were committed **before** any
experiment run — the git history shows the
[experiment README](../../experiments/tradingagents-cache-fix/README.md)
predicting a hit rate of roughly **30–35%** (from ~10%) and ~20% input-bill
reduction, attributing the gain to three specific requests: the bear
researcher and the conservative and neutral risk debaters, each expected to
gain ~5.5–7.5k cached tokens.

Design, in brief: 6 traced runs (3 matched baseline/patched pairs, arm
order alternating), ≥15 minutes apart so gpt-5.4-mini's implicit cache
(~5–10 idle minutes) cannot carry one run's prefixes into the next, plus
untraced control runs measuring whether observation itself moves the
number. Threats to validity are listed in the experiment README and are not
repeated here — read them before quoting numbers.

## 4. Results

| Config | Baseline hit | Patched hit | Delta |
|---|---|---|---|
| NVDA 2026-08-14 | 9.7% | 28.4% | +18.8 pts |
| AAPL 2026-08-14 | 4.6% | 34.1% | +29.5 pts |
| NVDA 2026-08-21 | 8.3% | 34.6% | +26.3 pts |

Mean **7.5% → 32.4% (4.3×)**; mean input-bill reduction **~24.6%** (extra
cached tokens × 0.9, the cached-read discount, over baseline input).

The attribution matches the registered prediction request-for-request: in
each patched run, the three predicted beneficiaries went from **0 to 6,912
cached tokens each** (predicted 5.5–7.5k), while the research manager —
deliberately left out of the patch — stayed at zero. When a mechanism
predicts not just the direction but *which requests* move and by roughly
how much, the mechanism is doing the explaining, not run-to-run luck.

**Tracing did not manufacture this.** Both arms are traced, so
instrumentation cancels in the comparison — but two untraced control runs
(baseline code, no OpenTelemetry, usage read passively from response
bodies) checked that directly: **8.6% and 3.4%**, against traced baselines
of 9.7% / 4.6% / 8.3%. The traced-vs-untraced difference of means is 1.5
points — smaller than the baseline arm's own run-to-run spread, and an
order of magnitude below the patch's +24.9. No untraced run comes within 20
points of the worst patched run. With n=2 that is a consistency check, not
a proof of exact neutrality, and it is reported as such.

Two honest footnotes. First, the structural "shared system-block hashes
across runs" metric did not move (6/39 both arms): whole-block hashes
cannot see that the volatile date moved *within* a block — the block still
hashes differently across dates. The within-run billing gains above are the
evidence; the block-level metric is simply the wrong instrument for that
part of the fix, and is reported anyway rather than quietly dropped.
Second, remaining headroom is real: the analysts' first calls sit below
gpt-5.4-mini's 2048-token cacheable minimum, the managers were left
unpatched, and `prompt_cache_key` routing hints were deliberately excluded
to keep one variable — all follow-up levers, not part of the measured
claim.

## 5. Reproduce it

    # diagnose the committed baseline traces (no API key needed)
    git clone https://github.com/yashmyeole/tokenaudit && cd tokenaudit
    pip install -e .
    tokenaudit diagnose corpus/captured/runs

    # rerun the whole before/after experiment (~$1 of gpt-5.4-mini, ~2h)
    export OPENAI_API_KEY=...
    bash experiments/tradingagents-cache-fix/run_experiment.sh
    python experiments/tradingagents-cache-fix/analyze.py

The committed [runs](../../experiments/tradingagents-cache-fix/runs/) are
redacted (block hashes and token counts, no prompt text) and
[results.md](../../experiments/tradingagents-cache-fix/results.md) is
regenerated from them by `analyze.py`.

## 6. What tokenaudit reports now

The diagnosis in section 1 was done by hand. Whole-message hashes can't see
the same reports inside two different messages, so up to 0.2 `tokenaudit
diagnose` called these runs clean, and the committed CI baseline gave them a
100% health score. Ingest now also keeps a digest per line (in memory only),
and two detectors use it.

`misordered_shared_context` flags 6 requests in each captured run: the bear
researcher, the research manager, all three risk debaters and the portfolio
manager, about 22-26k full-price-equivalent tokens per run. On the patched
experiment runs it flags 3, the research manager, trader and portfolio
manager, which the patch deliberately left alone (about 5-6k).

`cross_session_drift` flags the market analyst's system prompt: its first
line holds the date and ticker, and the ~1,100 tokens after it are identical
in every run. It fires on the baseline arm and not on the patched arm, which
moved the date to the end. The three captured runs were recorded back to
back, about 2.5 minutes apart, so a shared cache entry would have been warm,
and the finding is rated high.

The traces didn't change; the old score couldn't see the waste. The health
floor for these runs is now 25%, and both results are pinned by tests in
`tests/test_detect.py`.
