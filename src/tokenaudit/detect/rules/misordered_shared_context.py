"""Misordered shared context: the same material behind different openings.

Multi-agent pipelines often send several agents the same reports but open
each prompt with that agent's own role text. Prefix caches match from the
first byte, so the shared part is billed in full every time.

Compares per-line digests against same-model requests from the last 5
minutes. Only shared content past the request's best real prefix match
counts, and it must be at least 1,024 tokens (or the model's minimum). Runs
already covered by reported cache reads are skipped, and openings that
differ by a short edit are left to prefix drift.
"""

from __future__ import annotations

from dataclasses import dataclass

from tokenaudit.counterfactual import impact, rates
from tokenaudit.detect.base import (
    CHARS_PER_TOKEN,
    TOKEN_ESTIMATE_NOTE,
    Detector,
    SharedRun,
    common_prefix_chars,
    est_tokens,
    longest_shared_run,
    request_segments,
    require_content,
    require_timestamps,
)
from tokenaudit.detect.findings import Finding, Fix, Severity
from tokenaudit.model.records import Request, Segment, Session
from tokenaudit.rulebook import Rulebook

WINDOW_NS = 300 * 1_000_000_000
MIN_SHARED_TOKENS = 1024
MUTATION_EDIT_CHARS = 512


@dataclass(frozen=True)
class _Match:
    request: Request
    earlier: Request
    run: SharedRun
    misplaced_chars: int  # shared chars past the request's best real prefix match
    request_chars: int


def _unmatched_chars(head: tuple[Segment, ...], other: tuple[Segment, ...]) -> int:
    seen = {s.sha256 for s in other}
    return sum(s.chars for s in head if s.sha256 not in seen)


def _is_small_edit(a: tuple[Segment, ...], b: tuple[Segment, ...], run: SharedRun) -> bool:
    head_a, head_b = a[: run.index_a], b[: _b_index(b, run.start_b)]
    return (
        _unmatched_chars(head_a, head_b) <= MUTATION_EDIT_CHARS
        and _unmatched_chars(head_b, head_a) <= MUTATION_EDIT_CHARS
    )


def _b_index(segments: tuple[Segment, ...], start_chars: int) -> int:
    position = 0
    for i, segment in enumerate(segments):
        if position >= start_chars:
            return i
        position += segment.chars
    return len(segments)


class MisorderedSharedContextDetector(Detector):
    detector_id = "misordered_shared_context"

    def check(self, session: Session, rulebook: Rulebook) -> list[Finding]:
        requests = require_content(session, minimum_requests=2)
        require_timestamps(requests)
        segments = {r.span_id: request_segments(r) for r in requests}

        matches: list[_Match] = []
        for i, request in enumerate(requests):
            rules = rulebook.lookup(request.model, request.provider)
            minimum = max(MIN_SHARED_TOKENS, (rules.min_cacheable_tokens or 0) if rules else 0)
            mine = segments[request.span_id]
            window = [
                e
                for e in requests[:i]
                if e.model == request.model
                and request.start_unix_nano - e.start_unix_nano <= WINDOW_NS
            ]
            prefix = max(
                (common_prefix_chars(mine, segments[e.span_id]) for e in window), default=0
            )
            reads = request.usage.cache_read
            best: _Match | None = None
            for earlier in window:
                theirs = segments[earlier.span_id]
                run = longest_shared_run(mine, theirs)
                if run is None:
                    continue
                end = run.start_a + run.chars
                misplaced = end - max(prefix, run.start_a)
                if misplaced <= 0 or est_tokens(misplaced) < minimum:
                    continue  # already served by a real common prefix
                if reads is not None and reads * CHARS_PER_TOKEN >= end:
                    continue  # the provider reports reading it from cache anyway
                if _is_small_edit(mine, theirs, run):
                    continue  # a mutated value, not a different opening
                if best is None or misplaced > best.misplaced_chars:
                    best = _Match(request, earlier, run, misplaced, sum(s.chars for s in mine))
            if best is not None:
                matches.append(best)

        if not matches:
            return []

        example = max(matches, key=lambda m: m.misplaced_chars)
        rules = rulebook.lookup(example.request.model, example.request.provider)
        rate = rates(rules)

        per_request = [est_tokens(m.misplaced_chars) * rate.waste(m.request) for m in matches]
        observed = [
            tokens
            for m, tokens in zip(matches, per_request, strict=True)
            if m.request.usage.cache_read is not None
            and m.request.usage.cache_read * CHARS_PER_TOKEN < m.run.start_a + m.run.chars
        ]
        tokens_high = sum(per_request)
        session_input = sum(r.usage.input_total or 0 for r in session.requests)
        severity = (
            Severity.HIGH
            if session_input and tokens_high >= 0.1 * session_input
            else Severity.MEDIUM
        )
        shared_total = sum(est_tokens(m.misplaced_chars) for m in matches)

        return [
            Finding(
                detector_id=self.detector_id,
                severity=severity,
                session_id=session.session_id,
                source=example.request.source,
                span_ids=tuple(m.request.span_id for m in matches),
                summary=(
                    f"{len(matches)} request(s) resend ~{shared_total:,} tokens that an earlier "
                    "request already sent, but behind a different opening, so the prefix "
                    "cache can't reuse them"
                ),
                evidence={
                    "requests_affected": len(matches),
                    "shared_tokens_total": shared_total,
                    "example_span_id": example.request.span_id,
                    "example_earlier_span_id": example.earlier.span_id,
                    "example_shared_tokens": est_tokens(example.misplaced_chars),
                    "example_offset_tokens": est_tokens(example.run.start_a),
                    "example_earlier_offset_tokens": est_tokens(example.run.start_b),
                    "example_request_tokens": est_tokens(example.request_chars),
                },
                impact=impact(
                    sum(observed),
                    tokens_high,
                    rate,
                    rulebook,
                    f"the shared run becomes a common prefix and is read from cache; "
                    f"{TOKEN_ESTIMATE_NOTE}; low bound counts only requests whose reported "
                    "cache reads stop before the shared run",
                ),
                fix=Fix(
                    summary=(
                        "Put the shared material first, in the same order in every prompt "
                        "that carries it, and move each agent's own instructions after it."
                    ),
                    hint=(
                        "the shared run starts ~"
                        f"{est_tokens(example.run.start_a):,} tokens into one prompt and "
                        f"~{est_tokens(example.run.start_b):,} into the other"
                    ),
                    doc_url=rules.source_url if rules else None,
                ),
            )
        ]
