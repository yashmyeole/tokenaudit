"""The engine: run every registered detector over ingested sessions.

Contract enforced here rather than trusted to each detector:

- requests the quality gate marked ``unpriceable`` are removed from the
  session view before any detector sees them, and the count is reported —
  wrong dollar figures are worse than no figures;
- an abstention (with its reason) and a clean pass are kept apart in the
  result, because "not checked" and "checked, nothing found" support very
  different conclusions;
- findings come back ordered by severity, then by impact upper bound.
"""

from __future__ import annotations

from dataclasses import replace

from tokenaudit.detect.base import Abstain, Detector
from tokenaudit.detect.findings import Finding, SessionDiagnosis, Severity
from tokenaudit.detect.rules.model_switch import ModelSwitchDetector
from tokenaudit.detect.rules.prefix_drift import PrefixDriftDetector
from tokenaudit.detect.rules.sub_minimum import SubMinimumDetector
from tokenaudit.model.records import IngestResult, Session
from tokenaudit.rulebook import Rulebook, load_rulebook

DETECTORS: tuple[Detector, ...] = (
    PrefixDriftDetector(),
    SubMinimumDetector(),
    ModelSwitchDetector(),
)

_SEVERITY_ORDER = {Severity.HIGH: 0, Severity.MEDIUM: 1, Severity.LOW: 2}


def _rank(finding: Finding) -> tuple[int, int]:
    impact = finding.impact.tokens_high if finding.impact else 0
    return (_SEVERITY_ORDER[finding.severity], -impact)


def diagnose_session(
    session: Session,
    rulebook: Rulebook,
    detectors: tuple[Detector, ...] = DETECTORS,
) -> SessionDiagnosis:
    priceable = tuple(r for r in session.requests if not r.unpriceable)
    skipped = len(session.requests) - len(priceable)
    view = replace(session, requests=priceable)

    findings: list[Finding] = []
    checked: list[str] = []
    abstained: dict[str, str] = {}
    for detector in detectors:
        try:
            found = detector.check(view, rulebook)
        except Abstain as abstention:
            abstained[detector.detector_id] = abstention.reason
        else:
            checked.append(detector.detector_id)
            findings.extend(found)

    source = session.requests[0].source if session.requests else ""
    return SessionDiagnosis(
        session_id=session.session_id,
        source=source,
        findings=tuple(sorted(findings, key=_rank)),
        checked=tuple(checked),
        abstained=abstained,
        requests_examined=len(priceable),
        requests_skipped_unpriceable=skipped,
    )


def diagnose(result: IngestResult, rulebook: Rulebook | None = None) -> list[SessionDiagnosis]:
    book = rulebook or load_rulebook()
    return [diagnose_session(session, book) for session in result.sessions]
