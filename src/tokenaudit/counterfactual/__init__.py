"""What a finding's waste costs, measured against the bill under the fix.

Detectors count prompt tokens that were billed at the wrong rate. This module
turns those counts into the ``Impact`` a finding carries, using the model's
own rates from the rulebook:

- a token re-billed at full price instead of read from cache wastes
  ``1 - read`` of a token;
- a token re-billed as a cache write instead of read wastes
  ``write - read``;
- dollars appear only when the rulebook holds a verified flat price.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from tokenaudit.model.records import Request
from tokenaudit.rulebook import ModelRules, Rulebook

if TYPE_CHECKING:
    from tokenaudit.detect.findings import Impact

DEFAULT_READ = 0.1


@dataclass(frozen=True)
class Rates:
    model: str | None
    read: float
    write: float | None
    price_per_mtok: float | None
    known: bool

    def waste(self, request: Request | None = None) -> float:
        """Share of one token's price wasted when it was re-billed, not read."""
        wrote = request is not None and (request.usage.cache_creation or 0) > 0
        if wrote and self.write is not None:
            return self.write - self.read
        return 1.0 - self.read

    @property
    def note(self) -> str:
        if not self.known:
            return f"cached reads assumed at {DEFAULT_READ}x input (model not in the rulebook)"
        parts = [f"cached reads at {self.read}x input"]
        if self.write is not None and self.write != 1.0:
            parts.append(f"cache writes at {self.write}x")
        note = ", ".join(parts) + f" for {self.model}"
        if self.price_per_mtok is not None:
            note += f" at ${self.price_per_mtok}/MTok input"
        return note


def rates(rules: ModelRules | None) -> Rates:
    if rules is None or rules.cache_read_multiplier is None:
        return Rates(rules.model if rules else None, DEFAULT_READ, None, None, False)
    write = rules.ttl[0].write_multiplier if rules.ttl else None
    return Rates(
        rules.model, rules.cache_read_multiplier, write, rules.base_input_price_per_mtok, True
    )


def _usd(tokens: int, price: float | None) -> float | None:
    return round(tokens * price / 1_000_000, 6) if price is not None else None


def impact(low: float, high: float, rate: Rates, rulebook: Rulebook, assumption: str) -> Impact:
    # imported here: the detect package imports this module while it loads
    from tokenaudit.detect.findings import Impact

    tokens_low, tokens_high = round(low), round(high)
    return Impact(
        tokens_low=tokens_low,
        tokens_high=tokens_high,
        usd_low=_usd(tokens_low, rate.price_per_mtok),
        usd_high=_usd(tokens_high, rate.price_per_mtok),
        assumption=f"{assumption}; {rate.note}",
        rulebook_version=rulebook.version,
    )


def _money(value: float) -> str:
    return f"${value:,.2f}" if value >= 1 else f"${value:.4f}"


def format_usd(impact: Impact) -> str | None:
    """``~$0.0012-$0.0030``, or ``None`` when the model has no verified price."""
    if impact.usd_low is None or impact.usd_high is None:
        return None
    if impact.usd_low == impact.usd_high:
        return f"~{_money(impact.usd_high)}"
    return f"~{_money(impact.usd_low)}-{_money(impact.usd_high)}"
