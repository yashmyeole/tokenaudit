"""The cache health score: realized savings over achievable savings.

Deliberately a ratio of two quantities computed the same way, so the answer
to "how did you weight that?" is "I didn't":

- **realized** — full-price-equivalent tokens actually saved: observed
  cache reads times each model's (1 - read multiplier) from the rulebook.
- **achievable** — realized plus what the findings say was left on the
  table (their impact bounds).

Finding impacts overlap (one fix at a time — they are not additive), so
summing them makes the denominator conservative and the score a **floor**;
the report says so. Models without a verified read multiplier contribute
nothing to "realized" and add a note instead of a guess.
"""

from __future__ import annotations

from dataclasses import dataclass

from tokenaudit.detect.findings import SessionDiagnosis
from tokenaudit.model.records import Session
from tokenaudit.rulebook import Rulebook


@dataclass(frozen=True)
class HealthScore:
    realized_tokens: int
    potential_low: int
    potential_high: int
    score_low: float | None  # floor: realized / (realized + potential_high)
    score_high: float | None
    notes: tuple[str, ...]


def health_score(session: Session, diagnosis: SessionDiagnosis, rulebook: Rulebook) -> HealthScore:
    realized = 0.0
    unknown_multiplier = False
    for request in session.requests:
        read = request.usage.cache_read or 0
        if read == 0:
            continue
        rules = rulebook.lookup(request.model, request.provider)
        if rules is None or rules.cache_read_multiplier is None:
            unknown_multiplier = True
            continue
        realized += read * (1.0 - rules.cache_read_multiplier)

    potential_low = sum(f.impact.tokens_low for f in diagnosis.findings if f.impact)
    potential_high = sum(f.impact.tokens_high for f in diagnosis.findings if f.impact)

    notes = ["overlapping findings make the score a floor, not a point estimate"]
    if unknown_multiplier:
        notes.append(
            "some models lack a verified cache-read multiplier; their reads are "
            "excluded from realized savings rather than guessed"
        )
    if diagnosis.abstained:
        notes.append(
            f"{len(diagnosis.abstained)} detector(s) abstained; unchecked failure "
            "modes are not in the denominator"
        )

    realized_int = round(realized)
    denominator_high = realized_int + potential_high
    denominator_low = realized_int + potential_low
    score_low = realized_int / denominator_high if denominator_high > 0 else None
    score_high = realized_int / denominator_low if denominator_low > 0 else None
    return HealthScore(
        realized_tokens=realized_int,
        potential_low=potential_low,
        potential_high=potential_high,
        score_low=score_low,
        score_high=score_high,
        notes=tuple(notes),
    )


def render_score(score: HealthScore) -> str:
    """One-line human rendering, honest about the unknown case."""
    if score.score_low is None:
        return "n/a (no cache reads observed and no quantified findings)"
    low = f"{score.score_low:.0%}"
    if score.score_high is not None and round(score.score_high, 2) != round(score.score_low, 2):
        return f"{low}-{score.score_high:.0%}"
    return low
