"""Context bloat: the whole conversation re-sent every call, uncached.

The only usage-driven detector in the library: it needs no message content,
so it still works on traces captured with content capture off (the OTel
default). Fires when input grows monotonically across an unmanaged session
— no cache markers anywhere — while cache reads stay near zero, i.e. every
repeated token is billed at full price, forever, by construction.

Sessions that use cache markers are excluded: growth under markers has
specific failure modes (lookback overflow, breakpoint budget) with their
own detectors, and blaming "bloat" there would suggest the wrong fix.
"""

from __future__ import annotations

from itertools import pairwise

from tokenaudit.detect.base import Abstain, Detector
from tokenaudit.detect.findings import Finding, Fix, Impact, Severity
from tokenaudit.model.records import Session
from tokenaudit.rulebook import Rulebook

_MIN_REQUESTS = 5
_MONOTONIC_FRACTION = 0.8
_GROWTH_FACTOR = 2.0
_MAX_READ_RATIO = 0.3


class ContextBloatDetector(Detector):
    detector_id = "context_bloat"

    def check(self, session: Session, rulebook: Rulebook) -> list[Finding]:
        requests = [r for r in session.requests if r.usage.input_total is not None]
        if len(requests) < _MIN_REQUESTS:
            raise Abstain(f"needs at least {_MIN_REQUESTS} requests with input token counts")
        if any(block.cache_marker for r in requests if r.blocks for block in r.blocks):
            return []  # marker-managed growth has its own, more specific detectors

        inputs = [r.usage.input_total or 0 for r in requests]
        growing_steps = sum(1 for a, b in pairwise(inputs) if b >= a)
        if growing_steps < _MONOTONIC_FRACTION * (len(inputs) - 1):
            return []
        if inputs[-1] < _GROWTH_FACTOR * inputs[0]:
            return []
        total_input = sum(inputs)
        total_read = sum(r.usage.cache_read or 0 for r in requests)
        if total_input == 0 or total_read / total_input > _MAX_READ_RATIO:
            return []

        # Waste from usage alone: each call re-sends roughly the prior call's
        # content; whatever wasn't served from cache was re-billed.
        resent = [
            max(min(b, a) - read, 0)
            for (a, b), read in zip(
                pairwise(inputs), [r.usage.cache_read or 0 for r in requests[1:]], strict=True
            )
        ]
        confirmed = [
            amount for (a, b), amount in zip(pairwise(inputs), resent, strict=True) if b >= a
        ]
        tokens_high = round(0.9 * sum(resent))
        tokens_low = round(0.9 * sum(confirmed))

        finding = Finding(
            detector_id=self.detector_id,
            severity=Severity.HIGH,
            session_id=session.session_id,
            source=requests[0].source,
            span_ids=tuple(r.span_id for r in requests),
            summary=(
                f"input grows from {inputs[0]:,} to {inputs[-1]:,} tokens across "
                f"{len(requests)} calls with a {total_read / total_input:.0%} cache read "
                "ratio — the conversation is re-sent at full price on every call"
            ),
            evidence={
                "input_first": inputs[0],
                "input_last": inputs[-1],
                "growing_steps": growing_steps,
                "steps": len(inputs) - 1,
                "cache_read_ratio": round(total_read / total_input, 4),
                "cache_markers_present": False,
            },
            impact=Impact(
                tokens_low=tokens_low,
                tokens_high=tokens_high,
                usd_low=None,
                usd_high=None,
                assumption=(
                    "each call re-sends the prior call's content (monotonic growth pattern); "
                    "amounts come from reported usage, not estimates; 0.9x cached-rate delta; "
                    "low bound counts only confirmed growth steps"
                ),
                rulebook_version=rulebook.version,
            ),
            fix=Fix(
                summary=(
                    "Cache the stable prefix (system prompt + history) with cache markers or "
                    "automatic prefix caching, and/or truncate or summarize history instead "
                    "of re-sending all of it every call."
                ),
                hint=None,
                doc_url=None,
            ),
        )
        return [finding]
