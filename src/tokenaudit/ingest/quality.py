"""The data-quality gate: validate before anyone prices anything.

A request that fails an accounting invariant is marked ``unpriceable`` and
carries the reason; it is never silently repaired or zero-filled. Detectors
and the counterfactual pricer must refuse ``unpriceable`` requests — a
wrong dollar figure is worse than no dollar figure.

Informational flags (``no_content``, ``cache_fields_absent``) do not make a
request unpriceable; they tell downstream code what it is allowed to
conclude. The placeholder-output check exists because real tooling ships
logs whose output counts are 1-2 token placeholders (claude-code#25941);
pricing those would fabricate money.
"""

from __future__ import annotations

from dataclasses import replace

from tokenaudit.model.records import Request, Session

FLAG_MISSING_INPUT = "missing_input_tokens"
FLAG_NEGATIVE_COUNT = "negative_token_count"
FLAG_CACHE_EXCEEDS_INPUT = "cache_exceeds_input"
FLAG_NO_CONTENT = "no_content"
FLAG_CACHE_FIELDS_ABSENT = "cache_fields_absent"
FLAG_PLACEHOLDER_OUTPUT = "placeholder_output_suspected"

_PLACEHOLDER_MAX = 2
_PLACEHOLDER_MIN_REQUESTS = 5


def assess_request(request: Request) -> Request:
    """Return the request with quality flags and priceability decided."""
    flags = list(request.quality)
    unpriceable = False
    usage = request.usage

    counts = (usage.input_total, usage.cache_read, usage.cache_creation, usage.output)
    if any(c is not None and c < 0 for c in counts):
        flags.append(FLAG_NEGATIVE_COUNT)
        unpriceable = True
    if usage.input_total is None:
        flags.append(FLAG_MISSING_INPUT)
        unpriceable = True
    elif (usage.cache_read or 0) + (usage.cache_creation or 0) > usage.input_total:
        flags.append(FLAG_CACHE_EXCEEDS_INPUT)
        unpriceable = True
    if request.blocks is None:
        flags.append(FLAG_NO_CONTENT)
    if usage.cache_read is None and usage.cache_creation is None:
        flags.append(FLAG_CACHE_FIELDS_ABSENT)

    return replace(request, quality=tuple(flags), unpriceable=unpriceable)


def assess_session(session: Session) -> Session:
    """Session-level checks that no single request can decide alone."""
    requests = session.requests
    if len(requests) >= _PLACEHOLDER_MIN_REQUESTS and all(
        r.usage.output is not None and r.usage.output <= _PLACEHOLDER_MAX for r in requests
    ):
        requests = tuple(
            replace(r, quality=(*r.quality, FLAG_PLACEHOLDER_OUTPUT)) for r in requests
        )
    return replace(session, requests=requests)
