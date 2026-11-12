# TradingAgents on Claude: caching off vs. on

The first case study ran TradingAgents on OpenAI, which caches every prompt
prefix automatically. Anthropic doesn't: a request is cached only if it marks
a breakpoint with `cache_control` or sets `cache_control` at the top level
(automatic caching). TradingAgents does neither, so on Claude every repeated
token should be billed at full price.

This experiment measures that, and what one line changes.

## Arms

- **baseline**: TradingAgents at `9fa1196` (v0.7.1), unchanged.
- **patched**: the same commit plus [`automatic_caching.patch`](automatic_caching.patch),
  which adds a top-level `cache_control` to every request from
  `AnthropicClient`. Nothing else changes: no prompt is reordered.

Six runs on `claude-sonnet-5` (deep and quick), alternating arms across three
ticker/date pairs, ten minutes apart so no run reads another's cache.

## Prediction, written before the runs

- Baseline: a 0% cache read rate, and `missing_cache_markers` on every run,
  driven mostly by the analysts' tool loops, which re-send the same system
  prompt and growing history several times a minute.
- Patched: a cache read rate well above the OpenAI baseline's ~10%, because
  automatic caching moves its breakpoint forward on every call. The debate
  turns still put role text before the shared reports at this commit, so
  `misordered_shared_context` should remain.

If the data disagrees, the data wins and this section stays as written.

## Checked before spending anything

Both checks ran against a local mock of the Messages API, so no key or spend
was involved:

- The patch puts `cache_control: {"type": "ephemeral"}` at the top level of
  the request body, for plain calls and for tool-bound ones (the analysts use
  `bind_tools`).
- The capture stack (OpenInference Anthropic + LangChain instrumentors,
  `corpus/captured/runner.py --provider anthropic`) produces spans that
  tokenaudit reads correctly: one request per call after dedup, input counted
  inclusive of cache reads and writes, tool definitions and tool calls kept.

## Run it

    export ANTHROPIC_API_KEY=...   # stays in your shell
    bash experiments/tradingagents-claude/run.sh

It reuses the capture virtualenv under `corpus/captured/.work`. Wall time is
roughly an hour and a half, mostly cooldowns. The OpenAI runs used 82k-96k
input and ~14k output tokens each, so expect a few dollars in total at
Sonnet 5 prices; check Anthropic's current price list before starting. Set
`MODEL` to try another Claude model; Haiku 4.5 needs 4,096 tokens before
anything is cacheable, so many TradingAgents prompts would be too short.

## Analyze

    tokenaudit diagnose experiments/tradingagents-claude/runs/*-baseline-*/trace.jsonl
    tokenaudit check experiments/tradingagents-claude/runs/*-baseline-*/trace.jsonl \
        --baseline /tmp/claude-baseline.json --update
    tokenaudit check experiments/tradingagents-claude/runs/*-patched-*/trace.jsonl \
        --baseline /tmp/claude-baseline.json

The second `check` prints the read-rate change between arms. Results go in
`results.md` next to this file, then into a case study under
`docs/case-studies/`.
