# Case study: TradingAgents on Claude, where turning caching on raised the bill

**Subject:** [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents)
v0.7.1 (`9fa1196`), every agent on `claude-sonnet-5`.
**Result:** as shipped, TradingAgents caches nothing on Claude (0% hit rate in
3 of 3 runs). The obvious one-line fix, Anthropic's automatic caching, made the
input bill **7.2% higher**, not lower. Caching only the agents that loop would
cut it by an estimated **9.4%**. tokenaudit now reports both problems.
Full data: [experiment results](../../experiments/tradingagents-claude/results.md).

## 1. Why Claude is different

The [first case study](tradingagents.md) ran TradingAgents on OpenAI, which
caches every prompt prefix automatically; the problem there was prompt order.
Anthropic caches nothing unless a request asks, either with a `cache_control`
breakpoint on a content block or with a top-level `cache_control` that turns on
automatic caching. TradingAgents does neither.

Three unmodified runs confirmed it: 0 cached tokens out of ~425k input. Every
analyst re-sends its whole growing tool-loop history at full price.

## 2. The obvious fix

[`automatic_caching.patch`](../../experiments/tradingagents-claude/automatic_caching.patch)
adds one line to TradingAgents' `AnthropicClient`: a top-level
`cache_control: {"type": "ephemeral"}` on every request. Automatic caching then
caches each prompt up to its end and reads it back on the next call that
extends it. No prompt text changes.

## 3. What happened

Six runs, alternating arms across three ticker/date pairs:

| | Unchanged (3 runs) | Automatic caching (3 runs) |
|---|---|---|
| Cache hit rate | 0% | 14.8% |
| Input written to cache | 0% | 85% |
| Writes never read back | n/a | 89% |
| Input cost | $0.849 | $0.910 (+7.2%) |

Every pair moved the same way (+9.1%, +7.7%, +4.9%).

The traces show where it goes. The analysts run tool loops, and there caching
works as intended: each follow-up call reads back the previous one, which is
all of the 14.8%. Nearly everything else is a single prompt sent once: the
sentiment analyst, two researchers, the research manager, the trader, three
risk debaters, the portfolio manager. Automatic caching writes each of them at
1.25x the input price, and nothing ever reads them. Those writes outweigh what
the analysts' reads save.

## 4. What tokenaudit reports

| Arm | Finding | Per run |
|---|---|---|
| Unchanged | `missing_cache_markers`: 4 to 7 requests re-send a prefix an earlier request sent minutes before, and nothing asks Claude to cache it | ~$0.016 to $0.044 |
| Automatic caching | `unread_cache_writes` (HIGH): 12 to 13 requests write ~105k tokens nothing reads back; "caching cost this session more than it saved" | ~$0.053 |
| Both | `misordered_shared_context`: 7 debate prompts re-send ~27k to 30k tokens of analyst reports behind different role text | up to ~$0.068 |

This data improved the tool before it went into this write-up. tokenaudit
0.4.0 reported the patched runs as clean apart from the debate ordering, and
sized the unchanged runs' repeats at 2k to 5k tokens per run while the patched
runs read back 19k to 24k. Two fixes came out of it:

- **`unread_cache_writes`** is new. A cache write only pays off when something
  reads it, and caching every request writes the one-off prompts too. It
  defers to the detectors that explain a missed read (drifting prefix, expired
  entry, changed tools), so it only reports writes nothing tried to reuse.
- **`missing_cache_markers` counts tool definitions**, which sit at the very
  front of an Anthropic prompt, and sizes each repeated prefix from the earlier
  request's reported tokens. These prompts are full of numbers and JSON, about
  2 characters per token, so the usual 4-characters-per-token estimate halved
  them.

## 5. A better fix

The fix `unread_cache_writes` points to: cache the agents that loop, and send
the one-off prompts plain. Repricing the same patched traces that way:

| Config | Unchanged | Automatic caching | Loops only (estimate) |
|---|---|---|---|
| NVDA 2026-08-14 | $0.277 | $0.302 | $0.256 (-7.7%) |
| AAPL 2026-08-14 | $0.277 | $0.298 | $0.252 (-8.9%) |
| NVDA 2026-08-21 | $0.296 | $0.310 | $0.262 (-11.4%) |
| **all** | **$0.849** | **$0.910** | **$0.770 (-9.4%)** |

That column is computed, not measured. The last call of each analyst loop
still writes a tail nothing reads; skipping those too would reach -11.6%, but a
client can't know which call is last. In TradingAgents the change belongs in
`AnthropicClient`: turn caching on for the analysts' tool-bound LLM rather than
for every call. The structured-output stages also bind a tool, so "has tools"
alone is not the right test.

The debate prompts could save more, but not with ordering alone. Each one is a
single content block, and Anthropic only matches cached prefixes at block
boundaries. Putting shared context first ([PR #1281](https://github.com/TauricResearch/TradingAgents/pull/1281))
helps on OpenAI; on Claude the shared reports also need their own block with a
breakpoint after it. That part is untested.

## 6. Prediction vs. data

The [prediction](../../experiments/tradingagents-claude/README.md) was written
before any run.

- Baseline at 0% with `missing_cache_markers` on every run: **right.**
- `misordered_shared_context` remaining after the patch: **right.**
- The patched read rate "well above the OpenAI baseline's ~10%" and, implied,
  a cheaper run: **wrong.** The read rate was 14.8%, and the bill went up.

## 7. Caveats

- Three pairs. Every pair agrees on direction, but the size of the effect is
  an estimate, not a precise figure.
- Input cost only, at the rulebook's Sonnet 5 rates (2026.11.10). Output tokens
  were 4.4% higher in the patched arm, which caching can't cause; it's
  run-to-run variation in how much the agents wrote.
- Decisions: Overweight in 5 of 6 runs, Hold in the patched AAPL run. The
  patch changes no prompt text, so this is sampling variation.
- The installed `anthropic` SDK (1.2.0) was too old for the OpenInference
  Anthropic instrumentor, so only the LangChain layer recorded. It carries
  input, cache read and cache write counts, blocks, tools and tool calls,
  checked against a mocked call before the analysis.
- Yahoo news, Reddit and FRED were partly unavailable during the runs.
  TradingAgents fell back or skipped them in both arms alike.

## 8. Reproduce

    export ANTHROPIC_API_KEY=...
    bash experiments/tradingagents-claude/run.sh
    python experiments/tradingagents-claude/analyze.py

The traces from these six runs are in
[`experiments/tradingagents-claude/runs/`](../../experiments/tradingagents-claude/runs/)
(API keys redacted; like the first case study's traces, the spans include the
prompts TradingAgents generated).
