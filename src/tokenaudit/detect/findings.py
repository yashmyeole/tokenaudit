"""The diagnosis result schema — frozen early, because everything downstream
(report rendering, the eval harness, the CI gate) builds on this shape.

Design commitments:

- A finding is a **claim with evidence**: every field a stranger would need
  to verify it by hand against the raw trace is present, or the finding
  should not exist.
- ``Impact`` is a **bounded range with a stated assumption**, denominated in
  full-price-equivalent input tokens; dollars appear only when the rulebook
  holds a verified price for the model. No point estimates, no unstated
  extrapolation, and the rulebook version that produced the numbers rides
  along.
- ``confidence`` is **measured, never asserted**: it stays ``None`` until
  the evaluation harness computes per-detector precision against the
  labeled corpus and attaches it at report time.
- A detector that could not examine a session **abstains with a reason** —
  a different fact from "checked and found nothing", and the report must
  keep the two apart.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Severity(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass(frozen=True)
class Impact:
    """Waste attributed to one finding, as a bounded range.

    ``tokens_*`` are full-price-equivalent input tokens: the amount of input
    that was (or will keep being) billed at the full rate but would have
    been served at the cached rate under the fix. One finding at a time —
    impacts of different findings on the same session are not additive.
    """

    tokens_low: int
    tokens_high: int
    usd_low: float | None
    usd_high: float | None
    assumption: str
    rulebook_version: str


@dataclass(frozen=True)
class Fix:
    summary: str
    hint: str | None = None
    doc_url: str | None = None


@dataclass(frozen=True)
class Finding:
    """One diagnosed cause of waste in one session."""

    detector_id: str
    severity: Severity
    session_id: str
    source: str
    span_ids: tuple[str, ...]
    summary: str
    evidence: dict[str, Any]
    impact: Impact | None  # None when the quality gate blocked pricing
    fix: Fix
    confidence: float | None = None  # measured precision, attached by the eval harness


@dataclass(frozen=True)
class SessionDiagnosis:
    """Everything the engine concluded about one session."""

    session_id: str
    source: str
    findings: tuple[Finding, ...]
    checked: tuple[str, ...]  # detectors that ran (clean if not in findings)
    abstained: dict[str, str] = field(default_factory=dict)  # detector_id -> reason
    requests_examined: int = 0
    requests_skipped_unpriceable: int = 0
