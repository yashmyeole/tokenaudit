"""Tool-schema churn: definitions alternate between versions, flushing the cache.

Tool definitions sit at the very top of the invalidation cascade, and a
mere *reordering* counts as a change. The tell is alternation: a block (or
the tool-schema digest) flips between a small set of previously-seen values
— unlike prefix drift, where every call produces a value never seen before.

Two evidence paths: the tool-schema digest when the trace carries tool
definitions explicitly (OpenInference), else an early prompt block that
reverts to an earlier hash. Guarded against multi-agent traces, where many
distinct schemas legitimately interleave: alternation must involve at most
three distinct values — beyond that it is architecture, not churn, and it
belongs to a session-structure analysis rather than this finding.
"""

from __future__ import annotations

from itertools import pairwise

from tokenaudit.counterfactual import impact, rates
from tokenaudit.detect.base import (
    TOKEN_ESTIMATE_NOTE,
    Detector,
    blocks_tokens,
    require_content,
)
from tokenaudit.detect.findings import Finding, Fix, Severity
from tokenaudit.model.records import Request, Session
from tokenaudit.rulebook import Rulebook

_MAX_DISTINCT = 3


def _alternating_flips(values: list[str | None]) -> tuple[int, int]:
    """(flip count, distinct count) for a sequence with at least one revert."""
    present = [v for v in values if v is not None]
    distinct = len(set(present))
    flips = sum(1 for a, b in pairwise(present) if a != b)
    seen: set[str] = set()
    reverts = 0
    previous: str | None = None
    for value in present:
        if value != previous and value in seen:
            reverts += 1
        seen.add(value)
        previous = value
    return (flips if reverts else 0, distinct)


class ToolSchemaChurnDetector(Detector):
    detector_id = "tool_schema_churn"

    def check(self, session: Session, rulebook: Rulebook) -> list[Finding]:
        requests = require_content(session, minimum_requests=3)

        # Path 1: explicit tool-schema digests. Path 2: the first block.
        schema_values: list[str | None] = [r.tool_schema_sha256 for r in requests]
        if any(v is not None for v in schema_values):
            values, via = schema_values, "tool_schema_sha256"
        else:
            values, via = [(r.blocks or ())[0].sha256 for r in requests], "leading_block_hash"

        flips, distinct = _alternating_flips(values)
        if flips == 0 or distinct > _MAX_DISTINCT:
            return []

        flip_spans: list[Request] = [
            b
            for a, b, va, vb in zip(requests, requests[1:], values, values[1:], strict=False)
            if va is not None and vb is not None and va != vb
        ]
        # Everything under the cascade is invalidated on each flip: the whole
        # prompt of the request after the flip, bounded by what was cacheable.
        example = flip_spans[0]
        rules = rulebook.lookup(example.model, example.provider)
        rate = rates(rules)
        per_flip = [blocks_tokens((r.blocks or ())[:-1]) * rate.waste(r) for r in flip_spans]
        observed = sum(
            1 for r in flip_spans if r.usage.cache_read == 0 or (r.usage.cache_creation or 0) > 0
        )
        finding = Finding(
            detector_id=self.detector_id,
            severity=Severity.MEDIUM,
            session_id=session.session_id,
            source=example.source,
            span_ids=tuple(r.span_id for r in flip_spans),
            summary=(
                f"tool/leading definitions alternate between {distinct} versions "
                f"({flips} flip(s)) — commonly reordered tool definitions or A/B prompt "
                "variants; every flip invalidates the entire cache cascade"
            ),
            evidence={
                "detected_via": via,
                "flips": flips,
                "distinct_versions": distinct,
                "flip_span_ids": [r.span_id for r in flip_spans],
            },
            impact=impact(
                sum(sorted(per_flip)[:observed]),
                sum(per_flip),
                rate,
                rulebook,
                "each flip re-bills the cacheable prompt that would otherwise have been "
                f"read; {TOKEN_ESTIMATE_NOTE}; low bound counts flips with an observed miss",
            ),
            fix=Fix(
                summary=(
                    "Pin tool definitions to one canonical serialization and ordering — sort "
                    "them once at startup, never rebuild the list per call."
                ),
                hint="ordering alone invalidates provider caches",
                doc_url=rules.source_url if rules else None,
            ),
        )
        return [finding]
