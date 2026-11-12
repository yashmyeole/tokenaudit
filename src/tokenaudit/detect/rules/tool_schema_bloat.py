"""Tool-schema bloat: tool definitions sent on every request and never called.

Definitions are input on every call, cached or not. An agent handed a shared
registry of twenty tools that only ever calls two pays for the other
eighteen each time.

Requests are grouped by the tool set they carry, and a tool counts as used
if any request in its group called it. That catches one agent looping over
a large toolbox and a registry handed to every agent alike. It does not
catch a registry where every tool is used by some agent but each agent uses
only a few; telling agents apart needs more than the trace says.
"""

from __future__ import annotations

from collections import defaultdict

from tokenaudit.counterfactual import Rates, impact, rates
from tokenaudit.detect.base import CHARS_PER_TOKEN, Abstain, Detector
from tokenaudit.detect.findings import Finding, Fix, Severity
from tokenaudit.model.records import Request, Session
from tokenaudit.rulebook import Rulebook

_MIN_REQUESTS = 3
_MIN_UNUSED_TOKENS = 1000
_MIN_UNUSED_SHARE = 0.5
_MEDIUM_SHARE = 0.05
_MAX_NAMES = 10


def _paid_rate(request: Request, rate: Rates, tool_chars: int) -> float:
    """What the definitions cost on this request, per token, relative to full input."""
    if (request.usage.cache_read or 0) * CHARS_PER_TOKEN >= tool_chars:
        return rate.read
    if (request.usage.cache_creation or 0) > 0 and rate.write is not None:
        return rate.write
    return 1.0


class ToolSchemaBloatDetector(Detector):
    detector_id = "tool_schema_bloat"

    def check(self, session: Session, rulebook: Rulebook) -> list[Finding]:
        tooled = [r for r in session.requests if r.tool_defs]
        if len(tooled) < _MIN_REQUESTS:
            raise Abstain(f"needs tool definitions on at least {_MIN_REQUESTS} requests")

        groups: dict[tuple[str, ...], list[Request]] = defaultdict(list)
        for request in tooled:
            groups[tuple(t.name for t in request.tool_defs or ())].append(request)
        known = {
            names: requests
            for names, requests in groups.items()
            if all(r.tool_calls is not None for r in requests)
        }
        if not known:
            raise Abstain("needs the tool calls in each response, and this source has none")

        flagged: list[Request] = []
        unused_names: set[str] = set()
        low = high = 0.0
        for requests in known.values():
            if len(requests) < _MIN_REQUESTS:
                continue
            called = {name for r in requests for name in r.tool_calls or ()}
            for request in requests:
                defs = request.tool_defs or ()
                unused = [t for t in defs if t.name not in called]
                unused_chars = sum(t.chars for t in unused)
                all_chars = sum(t.chars for t in defs)
                unused_tokens = unused_chars // CHARS_PER_TOKEN
                if unused_tokens < _MIN_UNUSED_TOKENS:
                    continue
                if unused_chars < _MIN_UNUSED_SHARE * all_chars:
                    continue
                rate = rates(rulebook.lookup(request.model, request.provider))
                low += unused_tokens * rate.read
                high += unused_tokens * _paid_rate(request, rate, all_chars)
                flagged.append(request)
                unused_names.update(t.name for t in unused)
        if not flagged:
            return []

        first = flagged[0]
        rate = rates(rulebook.lookup(first.model, first.provider))
        session_input = sum(r.usage.input_total or 0 for r in session.requests)
        severity = Severity.MEDIUM if high >= _MEDIUM_SHARE * session_input else Severity.LOW
        names = sorted(unused_names)
        per_request = high / len(flagged)
        finding = Finding(
            detector_id=self.detector_id,
            severity=severity,
            session_id=session.session_id,
            source=first.source,
            span_ids=tuple(r.span_id for r in flagged),
            summary=(
                f"{len(names)} tool definition(s) were sent on {len(flagged)} request(s) "
                f"and never called (~{per_request:,.0f} full-price tokens per request)"
            ),
            evidence={
                "unused_tools": names[:_MAX_NAMES],
                "unused_tool_count": len(names),
                "requests": len(flagged),
            },
            impact=impact(
                round(low),
                round(high),
                rate,
                rulebook,
                "removing the unused definitions saves what each request paid for them; "
                "low bound prices them as cached reads, high bound at the rate the request "
                "actually paid; definition sizes estimated at ~4 chars/token",
            ),
            fix=Fix(
                summary=(
                    "Give each agent only the tools it calls, and load rarely used tools "
                    "on demand instead of sending the whole registry with every request."
                ),
                hint="definitions are billed as input on every call, cached or not",
            ),
        )
        return [finding]
