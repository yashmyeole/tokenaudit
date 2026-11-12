"""Retry storm: byte-identical requests billed repeatedly within a short window.

Client timeouts and non-idempotent re-asks complete server-side and bill in
full; the trace shows the same prompt (identical block hash chain) landing
several times within seconds. Impact comes straight from reported usage —
no estimation — because every duplicate's billed input beyond its cache
reads is waste by definition, whatever the caching setup.
"""

from __future__ import annotations

from tokenaudit.counterfactual import impact, rates
from tokenaudit.detect.base import Detector, require_content, require_timestamps
from tokenaudit.detect.findings import Finding, Fix, Severity
from tokenaudit.model.records import Request, Session
from tokenaudit.rulebook import Rulebook

_MIN_DUPLICATES = 3  # identical requests in a burst (1 original + 2 repeats)
_WINDOW_S = 120


def _chain(request: Request) -> tuple[str, ...]:
    return tuple(block.sha256 for block in request.blocks or ())


class RetryStormDetector(Detector):
    detector_id = "retry_storm"

    def check(self, session: Session, rulebook: Rulebook) -> list[Finding]:
        requests = require_content(session, minimum_requests=_MIN_DUPLICATES)
        require_timestamps(requests)

        bursts: list[list[Request]] = []
        current: list[Request] = []
        for request in requests:
            if (
                current
                and _chain(request) == _chain(current[-1])
                and request.model == current[-1].model
                and (request.start_unix_nano - current[-1].start_unix_nano)
                <= _WINDOW_S * 1_000_000_000
            ):
                current.append(request)
            else:
                if len(current) >= _MIN_DUPLICATES:
                    bursts.append(current)
                current = [request]
        if len(current) >= _MIN_DUPLICATES:
            bursts.append(current)
        if not bursts:
            return []

        findings = []
        for burst in bursts:
            duplicates = burst[1:]
            wasted_input = sum(
                max((r.usage.input_total or 0) - (r.usage.cache_read or 0), 0) for r in duplicates
            )
            wasted_output = sum(r.usage.output or 0 for r in duplicates)
            window_s = (burst[-1].start_unix_nano - burst[0].start_unix_nano) // 1_000_000_000
            findings.append(
                Finding(
                    detector_id=self.detector_id,
                    severity=Severity.HIGH,
                    session_id=session.session_id,
                    source=burst[0].source,
                    span_ids=tuple(r.span_id for r in burst),
                    summary=(
                        f"{len(burst)} byte-identical requests completed within {window_s}s — "
                        f"{len(duplicates)} duplicate(s) billed in full, the signature of "
                        "client-side retries against calls that succeeded server-side"
                    ),
                    evidence={
                        "burst_size": len(burst),
                        "window_seconds": int(window_s),
                        "duplicate_span_ids": [r.span_id for r in duplicates],
                        "wasted_output_tokens": wasted_output,
                        "prompt_chain_sha256": _chain(burst[0])[0],
                    },
                    impact=impact(
                        wasted_input,
                        wasted_input,
                        rates(rulebook.lookup(burst[0].model, burst[0].provider)),
                        rulebook,
                        "duplicates of an identical prompt within the burst window are "
                        "retries; billed input beyond cache reads comes from reported usage, "
                        "no estimation (duplicate output tokens reported separately in "
                        "evidence)",
                    ),
                    fix=Fix(
                        summary=(
                            "Make retries idempotent: raise client timeouts above realistic "
                            "completion times, use request ids / idempotency keys where the "
                            "provider supports them, and back off instead of re-firing."
                        ),
                        hint=None,
                        doc_url=None,
                    ),
                )
            )
        return findings
