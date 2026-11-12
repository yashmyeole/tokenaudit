# Changelog

## Unreleased

- Two new detectors, using per-line digests that ingest now keeps in memory:
  - `misordered_shared_context`: the same reports or documents sent to
    several agents, each behind its own role text, so the prefix cache never
    reuses them.
  - `cross_session_drift`: a date, user or ticker near the start of a long
    prompt, which keeps it from being reused across sessions.
- They find the TradingAgents problems the case study diagnosed by hand. The
  captured runs, previously reported as clean, now show both findings and a
  25% health floor (was 100%).
- Upgrading will add findings to traces with these patterns: re-baseline
  `tokenaudit check` after upgrading.
- `diagnose` and `check` report a cross-session section when given more than
  one session; `ingest` output leaves out the line digests.
- Six new corpus fixtures (one positive and two near-misses per detector).
- Impact for the new detectors uses each model's read rate from the rulebook.

## 0.2.0 — 2026-11-10

- Fix the GitHub Action for external users. It installed the latest PyPI
  release, and 0.1.0 has no `check` command. It now installs the code at the
  ref it is used from, so `@v0.2.0` runs 0.2.0. The `version` input still
  pins a PyPI release.
- Rulebook re-verified against current provider docs (version 2026.11.10):
  - Anthropic: Opus, Sonnet and Haiku 5.5, Fable and Mythos 5.1/5.2.
    Per-model read multipliers (0.05x and 0.025x) and base input prices.
  - OpenAI: GPT-5.6 sizes, GPT-6 and GPT-6.1, 12h TTL. Pre-5.6 models no
    longer publish a fixed minimum, so it is recorded as unknown.
  - Google: added Gemini 4 Argon and 3.8 Flash; removed 3.7 and 3.5 Flash,
    which are no longer in Google's table.
- `sub_minimum` abstains when a model has no published minimum.
- Release workflow: pushing a `vX.Y.Z` tag publishes to PyPI.
- The sdist no longer includes the experiment traces.
- **`ingest_text(text, source)`**: ingest trace content already in memory
  (an upload, a stream) — nothing touches disk. `ingest_path` now wraps it.
- **Hosted demo** (`demo/`, not part of the package): a one-page FastAPI
  app — drop a trace, get the report; two prerendered samples (the
  synthetic prefix-drift fixture and the real captured TradingAgents
  runs). Uploads are parsed in memory and never stored or logged.
- **CI cost-regression gate**: `tokenaudit check <traces> --baseline
  baseline.json` fails a build (exit 1) on a new HIGH finding or a drop in
  cache read rate / health floor beyond `--max-regression` points; create
  or refresh the baseline with `--update`. Ships as a composite GitHub
  Action (`action.yml`) with a markdown summary written to the job page
  (and to a file via `--markdown`, for PR comments). See `docs/ci.md`.

## 0.1.0 — 2026-08-30

First usable release.

- **Ingest** four real trace dialects into one normalized accounting model:
  OTel GenAI current semconv, legacy flat names (older openllmetry),
  OpenInference, and the OTel Python SDK's `to_json()` JSONL — with
  layered-span deduplication (framework + SDK instrumentors double-report
  the same call), a data-quality gate that refuses to price malformed
  telemetry, and prompt privacy by construction (block hashes and lengths,
  never text).
- **Nine deterministic detectors** — prefix drift, sub-minimum cache
  blocks, model switch, TTL expiry, breakpoint limit, lookback overflow,
  tool-schema churn, context bloat, retry storm — each with evidence
  pointers, bounded impact ranges with stated assumptions, and abstention
  (with a reason) as a first-class outcome distinct from "checked, clean".
- **Provider caching rulebook** packaged as sourced, dated JSON (Anthropic,
  OpenAI, Google), with a CI staleness alarm and a rendered comparison
  table (`tokenaudit rulebook`).
- **Evaluation** against a byte-reproducible labeled corpus, published in
  `eval/results.md`; per-detector corpus records ship inside the package
  and appear on findings.
- **CLI**: `tokenaudit diagnose` (text, `--json`, self-contained `--html`
  report, `--sample`), `tokenaudit ingest`, `tokenaudit rulebook`.
- Zero runtime dependencies. Python 3.11+.
