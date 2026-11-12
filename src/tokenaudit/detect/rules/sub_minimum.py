"""Sub-minimum cache blocks: marked for caching, silently too small to cache.

Minimum cacheable prompt length is model-tiered (512/1024/2048/4096 on
Anthropic models as of the rulebook's verification date) and the tiers do
not track model size — so teams get it wrong, and the failure is perfectly
silent: the provider ignores the marker, bills full price, and returns no
error. The only symptom is cache fields that stay at zero forever.

Fires when a session marks blocks for caching, the marked prefix sits below
the model's minimum, and no cache activity was ever observed. The stable
content is expressly NOT prefix drift; the corpus encodes that as a
must-not."""

from __future__ import annotations

from tokenaudit.counterfactual import impact, rates
from tokenaudit.detect.base import (
    TOKEN_ESTIMATE_NOTE,
    Abstain,
    Detector,
    blocks_tokens,
    last_marked_index,
    require_content,
)
from tokenaudit.detect.findings import Finding, Fix, Severity
from tokenaudit.model.records import Session
from tokenaudit.rulebook import Rulebook


class SubMinimumDetector(Detector):
    detector_id = "sub_minimum"

    def check(self, session: Session, rulebook: Rulebook) -> list[Finding]:
        requests = require_content(session, minimum_requests=2)
        marked = [r for r in requests if last_marked_index(r.blocks or ()) is not None]
        if not marked:
            return []  # nothing marked for caching: checked, clean

        models = {r.model for r in marked}
        rules_by_model = {m: rulebook.lookup(m, marked[0].provider) for m in models}
        if any(rules is None for rules in rules_by_model.values()):
            unknown = sorted(m or "?" for m, rules in rules_by_model.items() if rules is None)
            raise Abstain(f"model(s) not in the rulebook, minimum unknown: {unknown}")
        unpublished = sorted(
            m or "?"
            for m, rules in rules_by_model.items()
            if rules and rules.min_cacheable_tokens is None
        )
        if unpublished:
            raise Abstain(f"no published minimum for: {unpublished}")

        # The marked prefix must be under the minimum on every call, and the
        # session must show zero cache activity — one observed write or read
        # means the prefix does cache and this diagnosis would be wrong.
        prefixes = []
        for r in marked:
            blocks = r.blocks or ()
            end = last_marked_index(blocks)
            assert end is not None  # guarded above
            prefixes.append((r, blocks_tokens(blocks[: end + 1])))
        minimum = max(
            rules.min_cacheable_tokens
            for rules in rules_by_model.values()
            if rules is not None and rules.min_cacheable_tokens is not None
        )
        if any(tokens >= minimum for _, tokens in prefixes):
            return []
        if any((r.usage.cache_read or 0) > 0 or (r.usage.cache_creation or 0) > 0 for r in marked):
            return []

        example, example_tokens = prefixes[0]
        rules = rules_by_model[example.model]
        assert rules is not None
        rate = rates(rules)
        est = max(tokens for _, tokens in prefixes)
        repeat_calls = len(marked) - 1
        high = est * repeat_calls * rate.waste()
        stable = len({(r.blocks or ())[0].sha256 for r in marked}) == 1
        low = high if stable else 0
        finding = Finding(
            detector_id=self.detector_id,
            severity=Severity.MEDIUM,
            session_id=session.session_id,
            source=example.source,
            span_ids=tuple(r.span_id for r, _ in prefixes),
            summary=(
                f"blocks are marked for caching but the marked prefix (~{example_tokens} tokens) "
                f"is below this model's {minimum}-token minimum, so the provider silently "
                "ignores the marker on every call"
            ),
            evidence={
                "marked_prefix_est_tokens": example_tokens,
                "model_minimum_tokens": minimum,
                "models": sorted(m or "?" for m in models),
                "calls_marked": len(marked),
                "observed_cache_reads": 0,
                "observed_cache_writes": 0,
                "prefix_stable_across_calls": stable,
            },
            impact=impact(
                low,
                high,
                rate,
                rulebook,
                "savings forgone if the prefix cleared the minimum and repeat calls read it "
                f"at the cached rate; {TOKEN_ESTIMATE_NOTE}; low bound requires the prefix "
                "to be byte-stable across calls",
            ),
            fix=Fix(
                summary=(
                    "Grow the cached block past the model's minimum (fold stable context into "
                    "it) or stop paying attention to a marker the provider ignores; minimums "
                    "are model-tiered, so also check the tier when switching models."
                ),
                hint=f"minimum for {example.model} is {minimum} tokens",
                doc_url=rules.source_url,
            ),
        )
        return [finding]
