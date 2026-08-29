# Captured runs

Real traces from real public projects — the evidence half of the corpus.
Everything under `runs/` was produced by executing an unmodified public agent
with process-level OpenTelemetry instrumentation, then redacted. Nothing here
is synthetic; the synthetic half lives in `../synthetic/` and is labeled as
such (see `../README.md` for the boundary between the two).

## TradingAgents

[TradingAgents](https://github.com/TauricResearch/TradingAgents) (~98k stars)
has an open report of a ~0% prompt-cache hit rate causing 30–40% unnecessary
API cost ([#750](https://github.com/TauricResearch/TradingAgents/issues/750)).
That makes it the ideal capture target: genuine waste, publicly documented,
in code this project's author did not write.

Reproduce (needs your own `OPENAI_API_KEY`; the key never appears in any
output — a redaction pass enforces that):

    export OPENAI_API_KEY=...
    bash corpus/captured/capture_tradingagents.sh

The script builds a throwaway venv under `.work/` (gitignored), clones
TradingAgents, installs OpenInference instrumentation for the OpenAI SDK and
LangChain, smoke-tests the exporter before spending money, then executes the
configured ticker/date analyses. Each run directory contains:

| File | Contents |
|---|---|
| `trace.jsonl` | one OTel SDK `ReadableSpan.to_json()` object per line — the SDK's own rendering, no custom mapping |
| `manifest.json` | ticker, date, models, TradingAgents commit, package versions, span count, duration |
| `decision.txt` | the agent's final trading decision, for the human-readable record |

Default models are `gpt-5.4-mini` for both think tiers to keep a capture in
the tens-of-cents range; override with `TOKENAUDIT_DEEP_LLM` /
`TOKENAUDIT_QUICK_LLM`.

## Honest notes on this data

- **A fourth dialect, on purpose.** The synthetic corpus covers three trace
  schemas; these captures add the OTel Python SDK's native `to_json()` shape,
  which is what real teams actually dump to files. The reader has to handle
  it, so the corpus contains it.
- **Non-determinism is the point.** Unlike the synthetic track, wall-clock
  times, market data and model outputs vary between captures. Manifests pin
  what can be pinned (commit, versions, config).
- **Cache-minimum interaction.** Mini-tier OpenAI models have a higher
  cacheable-prefix minimum than frontier models, and TradingAgents' system
  prompts sit near that boundary (per #750, ~2k tokens). A capture may show
  compound causes — dynamic prompts *and* sub-minimum prefixes. Compound
  causes are exactly what a root-cause tool has to untangle, so this is a
  feature of the dataset, not a flaw.
- **Redaction.** `redact.py` scrubs key-shaped strings and the capturing
  user's home directory from every file, verifies nothing key-shaped
  survived, and fails the capture otherwise. Prompts and market data remain
  intact — they come from public code and public sources.
