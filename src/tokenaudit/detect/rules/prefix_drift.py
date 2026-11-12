"""Prefix drift: the cacheable start of the prompt keeps changing.

The classic silent killer — a timestamp, counter, or mutating tracker
embedded early in the prompt makes the prefix miss byte-identical matching
on every call, so the session pays cache writes (or full price) forever.

Three guards keep this detector from crying wolf, each earned against a
labeled negative case:

- **Trailing-block exclusion**: the last block changing is just the new
  user turn (every healthy conversation does it). A divergence only counts
  when it sits before the final block or on a cache-marked block.
- **Size-similarity guard**: drift means the *same* block mutated slightly
  (equal or near-equal length, different hash). A block replaced by
  something of a very different size is a different prompt, not drift —
  which is what consecutive calls from different agent roles look like in
  real multi-agent traces.
- **Same-index majority**: the divergence must recur at the same block
  index in at least half of the compared pairs (and at least twice). A
  one-off change — e.g. tool definitions reordered once — is attributed by
  a different detector; ground truth in the corpus encodes that precedence.
"""

from __future__ import annotations

from tokenaudit.counterfactual import impact, rates
from tokenaudit.detect.base import (
    TOKEN_ESTIMATE_NOTE,
    Abstain,
    Detector,
    Pair,
    blocks_tokens,
    consecutive_pairs,
    last_marked_index,
)
from tokenaudit.detect.findings import Finding, Fix, Severity
from tokenaudit.model.records import Session
from tokenaudit.rulebook import Rulebook

_SIMILAR_ABS = 32  # chars
_SIMILAR_FRAC = 0.02
_TURN_ROLES = {"user", "human", "tool"}  # roles that legitimately change every call


def _similar_size(a_chars: int, b_chars: int) -> bool:
    return abs(a_chars - b_chars) <= max(_SIMILAR_ABS, int(_SIMILAR_FRAC * max(a_chars, b_chars)))


def _qualifying_divergence(pair: Pair) -> int | None:
    d = pair.first_divergence
    if d is None:
        return None
    a = pair.first.blocks or ()
    b = pair.second.blocks or ()
    trailing = d >= min(len(a), len(b)) - 1
    if trailing and not a[d].cache_marker and a[d].role in _TURN_ROLES:
        return None  # the new user turn changing is not drift; a lone mutating
        # system prompt (single-block prompts exist in legacy traces) still is
    if a[d].role != b[d].role or not _similar_size(a[d].chars, b[d].chars):
        return None  # replaced content, not a mutated block
    return d


class PrefixDriftDetector(Detector):
    detector_id = "prefix_drift"

    def check(self, session: Session, rulebook: Rulebook) -> list[Finding]:
        pairs = consecutive_pairs(session)
        if len(pairs) < 2:
            raise Abstain("needs at least 3 same-model requests with captured content")

        by_index: dict[int, list[Pair]] = {}
        for pair in pairs:
            d = _qualifying_divergence(pair)
            if d is not None:
                by_index.setdefault(d, []).append(pair)
        if not by_index:
            return []

        index, affected = max(by_index.items(), key=lambda item: len(item[1]))
        if len(affected) < 2 or len(affected) * 2 < len(pairs):
            return []  # no recurring same-index drift; one-off changes belong elsewhere

        # Counterfactual, one fix at a time: with a stable prefix, everything
        # from the divergence point through the cacheable region would have
        # been read at the cached rate instead of re-billed.
        example = affected[0]
        rules = rulebook.lookup(example.second.model, example.second.provider)
        rate = rates(rules)
        per_pair: list[float] = []
        observed_misses = 0
        for pair in affected:
            blocks = pair.second.blocks or ()
            end = last_marked_index(blocks)
            end = end if end is not None and end >= index else len(blocks) - 2
            per_pair.append(blocks_tokens(blocks[index : end + 1]) * rate.waste(pair.second))
            usage = pair.second.usage
            if usage.cache_read == 0 or (
                usage.cache_creation is not None and usage.cache_creation > 0
            ):
                observed_misses += 1

        a = example.first.blocks or ()
        b = example.second.blocks or ()
        drift_kind = (
            "same_length_mutation" if a[index].chars == b[index].chars else "small_mutation"
        )

        finding = Finding(
            detector_id=self.detector_id,
            severity=Severity.HIGH,
            session_id=session.session_id,
            source=example.second.source,
            span_ids=tuple(p.second.span_id for p in affected),
            summary=(
                f"the prompt prefix changes at block {index} in {len(affected)} of "
                f"{len(pairs)} consecutive calls, so the cacheable prefix never matches"
            ),
            evidence={
                "block_index": index,
                "block_role": a[index].role,
                "block_chars": a[index].chars,
                "drift_kind": drift_kind,
                "pairs_affected": len(affected),
                "pairs_compared": len(pairs),
                "observed_cache_misses": observed_misses,
                "example_before_sha256": a[index].sha256,
                "example_after_sha256": b[index].sha256,
                "example_span_ids": [example.first.span_id, example.second.span_id],
            },
            impact=impact(
                sum(sorted(per_pair)[:observed_misses]),
                sum(per_pair),
                rate,
                rulebook,
                "the invalidated prefix region is read from cache instead of re-billed; "
                f"{TOKEN_ESTIMATE_NOTE}; low bound counts only pairs with an observed "
                "cache miss",
            ),
            fix=Fix(
                summary=(
                    "Move the changing value out of the cached prefix — put timestamps, "
                    "counters and per-call state in the user message, not the system prompt."
                ),
                hint=(
                    "the mutating block keeps its length, which is what an embedded "
                    "timestamp or counter looks like"
                    if drift_kind == "same_length_mutation"
                    else "the mutating block changes size slightly on each call"
                ),
                doc_url=rules.source_url if rules else None,
            ),
        )
        return [finding]
