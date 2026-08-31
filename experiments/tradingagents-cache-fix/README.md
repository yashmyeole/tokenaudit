# Experiment: cache-friendly prompt layout for TradingAgents

The before/after experiment behind the TradingAgents case study and the
upstream PR for [TauricResearch/TradingAgents#750][750]. Everything here is
registered **before** the experiment runs — hypothesis, mechanism, predicted
effect, and analysis plan — so the numbers can't be quietly re-framed after
the fact.

[750]: https://github.com/TauricResearch/TradingAgents/issues/750

## What the traces showed (evidence first)

tokenaudit's ingest of three unmodified TradingAgents runs
(`corpus/captured/runs/tradingagents/`, commit `a33fd4c`, gpt-5.4-mini)
shows a combined cache read rate of **~10.6%**, and the per-request anatomy
explains exactly why:

- The four tool-loop analysts *do* cache: their second and third calls
  extend their own growing history, and reads appear (this is nearly all of
  the 10.6%).
- The eight single-shot debate prompts — bull/bear researchers, research
  manager, trader, three risk debaters, portfolio manager — account for
  **~59k of run-01's 82.5k input tokens and receive zero cached reads.**
  Each stuffs the *same* four analyst reports (and, for the risk debate,
  the same trader plan and growing history) behind a *different* opening
  role paragraph. Shared content, no shared prefix: prefix caches match
  from byte zero, so the role-first layout makes every one of these
  requests bill at full price.
- Across runs, only **1 of ~12** distinct system blocks is byte-identical
  even for the same ticker a week apart. The analyst system prompts differ
  only by the injected trade date — same length, different hash.

## Hypothesis and fix

**Hypothesis:** reordering prompt sections so that shared context leads and
role-specific instructions trail will let the provider's implicit prefix
cache serve the debate prompts, raising the run-level cache read rate
severalfold, with no change to what any agent is told — only to the order
it is told in.

`cache_friendly_prompts.patch` (9 files, wording preserved, sections
reordered):

- **bull/bear researchers, three risk debaters:** layout becomes
  `[shared reports (+ trader decision)] + [append-only debate history] +
  [role instructions + last opposing arguments]`. Within each group the
  shared block is byte-identical (verified by hash at patch time), so each
  debate turn extends the previous turn's prefix.
- **four analysts:** the volatile `Today's date is ... {instrument_context}`
  sentence moves from the middle of the system prompt to the end, after
  `{system_message}`, so analyst system prefixes are byte-stable across
  trade dates and tickers (this mostly enables *cross-run* reuse, e.g.
  backtests; analyst first calls are often below gpt-5.4-mini's 2048-token
  cacheable minimum, so we do not predict much within-run gain from it).

## Registered prediction

From run-01's anatomy: the bear researcher (~5.5k shareable tokens), the
conservative (~6.5k) and neutral (~7.5k) risk debaters should gain roughly
**19-20k cached reads per run**, moving the combined hit rate from ~10% to
roughly **30-35%**, an input-bill reduction around **20%** (cached reads
bill at 0.1x). Token figures use ~4 chars/token estimates, so treat the
prediction as a range, not a point. If OpenAI's implicit cache fails to
serve some identical prefixes (routing/load are provider-side and
best-effort), measured gains will land below the structural ceiling — that
gap is itself a finding, and `prompt_cache_key` routing hints are the
follow-up lever, deliberately kept out of this patch to test one variable.

## Design

8 runs, one variable, alternating arm order (cancels time-of-day effects),
**>= 15 minutes apart** so gpt-5.4-mini's implicit cache (~5-10 idle
minutes) cannot carry one run's prefixes into the next:

| # | Arm | Config | Purpose |
|---|---|---|---|
| 1 | baseline | NVDA:2026-08-14 | pair 1 |
| 2 | patched | NVDA:2026-08-14 | pair 1 |
| 3 | control | NVDA:2026-08-14 | untraced baseline |
| 4 | baseline | AAPL:2026-08-14 | pair 2 |
| 5 | patched | AAPL:2026-08-14 | pair 2 (patched first) |
| 6 | baseline | NVDA:2026-08-21 | pair 3 |
| 7 | patched | NVDA:2026-08-21 | pair 3 |
| 8 | control | NVDA:2026-08-14 | untraced baseline, replicate |

