"""Cross-session drift: a per-session value near the start of a long prompt.

A date, user or ticker in the first lines of a system prompt keeps it stable
within a session, but every new session pays again for the identical text
after it.

Fires when the same block (same position, role, model, and identical blocks
before it) has one variant per session, and the variants differ only in a
short early region (at most a quarter of the block) followed by at least
1,024 identical tokens. A value that changes on every call is prefix drift.

The low impact bound counts only sessions that started within the model's
cache lifetime of a session using another variant.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from tokenaudit.counterfactual import impact, rates
from tokenaudit.detect.base import (
    TOKEN_ESTIMATE_NOTE,
    Abstain,
    CrossSessionDetector,
    block_segments,
    common_prefix_chars,
    est_tokens,
    require_timestamps,
)
from tokenaudit.detect.findings import Finding, Fix, Severity
from tokenaudit.model.records import BlockDigest, Request, Segment, Session
from tokenaudit.rulebook import Rulebook

MIN_TAIL_TOKENS = 1024
MAX_VOLATILE_SHARE = 0.25
DEFAULT_WINDOW_S = 300
_TURN_ROLES = {"user", "human", "tool"}


@dataclass(frozen=True)
class _Use:
    session_key: str
    request: Request
    block_index: int
    block: BlockDigest


def _common_suffix_chars(a: Sequence[Segment], b: Sequence[Segment]) -> int:
    return common_prefix_chars(a[::-1], b[::-1])


def _session_key(session: Session) -> str:
    source = session.requests[0].source if session.requests else ""
    return f"{source}#{session.session_id}"


def _candidate_blocks(request: Request) -> list[tuple[int, BlockDigest]]:
    blocks = request.blocks or ()
    out = []
    for i, block in enumerate(blocks):
        trailing_turn = i == len(blocks) - 1 and block.role in _TURN_ROLES
        if not trailing_turn and est_tokens(block.chars) >= MIN_TAIL_TOKENS:
            out.append((i, block))
    return out


class CrossSessionDriftDetector(CrossSessionDetector):
    detector_id = "cross_session_drift"

    def check_sessions(self, sessions: Sequence[Session], rulebook: Rulebook) -> list[Finding]:
        with_content = [s for s in sessions if any(r.blocks for r in s.requests)]
        if len(with_content) < 2:
            raise Abstain("needs at least 2 sessions with captured message content")
        require_timestamps([r for s in with_content for r in s.requests])

        # First use of each distinct block per session, grouped by what comes
        # before it and how it ends: variants of one templated prompt share
        # their preceding blocks and their last line.
        groups: dict[tuple[str, int, str, str, str], dict[str, _Use]] = defaultdict(dict)
        for session in with_content:
            key = _session_key(session)
            for request in session.requests:
                blocks = request.blocks or ()
                for index, block in _candidate_blocks(request):
                    segments = block_segments(block)
                    if not segments:
                        continue
                    before = "".join(b.sha256 for b in blocks[:index])
                    before_key = hashlib.sha256(before.encode()).hexdigest()
                    group = (
                        request.model or "",
                        index,
                        block.role,
                        segments[-1].sha256,
                        before_key,
                    )
                    uses = groups[group]
                    use_key = f"{key}\x00{block.sha256}"
                    if use_key not in uses:
                        uses[use_key] = _Use(key, request, index, block)

        findings = []
        for (model, index, role, _, _), uses in sorted(groups.items()):
            finding = self._check_group(model, index, role, list(uses.values()), rulebook)
            if finding is not None:
                findings.append(finding)
        return findings

    def _check_group(
        self, model: str, index: int, role: str, uses: list[_Use], rulebook: Rulebook
    ) -> Finding | None:
        sessions = {u.session_key for u in uses}
        variants: dict[str, list[_Use]] = defaultdict(list)
        for use in uses:
            variants[use.block.sha256].append(use)
        if len(sessions) < 2 or len(variants) < 2:
            return None
        per_session: dict[str, set[str]] = defaultdict(set)
        for use in uses:
            per_session[use.session_key].add(use.block.sha256)
        if any(len(shas) > 1 for shas in per_session.values()):
            return None  # changes call to call: prefix drift, not a per-session value

        reference = max(variants.values(), key=len)[0].block
        ref_segments = block_segments(reference)
        head = tail = None
        for sha, group in variants.items():
            if sha == reference.sha256:
                continue
            variant_segments = block_segments(group[0].block)
            pair_head = common_prefix_chars(ref_segments, variant_segments)
            pair_tail = _common_suffix_chars(ref_segments, variant_segments)
            head = pair_head if head is None else min(head, pair_head)
            tail = pair_tail if tail is None else min(tail, pair_tail)
        assert head is not None and tail is not None
        volatile = reference.chars - head - tail
        if (
            est_tokens(tail) < MIN_TAIL_TOKENS
            or tail <= head
            or volatile > MAX_VOLATILE_SHARE * reference.chars
        ):
            return None

        first = min(uses, key=lambda u: u.request.start_unix_nano)
        rules = rulebook.lookup(first.request.model, first.request.provider)
        rate = rates(rules)
        window_s = DEFAULT_WINDOW_S
        if rules and rules.ttl and rules.ttl[0].seconds:
            window_s = rules.ttl[0].seconds
        window_ns = window_s * 1_000_000_000

        per_session_saving = est_tokens(tail) * rate.waste()
        later = sorted(
            (u for u in uses if u.session_key != first.session_key),
            key=lambda u: u.request.start_unix_nano,
        )
        later_sessions: dict[str, _Use] = {}
        for use in later:
            later_sessions.setdefault(use.session_key, use)
        warm = [
            use
            for use in later_sessions.values()
            if any(
                other.block.sha256 != use.block.sha256
                and 0 <= use.request.start_unix_nano - other.request.start_unix_nano <= window_ns
                for other in uses
            )
        ]
        tokens_high = per_session_saving * len(later_sessions)
        tokens_low = per_session_saving * len(warm)

        other = next(u for u in uses if u.block.sha256 != reference.sha256)
        return Finding(
            detector_id=self.detector_id,
            severity=Severity.HIGH if warm else Severity.MEDIUM,
            session_id="across-sessions",
            source=first.request.source,
            span_ids=tuple(u.request.span_id for u in uses),
            summary=(
                f"a {role} block of ~{est_tokens(reference.chars):,} tokens differs across "
                f"{len(sessions)} sessions only in a short region near its start; the "
                f"~{est_tokens(tail):,} tokens after it are identical but can't be reused "
                "across sessions"
            ),
            evidence={
                "model": model,
                "block_index": index,
                "block_role": role,
                "block_chars": reference.chars,
                "sessions": len(sessions),
                "variants": len(variants),
                "shared_head_chars": head,
                "volatile_chars": volatile,
                "shared_tail_tokens": est_tokens(tail),
                "sessions_within_cache_window": len(warm),
                "example_sha256": [reference.sha256, other.block.sha256],
                "example_span_ids": [
                    variants[reference.sha256][0].request.span_id,
                    other.request.span_id,
                ],
            },
            impact=impact(
                tokens_low,
                tokens_high,
                rate,
                rulebook,
                "each session after the first reads the shared tail from cache once; "
                f"{TOKEN_ESTIMATE_NOTE}; low bound counts only sessions that started within "
                f"{window_s}s of a session using another variant",
            ),
            fix=Fix(
                summary=(
                    "Move per-session values (date, user, account, ticker) out of the start "
                    "of the prompt: put them at the end of the system prompt or in the "
                    "first user message."
                ),
                hint=(
                    f"the variants match for the first {head:,} chars, differ for about "
                    f"{volatile:,}, then match again to the end"
                ),
                doc_url=rules.source_url if rules else None,
            ),
        )
