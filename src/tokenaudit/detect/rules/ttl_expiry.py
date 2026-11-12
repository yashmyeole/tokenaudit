"""TTL expiry: an idle gap outlives the cache and an identical prefix re-bills.

Uses the rulebook's per-model TTL and abstains when the provider does not
publish one (older OpenAI tiers, Gemini) — a gap can't be judged against an
unknown window. Anthropic's clock runs from the START of the request that
wrote or last read the entry; request start-to-start gaps measure exactly
that, and are a conservative approximation for sliding-window providers.
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
    last_marked_index,
    require_timestamps,
)
from tokenaudit.detect.findings import Finding, Fix, Severity
from tokenaudit.model.records import Session
from tokenaudit.rulebook import Rulebook


def _stable_prefix(pair: Pair) -> bool:
    d = pair.first_divergence
    if d is None:
        return True
    blocks = pair.first.blocks or ()
    end = last_marked_index(blocks)
    cacheable_end = end if end is not None else min(len(blocks), len(pair.second.blocks or ())) - 2
    return d > cacheable_end


class TtlExpiryDetector(Detector):
    detector_id = "ttl_expiry"

    def check(self, session: Session, rulebook: Rulebook) -> list[Finding]:
        require_timestamps(session.requests)
        pairs = [p for p in consecutive_pairs(session) if _stable_prefix(p)]
        if not pairs:
            raise Abstain("needs consecutive same-model requests with a stable captured prefix")

        events: list[tuple[Pair, int, int]] = []  # (pair, gap_s, ttl_s)
        for pair in pairs:
            rules = rulebook.lookup(pair.second.model, pair.second.provider)
            if rules is None:
                raise Abstain(f"model not in the rulebook: {pair.second.model}")
            ttls = [t.seconds for t in rules.ttl if t.seconds is not None]
            if not ttls:
                raise Abstain(
                    f"cache lifetime not published for {pair.second.model}; a gap cannot "
                    "be judged against an unknown window"
                )
            ttl_s = min(ttls)  # judge against the default (shortest) lifetime
            gap_s = (pair.second.start_unix_nano - pair.first.start_unix_nano) // 1_000_000_000
            usage = pair.second.usage
            missed = usage.cache_read == 0 or (usage.cache_creation or 0) > 0
            if gap_s > ttl_s and missed:
                events.append((pair, int(gap_s), ttl_s))
        if not events:
            return []

        first_pair, gap_s, ttl_s = events[0]
        rules = rulebook.lookup(first_pair.second.model, first_pair.second.provider)
        rate = rates(rules)
        per_event: list[float] = []
        observed = 0
        for pair, _, _ in events:
            blocks = pair.second.blocks or ()
            end = last_marked_index(blocks)
            end = end if end is not None else len(blocks) - 2
            per_event.append(blocks_tokens(blocks[: end + 1]) * rate.waste(pair.second))
            if (pair.second.usage.cache_creation or 0) > 0:
                observed += 1
        finding = Finding(
            detector_id=self.detector_id,
            severity=Severity.MEDIUM,
            session_id=session.session_id,
            source=first_pair.second.source,
            span_ids=tuple(p.second.span_id for p, _, _ in events),
            summary=(
                f"an idle gap of {gap_s}s exceeds the {ttl_s}s cache lifetime, so an "
                f"identical prefix was re-billed ({len(events)} expiry event(s))"
            ),
            evidence={
                "events": [
                    {"gap_seconds": g, "ttl_seconds": t, "span_id": p.second.span_id}
                    for p, g, t in events
                ],
                "ttl_semantics": rules.ttl_semantics if rules else None,
                "note": (
                    "judged against the default (shortest) lifetime; a configured "
                    "longer-TTL option would survive this gap - check the session config"
                ),
            },
            impact=impact(
                sum(sorted(per_event)[:observed]),
                sum(per_event),
                rate,
                rulebook,
                f"the expired prefix is re-billed instead of read; {TOKEN_ESTIMATE_NOTE}; "
                "low bound counts only events with an observed cache write after the gap",
            ),
            fix=Fix(
                summary=(
                    "Keep sessions warm across known idle points (a cheap keepalive read), "
                    "use a longer-TTL cache option where offered, or batch work to avoid "
                    "gaps longer than the cache lifetime."
                ),
                hint=f"lifetime runs from request start ({rules.ttl_semantics})" if rules else None,
                doc_url=rules.source_url if rules else None,
            ),
        )
        return [finding]
