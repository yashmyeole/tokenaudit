"""Breakpoint limit: more cache markers than the provider honors.

Anthropic honors at most 4 explicit cache breakpoints per request; content
marked beyond the honored set is silently re-billed every call. Modeled as
silent truncation, matching the corpus simulator — a note rides the
evidence because live-API behavior for over-marking should be confirmed
before this claim is made in a published case study.
"""

from __future__ import annotations

from tokenaudit.detect.base import (
    TOKEN_ESTIMATE_NOTE,
    Abstain,
    Detector,
    blocks_tokens,
    require_content,
)
from tokenaudit.detect.findings import Finding, Fix, Impact, Severity
from tokenaudit.model.records import Session
from tokenaudit.rulebook import Rulebook


class BreakpointLimitDetector(Detector):
    detector_id = "breakpoint_limit"

    def check(self, session: Session, rulebook: Rulebook) -> list[Finding]:
        requests = require_content(session, minimum_requests=1)
        over: list[tuple[int, int, int]] = []  # (request idx, marked count, unhonored tokens)
        max_allowed: int | None = None
        for i, request in enumerate(requests):
            blocks = request.blocks or ()
            marked_indices = [j for j, b in enumerate(blocks) if b.cache_marker]
            if not marked_indices:
                continue
            rules = rulebook.lookup(request.model, request.provider)
            if rules is None:
                raise Abstain(f"model not in the rulebook: {request.model}")
            if rules.max_explicit_breakpoints is None:
                continue  # provider has no explicit-marker concept
            max_allowed = rules.max_explicit_breakpoints
            if len(marked_indices) > max_allowed:
                honored_end = marked_indices[max_allowed - 1]
                last_marked = marked_indices[-1]
                unhonored = blocks_tokens(blocks[honored_end + 1 : last_marked + 1])
                over.append((i, len(marked_indices), unhonored))
        if not over or max_allowed is None:
            return []

        repeat_calls = max(len(over) - 1, 1)
        unhonored_tokens = max(tokens for _, _, tokens in over)
        # Stable-tail check: the low bound needs the unhonored region to be
        # identical across calls, otherwise it wouldn't have cached anyway.
        tails = {tuple(b.sha256 for b in (requests[i].blocks or ()))[-2:] for i, _, _ in over}
        tokens_high = round(0.9 * unhonored_tokens * repeat_calls)
        tokens_low = tokens_high if len(tails) == 1 else 0

        first_idx, marked_count, _ = over[0]
        rules = rulebook.lookup(requests[first_idx].model, requests[first_idx].provider)
        finding = Finding(
            detector_id=self.detector_id,
            severity=Severity.MEDIUM,
            session_id=session.session_id,
            source=requests[first_idx].source,
            span_ids=tuple(requests[i].span_id for i, _, _ in over),
            summary=(
                f"{marked_count} blocks carry cache markers but the provider honors at most "
                f"{max_allowed}; ~{unhonored_tokens} tokens of marked content past the limit "
                "are re-billed on every call"
            ),
            evidence={
                "marked_blocks": marked_count,
                "max_explicit_breakpoints": max_allowed,
                "unhonored_est_tokens": unhonored_tokens,
                "calls_affected": len(over),
                "note": (
                    "over-marking modeled as silent truncation of the extra markers; "
                    "confirm live-API behavior before publishing this claim"
                ),
            },
            impact=Impact(
                tokens_low=tokens_low,
                tokens_high=tokens_high,
                usd_low=None,
                usd_high=None,
                assumption=(
                    "the region past the last honored marker would have been cached and read "
                    f"on repeat calls (0.9x delta); {TOKEN_ESTIMATE_NOTE}; low bound requires "
                    "a byte-stable tail"
                ),
                rulebook_version=rulebook.version,
            ),
            fix=Fix(
                summary=(
                    "Consolidate cached content behind at most the honored number of markers "
                    "— merge adjacent stable blocks so one marker covers them."
                ),
                hint=None,
                doc_url=rules.source_url if rules else None,
            ),
        )
        return [finding]
