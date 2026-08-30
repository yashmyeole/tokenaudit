# Changelog

## Unreleased

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