- **baseline** — TradingAgents @ `a33fd4c`, traced exactly as in Phase 2.
- **patched** — same commit + the patch, same tracing.
- **control** — baseline code, *no* OpenTelemetry: usage is recorded by a
  passive tee over the SDK's own HTTP module that only copies each
  response's `usage` object (see the measurement log below). It
  answers the auditor question "does tracing itself change cache
  behavior?" — if untraced hit rates sit inside the traced runs' spread,
  observation isn't moving the number. (tokenaudit never sits in the
  request path, but the *capture* instrumentation wraps the client, so the
  question deserves data, not assertion.)
- Configs reuse the Phase-2 corpus configs, so the three corpus runs serve
  as additional baseline replicates recorded on a different day.

Cost: ~8 runs x ~100k tokens of gpt-5.4-mini — around a dollar. Wall time
~2h15m, dominated by cooldowns; start it and walk away.

## Threats to validity (read before quoting numbers)

- **n=3 per arm.** Deltas are reported per matched config pair, alongside
  the run-to-run spread of identical configs (9.3% vs 18.1% on the two
  corpus NVDA runs), not just as means.
- **Implicit caching is best-effort.** A miss on an identical prefix is
  provider-side variance; the structural metric (byte-identical prefix
  availability, from block hashes) is reported separately from realized
  hits for exactly this reason.
- **Live tools.** TradingAgents fetches live market/news data, so token
  totals differ between arms even on the same config; that is why the
  primary metric is the hit *rate* and per-config deltas, not absolute
  token counts.
- The patch also changes *section order* seen by the model. Decisions
  (BUY/HOLD/SELL) and report lengths are captured per run in
  `decision.txt`/`manifest.json` so any behavior drift is visible, but this
  experiment measures cost, not decision quality.

## Measurement log (errata)

Kept because a controlled experiment's credibility depends on what went
wrong being visible, not only what worked.

- **2026-08-31 — the first control runs measured nothing.** The untraced
  arm records usage with a passive tee over the HTTP client. That tee
  patched `httpx`, but the OpenAI SDK in the capture venv uses `httpx2`
  (both generations are installed side by side), so it wrapped a module no
  request ever passed through and wrote a 0-byte `usage.jsonl`. An earlier
  diagnosis blamed sync-vs-async clients; the stack trace from a later run
  disproved that — the SDK's call is a plain sync `send`, just in the other
  module. Fixed by deriving the module from the installed SDK
  (`_http_modules` in `exp_runner.py`), patching every candidate, exiting
  non-zero when a control run ends with an empty tee, and adding
  `exp_runner.py --selftest`, which proves the tee records a mocked OpenAI
  response through the SDK's own module before any run starts. The six
  traced runs were unaffected — they use OpenTelemetry instrumentation, not
  the tee — and `analyze.py` reports an empty control as "no data", never
  as a 0% hit rate.
- **2026-08-31 — a control rerun died mid-run on DNS failure**
  (`[Errno 8] nodename nor servname provided`, both Reddit and
  api.openai.com): the machine's network dropped. No data was affected;
  both scripts now check that `api.openai.com` resolves before spending
  cooldowns or tokens.

## Running it

    export OPENAI_API_KEY=...      # stays in your shell
    bash experiments/tradingagents-cache-fix/run_experiment.sh
    python experiments/tradingagents-cache-fix/analyze.py   # writes results.md

Files: `cache_friendly_prompts.patch` (the fix), `run_experiment.sh`
(orchestration + cache isolation), `exp_runner.py` (one run, traced or
control), `analyze.py` (accounting -> results.md). Traced runs are redacted
by the Phase-2 redactor before anything can be committed.
