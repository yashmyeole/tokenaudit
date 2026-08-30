# CI cost-regression gate

`tokenaudit check` turns cache health into something a build can fail on.
The idea: prompt-caching regressions are silent — a reordered tool list or
a timestamp added to a system prompt changes no test output, but can double
the input bill. If your test or staging runs already emit traces, the gate
catches the regression in the pull request that introduces it.

## How it works

1. From a known-good run, snapshot a **baseline** and commit it:

       tokenaudit check traces/ --baseline tokenaudit-baseline.json --update

2. In CI, diagnose the fresh run and compare:

       tokenaudit check traces/ --baseline tokenaudit-baseline.json

   Exit code 0 = pass, 1 = regression, 2 = the comparison itself failed
   (missing baseline, unreadable traces, no token data).

The build **fails** when a detector fires at HIGH severity that was not
HIGH in the baseline, or when the cache read rate or cache health floor
drops by more than `--max-regression` percentage points (default 5.0).
`--strict` also fails on any new finding. Improvements never fail the
build; a large one prints a reminder to refresh the baseline.

The baseline is aggregate-level on purpose: session ids, request counts
and token totals vary run to run, so the gate compares only ratios (cache
read rate, health floor) and which failure modes are present — the things
that are stable across runs of the same workload. Metrics that cannot be
computed on one side are reported as incomparable, never guessed at.

This repo runs the gate on itself: the `cache-regression` job in
[ci.yml](../.github/workflows/ci.yml) checks the captured TradingAgents
runs against [tokenaudit-baseline.json](../tokenaudit-baseline.json).

## GitHub Action

The repo root ships an [`action.yml`](../action.yml), so the whole gate is
one step:

```yaml
- uses: actions/checkout@v4
- uses: actions/setup-python@v5
  with: { python-version: "3.12" }
- uses: yashmyeole/tokenaudit@main   # pin a tag once released
  with:
    traces: traces/
    baseline: tokenaudit-baseline.json
```

Optional inputs: `max-regression` (points, default `5.0`), `strict`
(`true`/`false`), `version` (pin the PyPI release the action installs).

Inside Actions, the CLI notices `$GITHUB_STEP_SUMMARY` and appends its
markdown summary automatically, so the job's summary page shows the
baseline-vs-current table. For a PR comment, write the same markdown to a
file with `--markdown summary.md` and post it with any comment action:

```yaml
- run: tokenaudit check traces/ --baseline tokenaudit-baseline.json --markdown summary.md
- uses: marocchino/sticky-pull-request-comment@v2
  if: always()
  with: { path: summary.md }
```

## Refreshing the baseline

When a change legitimately alters cache behavior (for better, or as an
accepted cost), regenerate with `--update` and commit the new baseline in
the same pull request — the diff is small, sorted JSON, so the review
shows exactly which numbers moved.
