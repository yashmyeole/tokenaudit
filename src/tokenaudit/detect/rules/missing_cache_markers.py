"""Missing cache markers: a provider that only caches on request, never asked.

Anthropic caches nothing unless a request marks a breakpoint (or turns on
automatic caching). A session with no markers and no cache activity at all,
where requests keep re-sending a prefix an earlier request sent minutes
before, pays full price for every repeat.

Only block-aligned prefixes count, since a breakpoint sits at the end of a
block, and only repeats within the cache lifetime, since a marker would not
have helped a request that came later. Tool definitions come first in the
prompt, so they count toward the shared prefix, and requests with different
tools share nothing. A conversation that grows call by
call is left to context_bloat, and a byte-identical re-send to retry_storm:
both report the same waste with a fix that fits it better.
"""

from __future__ import annotations

from tokenaudit.counterfactual import impact, rates
from tokenaudit.detect.base import (
    Abstain,
    Detector,
    prefix_tokens,
    require_content,
    require_timestamps,
    shared_prefix,
)
from tokenaudit.detect.findings import Finding, Fix, Severity
from tokenaudit.detect.rules.context_bloat import ContextBloatDetector
from tokenaudit.model.records import Request, Session
from tokenaudit.rulebook import Rulebook

_DEFAULT_TTL_S = 300
_MIN_REPEATS = 2
_HIGH_SHARE = 0.10


class MissingCacheMarkersDetector(Detector):
    detector_id = "missing_cache_markers"

    def check(self, session: Session, rulebook: Rulebook) -> list[Finding]:
        requests = require_content(session, minimum_requests=2)
        rules = {r.span_id: rulebook.lookup(r.model, r.provider) for r in requests}
        explicit_only = [
            r
            for r in requests
            if (rule := rules[r.span_id]) is not None
            and rule.supports_explicit_cache
            and not rule.supports_implicit_cache
        ]
        if all(rule is None for rule in rules.values()):
            raise Abstain("model not in the rulebook, so its caching rules are unknown")
        if len(explicit_only) < 2:
            return []  # caching here doesn't depend on markers
        require_timestamps(explicit_only)

        if any(b.cache_marker for r in session.requests for b in r.blocks or ()):
            return []
        if any(
            (r.usage.cache_read or 0) or (r.usage.cache_creation or 0) for r in session.requests
        ):
            return []  # caching is on, e.g. automatic caching; other detectors take it from here
        try:
            if ContextBloatDetector().check(session, rulebook):
                return []
        except Abstain:
            pass

        repeats: list[tuple[Request, int]] = []
        written: dict[tuple[str, ...], int] = {}
        seen: set[tuple[str, ...]] = set()
        for i, request in enumerate(explicit_only):
            chain = tuple(b.sha256 for b in request.blocks or ())
            if chain in seen:
                continue  # an exact re-send is a retry
            seen.add(chain)
            rule = rules[request.span_id]
            if rule is None:
                continue
            ttl_s = (rule.ttl[0].seconds if rule.ttl else None) or _DEFAULT_TTL_S
            tokens = 0
            best_key: tuple[str, ...] = ()
            for earlier in explicit_only[:i]:
                if earlier.model != request.model:
                    continue
                if request.start_unix_nano - earlier.start_unix_nano > ttl_s * 1_000_000_000:
                    continue
                chars, key = shared_prefix(earlier, request)
                shared = prefix_tokens(earlier, chars) if chars else 0
                if shared > tokens:
                    tokens, best_key = shared, key
            if tokens and tokens >= (rule.min_cacheable_tokens or 1024):
                repeats.append((request, tokens))
                written.setdefault(best_key, tokens)
        if len(repeats) < _MIN_REPEATS:
            return []

        first = repeats[0][0]
        rate = rates(rules[first.span_id])
        saved = sum(tokens for _, tokens in repeats) * (1.0 - rate.read)
        premium = sum(written.values()) * ((rate.write or 1.0) - 1.0)
        session_input = sum(r.usage.input_total or 0 for r in session.requests)
        severity = Severity.HIGH if saved >= _HIGH_SHARE * session_input else Severity.MEDIUM
        largest = max(tokens for _, tokens in repeats)
        rule = rules[first.span_id]
        finding = Finding(
            detector_id=self.detector_id,
            severity=severity,
            session_id=session.session_id,
            source=first.source,
            span_ids=tuple(r.span_id for r, _ in repeats),
            summary=(
                f"{len(repeats)} request(s) re-sent a prefix of up to ~{largest:,} tokens "
                "that an earlier request had sent within the cache lifetime, and nothing in "
                f"the session asks {first.provider or 'the provider'} to cache it"
            ),
            evidence={
                "requests_affected": len(repeats),
                "largest_repeated_prefix_tokens": largest,
                "distinct_prefixes": len(written),
                "cache_markers_present": False,
            },
            impact=impact(
                max(0, round(saved - premium)),
                round(saved),
                rate,
                rulebook,
                "each repeat would have been a cache read; low bound subtracts the write "
                "premium for caching each distinct prefix once; prefixes sized from the "
                "earlier request's reported tokens",
            ),
            fix=Fix(
                summary=(
                    "Mark a cache breakpoint at the end of the stable prefix (system prompt, "
                    "tools, shared context), or turn on automatic caching for these requests."
                ),
                hint="in LangChain, send the system prompt as a content block with cache_control",
                doc_url=rule.source_url if rule else None,
            ),
        )
        return [finding]
