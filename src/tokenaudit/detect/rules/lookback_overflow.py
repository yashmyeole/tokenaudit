"""Lookback overflow: the reusable prefix sits too far behind the breakpoint.

A breakpoint checks a bounded number of positions (20 on Anthropic,
counting itself first). When history grows by more blocks than that between
calls, the previously cached prefix is out of reach and never read — even
though it is byte-identical and still alive.
"""

from __future__ import annotations

from tokenaudit.counterfactual import impact, rates
from tokenaudit.detect.base import (
    TOKEN_ESTIMATE_NOTE,
    Abstain,
    Detector,
    Pair,
    blocks_tokens,
    consecutive_pairs,
)
from tokenaudit.detect.findings import Finding, Fix, Severity
from tokenaudit.model.records import Session
from tokenaudit.rulebook import Rulebook


def _shared_prefix_len(pair: Pair) -> int:
    a = pair.first.blocks or ()
    b = pair.second.blocks or ()
    shared = 0
    for i in range(min(len(a), len(b))):
        if a[i].sha256 != b[i].sha256:
            break
        shared = i + 1
    return shared


class LookbackOverflowDetector(Detector):
    detector_id = "lookback_overflow"

    def check(self, session: Session, rulebook: Rulebook) -> list[Finding]:
        pairs = consecutive_pairs(session)
        if not pairs:
            raise Abstain("needs consecutive same-model requests with captured content")

        events: list[tuple[Pair, int, int, int]] = []  # (pair, marker idx, distance, lookback)
        for pair in pairs:
            blocks = pair.second.blocks or ()
            marked = [j for j, block in enumerate(blocks) if block.cache_marker]
            if not marked:
                continue
            rules = rulebook.lookup(pair.second.model, pair.second.provider)
            if rules is None or rules.lookback_positions is None:
                continue
            shared = _shared_prefix_len(pair)
            if shared == 0:
                continue
            marker = marked[-1]
            distance = marker - (shared - 1)
            shared_tokens = blocks_tokens(blocks[:shared])
            read = pair.second.usage.cache_read
            # Miss condition: reads cover less than half of the identical,
            # still-live prefix - the reusable region is provably out of reach.
            missed = read is not None and read < 0.5 * shared_tokens
            if distance >= rules.lookback_positions and missed:
                events.append((pair, marker, distance, rules.lookback_positions))
        if not events:
            return []

        first = events[0][0].second
        rate = rates(rulebook.lookup(first.model, first.provider))
        per_event = []
        for pair, _, _, _ in events:
            shared_tokens = blocks_tokens((pair.second.blocks or ())[: _shared_prefix_len(pair)])
            shortfall = max(shared_tokens - (pair.second.usage.cache_read or 0), 0)
            per_event.append(shortfall * rate.waste(pair.second))
        wasted = sum(per_event)  # every event already required an observed shortfall

        pair, marker, distance, lookback = events[0]
        rules = rulebook.lookup(pair.second.model, pair.second.provider)
        finding = Finding(
            detector_id=self.detector_id,
            severity=Severity.MEDIUM,
            session_id=session.session_id,
            source=pair.second.source,
            span_ids=tuple(p.second.span_id for p, _, _, _ in events),
            summary=(
                f"history grows so fast that the cached prefix sits {distance} positions "
                f"behind the breakpoint — beyond the {lookback}-position lookback — so an "
                f"identical, still-live prefix is never reused ({len(events)} event(s))"
            ),
            evidence={
                "events": [
                    {
                        "span_id": p.second.span_id,
                        "marker_index": m,
                        "prefix_distance": d,
                        "lookback_positions": lb,
                    }
                    for p, m, d, lb in events
                ],
            },
            impact=impact(
                wasted,
                wasted,
                rate,
                rulebook,
                "the out-of-reach identical prefix would have been read at the cached rate; "
                f"{TOKEN_ESTIMATE_NOTE}; low bound counts events with an observed zero "
                "cache read",
            ),
            fix=Fix(
                summary=(
                    "Add intermediate cache markers as history grows (a moving breakpoint "
                    "every few turns) so each new breakpoint finds the previous one inside "
                    "its lookback window."
                ),
                hint=f"keep marker spacing under {lookback} blocks",
                doc_url=rules.source_url if rules else None,
            ),
        )
        return [finding]
