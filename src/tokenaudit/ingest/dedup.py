"""Layered-span deduplication.

Real instrumentation stacks double-report: a framework-level instrumentor
(LangChain) and an SDK-level one (OpenAI) each emit a span for the same LLM
call, with identical token totals. Summing both doubles every dollar. In
the captured TradingAgents runs the two layers are not even in the same
trace (the SDK spans start their own traces with no parent), so ancestry
cannot pair them.

The rule used here was validated against those captures (49/49 calls
paired): two requests describe the same call when **one's time window
contains the other's** (with 5 ms slack for clock jitter) **and their
(input_total, output) totals are equal**. Sequential duplicates — retry
storms — never satisfy containment, so genuine repeats survive.

Of a pair, the richer request wins (cache fields known beats unknown, then
more metadata), because the layer closest to the wire reports what the
provider actually said.
"""

from __future__ import annotations

from tokenaudit.ingest.dialects import richness
from tokenaudit.model.records import Request

_SLACK_NS = 5_000_000  # 5 ms

FLAG_DEDUPED_LAYER = "deduped_layered_span"


def _contains(outer: Request, inner: Request) -> bool:
    return (
        outer.start_unix_nano - _SLACK_NS <= inner.start_unix_nano
        and outer.end_unix_nano + _SLACK_NS >= inner.end_unix_nano
    )


def _same_call(a: Request, b: Request) -> bool:
    if a.usage.input_total is None or a.usage.output is None:
        return False
    if a.usage.input_total != b.usage.input_total or a.usage.output != b.usage.output:
        return False
    return _contains(a, b) or _contains(b, a)


def dedup_layered(requests: list[Request]) -> tuple[list[Request], list[str]]:
    """Collapse duplicate layers; returns (kept, dropped span ids)."""
    ordered = sorted(requests, key=lambda r: (r.start_unix_nano, r.span_id))
    dropped: set[str] = set()
    for i, first in enumerate(ordered):
        if first.span_id in dropped:
            continue
        for second in ordered[i + 1 :]:
            if second.span_id in dropped:
                continue
            if second.start_unix_nano - _SLACK_NS > first.end_unix_nano:
                break  # sorted by start: nothing later can be contained in `first`
            if _same_call(first, second):
                loser = min((first, second), key=richness)
                dropped.add(loser.span_id)
                if loser is first:
                    break  # `first` is gone; stop pairing against it
    kept = [r for r in ordered if r.span_id not in dropped]
    return kept, sorted(dropped)
