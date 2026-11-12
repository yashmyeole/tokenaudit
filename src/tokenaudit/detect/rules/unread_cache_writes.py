"""Unread cache writes: prompts written to the cache that nothing reads back.

A cache write costs more than plain input (1.25x on Anthropic's 5-minute
cache), and it only pays off when a later request reads the prefix back.
Turning caching on for every request, with markers on every prompt or
automatic caching, also writes the one-off prompts: a final report, a
single debate turn, the last call of a tool loop. Those writes are pure
premium.

A write counts as unread when no later request in the session re-sends any
of what it wrote. Sessions where another detector already explains why the
cache isn't reused (a drifting prefix, an expired entry, a schema change, a
breakpoint the provider can't reach) are left to that detector.
"""

from __future__ import annotations

from tokenaudit.counterfactual import impact, rates
from tokenaudit.detect.base import (
    Abstain,
    Detector,
    prefix_tokens,
    require_content,
    shared_prefix,
)
from tokenaudit.detect.findings import Finding, Fix, Severity
from tokenaudit.detect.rules.breakpoint_limit import BreakpointLimitDetector
from tokenaudit.detect.rules.lookback_overflow import LookbackOverflowDetector
from tokenaudit.detect.rules.prefix_drift import PrefixDriftDetector
from tokenaudit.detect.rules.tool_schema_churn import ToolSchemaChurnDetector
from tokenaudit.detect.rules.ttl_expiry import TtlExpiryDetector
from tokenaudit.model.records import Request, Session
from tokenaudit.rulebook import Rulebook

_DEFERS_TO = (
    PrefixDriftDetector,
    TtlExpiryDetector,
    ToolSchemaChurnDetector,
    BreakpointLimitDetector,
    LookbackOverflowDetector,
)
_MIN_REQUESTS = 2
_MIN_SHARE = 0.25
_MEDIUM_SHARE = 0.02


def _explained_elsewhere(session: Session, rulebook: Rulebook) -> bool:
    for detector in _DEFERS_TO:
        try:
            if detector().check(session, rulebook):
                return True
        except Abstain:
            continue
    return False


def _resent_tokens(request: Request, later: list[Request]) -> int:
    """How much of `request`'s prompt a later same-model request sends again."""
    best = 0
    for other in later:
        if other.model != request.model:
            continue
        chars, _ = shared_prefix(request, other)
        if chars:
            best = max(best, prefix_tokens(request, chars))
    return best


class UnreadCacheWritesDetector(Detector):
    detector_id = "unread_cache_writes"

    def check(self, session: Session, rulebook: Rulebook) -> list[Finding]:
        requests = require_content(session, minimum_requests=2)
        if all(r.usage.cache_creation is None for r in requests):
            marked = any(b.cache_marker for r in requests for b in r.blocks or ())
            implicit = any(
                (rule := rulebook.lookup(r.model, r.provider)) and rule.supports_implicit_cache
                for r in requests
            )
            if marked or implicit:
                raise Abstain("needs cache write counts, and this source has none")
            return []  # nothing asked to be cached, so nothing was written
        written = sum(r.usage.cache_creation or 0 for r in requests)
        if not written or _explained_elsewhere(session, rulebook):
            return []

        unread: list[tuple[Request, int]] = []
        for i, request in enumerate(requests):
            wrote = request.usage.cache_creation or 0
            if not wrote:
                continue
            resent = _resent_tokens(request, requests[i + 1 :])
            if resent <= (request.usage.cache_read or 0):
                unread.append((request, wrote))
        unread_tokens = sum(tokens for _, tokens in unread)
        if len(unread) < _MIN_REQUESTS or unread_tokens < _MIN_SHARE * written:
            return []

        first = unread[0][0]
        rule = rulebook.lookup(first.model, first.provider)
        rate = rates(rule)
        if rate.write is None:
            return []
        premium = unread_tokens * (rate.write - 1.0)
        if premium <= 0:
            return []
        saved = sum(r.usage.cache_read or 0 for r in requests) * (1.0 - rate.read)
        session_input = sum(r.usage.input_total or 0 for r in requests)
        if premium > saved:
            severity = Severity.HIGH
            verdict = (
                f"; reads saved ~{saved:,.0f} full-price tokens, so caching cost this "
                "session more than it saved"
            )
        else:
            severity = Severity.MEDIUM if premium >= _MEDIUM_SHARE * session_input else Severity.LOW
            verdict = ""
        finding = Finding(
            detector_id=self.detector_id,
            severity=severity,
            session_id=session.session_id,
            source=first.source,
            span_ids=tuple(r.span_id for r, _ in unread),
            summary=(
                f"{len(unread)} request(s) wrote ~{unread_tokens:,} tokens to the cache that "
                f"no later request sent again, at {rate.write}x input{verdict}"
            ),
            evidence={
                "requests_affected": len(unread),
                "unread_write_tokens": unread_tokens,
                "all_write_tokens": written,
                "read_saving_tokens": round(saved),
            },
            impact=impact(
                round(premium),
                round(premium),
                rate,
                rulebook,
                "sending these prompts uncached saves the write premium and loses "
                "nothing, since nothing read them back",
            ),
            fix=Fix(
                summary=(
                    "Cache only prefixes that get reused within the cache lifetime: mark "
                    "the stable system prompt, tools and shared context, and leave one-off "
                    "prompts unmarked instead of caching every request."
                ),
                hint="automatic caching writes every prompt, including ones sent once",
                doc_url=rule.source_url if rule else None,
            ),
        )
        return [finding]
