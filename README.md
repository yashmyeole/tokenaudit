# tokenaudit

**Root-cause diagnosis of wasted LLM tokens — from the traces your agents already emit.**

> Status: early development, pre-0.1. Interfaces will change.

Every LLM cost tool can tell you *how much* you spent and *who* spent it. Almost none can tell you *why a specific request wasted money*. The biggest lever on an LLM bill — prompt caching, up to a 10× discount on repeated input — fails silently: change one byte of a cached prefix, drop below a model's minimum cacheable size, let a timer expire, and the provider just bills you full price. No error, no warning. There are [documented](https://barndoor.ai/what-happens-to-your-llm-bill-when-prompt-caching-fails/) [cases](https://github.com/TauricResearch/TradingAgents/issues/750) of agents running at ~0% cache hit rate for months, at 30–40% unnecessary cost, because of a single line of prompt construction.

tokenaudit reads the traces agents already produce and, for each expensive request, answers four questions: **what was wasted, why, what it cost, and what to change.**

It works: diagnosing one of those documented cases produced a 9-file patch that took TradingAgents' cache hit rate from **7.5% to 32.4% (4.3×)** in a controlled, pre-registered before/after experiment — the [first case study](docs/case-studies/tradingagents.md).

## Quickstart

    pip install tokenaudit
    tokenaudit diagnose --sample          # diagnose the bundled demo trace
    tokenaudit diagnose mytraces/ --html report.html

Zero runtime dependencies; reads OTLP/JSON files and OTel SDK JSONL. Try it
on this repo's real captured runs: `tokenaudit diagnose corpus/captured/runs`.
Prefer a browser? The [hosted demo](demo/) diagnoses a dropped trace without
installing anything — uploads are parsed in memory and never stored.

The plan, in one line each:

- Ingest standard traces — OpenTelemetry GenAI in all three of its cache-field naming generations, OpenInference, raw provider SDK logs — into one normalized accounting model.
- Run a library of deterministic detectors for the silent failure modes: prefix drift, sub-minimum cache blocks, model switches, TTL expiry, breakpoint and lookback limits, tool-schema churn and bloat, context bloat, retry storms.
- Price each finding against a counterfactual — what the bill would have been under the fix — as a bounded range, never a point guess.
- Publish per-detector precision and recall against a labeled corpus, so you know how much to trust each finding.

## Catch regressions in CI

Prompt-caching regressions are silent: no test fails when a reordered tool
list doubles your input bill. If CI already produces traces, fail the pull
request instead ([full docs](docs/ci.md)):

```yaml
- uses: yashmyeole/tokenaudit@main
  with:
    traces: traces/
    baseline: tokenaudit-baseline.json   # tokenaudit check traces/ --baseline ... --update
```

This repo gates itself: every build re-diagnoses the captured
TradingAgents runs against [tokenaudit-baseline.json](tokenaudit-baseline.json).

## Prior art

This space is crowded, and being precise about what already exists is the point of this section.

| Tool | What it does |
|---|---|
| [CacheLens](https://github.com/ChingEnLin/CacheLens) | Closest prior art. Dev-time SDK wrapper that meters cache hit rate and prefix stability per prompt layer in apps you instrument. Doesn't read existing traces, and prefix stability is its one diagnosis. |
| [Helicone](https://helicone.ai) | Observability proxy: cost and token accounting per user/session/feature, including cache token counts |
| [Langfuse](https://langfuse.com) | Open-source LLM engineering platform: tracing, evals, per-trace cost accounting |
| [Datadog LLM Observability](https://docs.datadoghq.com/llm_observability/) | Enterprise monitoring: per-request token and cost tracking, quality-issue clustering |
| [Braintrust](https://braintrust.dev) | Evals and observability; its Loop assistant root-causes *quality* failures |
| [LiteLLM](https://github.com/BerriAI/litellm) | Gateway with budgets, rate limits and spend tracking across providers |
| [ccusage](https://github.com/ryoppippi/ccusage) | CLI usage and cost reporting from Claude Code's local logs |
| [Cloudflare AI Gateway](https://developers.cloudflare.com/ai-gateway/) | Platform-level caching, spend caps and alerts |

All of these answer "how much", and several now count cache tokens. The ones that do automatic root-cause analysis target response *quality*, not cost. The closest tool diagnoses one failure mode, for one integration style, going forward only. What none of them do is take the telemetry any agent already emitted — no code changes, any vendor — and say: *these 44 requests missed cache because the system prompt embeds a timestamp; here is the evidence, and here is what it cost.* That attribution step is this project.

If you know a tool that already does this, please open an issue — I would genuinely rather find out now.

## What tokenaudit is not

- **Not a dashboard.** The tools above already report spend well.
- **Not a proxy, gateway, or wrapper.** It reads traces after the fact and never sits in the request path, so it can't change what it measures and doesn't need your code.
- **Not an LLM reading your logs.** Verdicts come from explicit rules with unit tests and a published false-positive rate ([decision 001](DECISIONS.md)).
- **Not a savings guarantee.** Estimates are ranges with stated assumptions, and requests whose telemetry fails a plausibility check are flagged as unpriceable, not silently priced.

## Planned layout

    src/tokenaudit/
      ingest/          trace readers + schema normalization
      model/           normalized requests/sessions + token accounting
      detect/          the diagnosis engine (deterministic detectors)
      counterfactual/  what the bill would have been under a fix
      report/          CLI, JSON and HTML output
      ci/              cost-regression gate for pipelines

## Roadmap

- [x] Prior-art survey, positioning, project scaffold
- [x] Labeled trace corpus: synthetic known-answer fixtures + captured runs from real public agents
- [x] Trace reader and normalizer with a data-quality gate (bad telemetry is refused, not zero-filled)
- [x] Provider caching rulebook: minimums, TTL semantics, invalidation rules — versioned, with sources
- [x] Detectors, starting with prefix drift, sub-minimum blocks, and model/version switch
- [x] Evaluation harness: per-detector results published in [eval/results.md](eval/results.md)
- [x] `tokenaudit diagnose` CLI, HTML report, cache health score
- [x] CI cost-regression mode: fail a build that tanks the cache hit rate ([docs/ci.md](docs/ci.md))
- [x] Hosted live demo: drag a trace in, get the report — uploads parsed in memory, never stored ([demo/](demo/))
- [x] Case studies on public agents, with reproduction commands ([TradingAgents: 4.3× cache hit rate](docs/case-studies/tradingagents.md))

## Development

    python -m venv .venv && source .venv/bin/activate
    pip install -e ".[dev]"
    pre-commit install
    pytest

## License

Apache-2.0
