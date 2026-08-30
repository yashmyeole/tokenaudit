"""Model switch: an identical warm prefix thrown away at a model boundary.

Provider caches are keyed per model, so switching models mid-session
discards every cached prefix even when the prompt is byte-identical. Fires
only when the blocks on both sides of the boundary actually share the
cacheable prefix — a switch accompanied by a different prompt didn't lose
anything to the switch itself."""

from __future__ import annotations

from itertools import pairwise

from tokenaudit.detect.base import (
    TOKEN_ESTIMATE_NOTE,
    Abstain,
    Detector,
    blocks_tokens,
    last_marked_index,
    require_content,
)
from tokenaudit.detect.findings import Finding, Fix, Impact, Severity
from tokenaudit.model.records import Request, Session
from tokenaudit.rulebook import Rulebook


def _shared_prefix_tokens(a: Request, b: Request) -> int:
    """Tokens of the identical cacheable prefix across the boundary."""
    a_blocks = a.blocks or ()
    b_blocks = b.blocks or ()
    shared = 0
    for i in range(min(len(a_blocks), len(b_blocks))):
        if a_blocks[i].sha256 != b_blocks[i].sha256:
            break
        shared = i + 1
    if shared == 0:
        return 0
    end = last_marked_index(b_blocks)
    limit = shared if end is None else min(shared, end + 1)
    return blocks_tokens(b_blocks[:limit])


class ModelSwitchDetector(Detector):
    detector_id = "model_switch"

    def check(self, session: Session, rulebook: Rulebook) -> list[Finding]:
        requests = require_content(session, minimum_requests=2)
        boundaries: list[tuple[Request, Request, int]] = []
        for a, b in pairwise(requests):
            if a.model and b.model and a.model != b.model:
                shared = _shared_prefix_tokens(a, b)
                if shared > 0:
                    boundaries.append((a, b, shared))
        if not boundaries:
            return []
        if len({r.model for r in requests}) > 2 or len(boundaries) > 2:
            # Frequent alternation looks like deliberate model tiering; the
            # fix differs (cache per tier), so don't blame "a switch".
            raise Abstain(
                "more than two models or repeated alternation: looks like deliberate "
                "tiering, which needs per-tier analysis rather than a switch finding"
            )

        findings = []
        for a, b, shared in boundaries:
            observed_rewrite = (b.usage.cache_creation or 0) > 0 or b.usage.cache_read == 0
            rules = rulebook.lookup(b.model, b.provider)
            findings.append(
                Finding(
                    detector_id=self.detector_id,
                    severity=Severity.MEDIUM,
                    session_id=session.session_id,
                    source=b.source,
                    span_ids=(a.span_id, b.span_id),
                    summary=(
                        f"the session switches from {a.model} to {b.model} with an identical "
                        f"~{shared}-token prefix; caches are per-model, so the warm prefix is "
                        "discarded and rebuilt"
                    ),
                    evidence={
                        "from_model": a.model,
                        "to_model": b.model,
                        "shared_prefix_est_tokens": shared,
                        "observed_rewrite_after_switch": observed_rewrite,
                    },
                    impact=Impact(
                        tokens_low=shared if observed_rewrite else 0,
                        tokens_high=shared,
                        usd_low=None,
                        usd_high=None,
                        assumption=(
                            "the shared prefix is re-billed once at the new model instead of "
                            f"read from cache; {TOKEN_ESTIMATE_NOTE}; low bound requires an "
                            "observed rewrite after the switch"
                        ),
                        rulebook_version=rulebook.version,
                    ),
                    fix=Fix(
                        summary=(
                            "Pin the session to one model, or accept one rebuild per deliberate "
                            "switch and keep switches out of hot loops."
                        ),
                        hint=None,
                        doc_url=rules.source_url if rules else None,
                    ),
                )
            )
        return findings
