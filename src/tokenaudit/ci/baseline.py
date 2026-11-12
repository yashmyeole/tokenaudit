"""The baseline: a small committed snapshot of how well-cached a workload is.

A baseline is aggregate-level on purpose. Session ids, request counts and
token totals all vary run to run in CI, so comparing them would flag noise.
What *is* comparable across runs of the same workload:

- **cache read rate** — cache-read tokens over total input tokens, a ratio.
- **cache health floor** — realized over achievable savings (the same score
  the report shows, aggregated across sessions; see report/health.py).
- **which detectors fire, at what worst severity** — the workload's failure
  modes, independent of run length.

The file is JSON with sorted keys so a regenerated baseline diffs cleanly
in review. Token totals are stored for context but never compared.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from tokenaudit.detect.findings import SessionDiagnosis, Severity
from tokenaudit.model.records import IngestResult
from tokenaudit.report.health import health_score
from tokenaudit.rulebook import Rulebook

BASELINE_FORMAT = 1

_SEVERITY_ORDER = {Severity.LOW: 0, Severity.MEDIUM: 1, Severity.HIGH: 2}


class BaselineError(Exception):
    """The baseline file is missing, malformed, or from a newer format."""


@dataclass(frozen=True)
class Baseline:
    """Everything ``tokenaudit check`` compares, plus context fields."""

    baseline_format: int
    cache_read_rate: float | None  # fraction of input tokens read from cache
    health_floor: float | None  # aggregate realized / (realized + potential_high)
    findings: dict[str, str]  # detector_id -> worst severity observed
    rulebook_version: str
    # context only — never compared (they vary with run length):
    sessions: int
    requests: int
    input_tokens: int
    cache_read_tokens: int


def snapshot(
    results: list[tuple[IngestResult, list[SessionDiagnosis]]],
    rulebook: Rulebook,
    across: SessionDiagnosis | None = None,
) -> Baseline:
    """Reduce a diagnosed run to the comparable aggregate numbers.

    Cross-session findings count toward which detectors fired, not toward
    the health floor: their savings depend on when sessions ran.
    """
    total_input = 0
    total_read = 0
    realized = 0
    potential_high = 0
    session_count = 0
    request_count = 0
    worst: dict[str, Severity] = {}
    for result, diagnoses in results:
        sessions = {s.session_id: s for s in result.sessions}
        for diagnosis in diagnoses:
            session = sessions.get(diagnosis.session_id)
            if session is None:
                continue
            session_count += 1
            request_count += len(session.requests)
            total_input += sum(r.usage.input_total or 0 for r in session.requests)
            total_read += sum(r.usage.cache_read or 0 for r in session.requests)
            score = health_score(session, diagnosis, rulebook)
            realized += score.realized_tokens
            potential_high += score.potential_high
            for finding in diagnosis.findings:
                current = worst.get(finding.detector_id)
                if current is None or _SEVERITY_ORDER[finding.severity] > _SEVERITY_ORDER[current]:
                    worst[finding.detector_id] = finding.severity

    for finding in across.findings if across else ():
        current = worst.get(finding.detector_id)
        if current is None or _SEVERITY_ORDER[finding.severity] > _SEVERITY_ORDER[current]:
            worst[finding.detector_id] = finding.severity

    rate = round(total_read / total_input, 4) if total_input else None
    denominator = realized + potential_high
    floor = round(realized / denominator, 4) if denominator > 0 else None
    return Baseline(
        baseline_format=BASELINE_FORMAT,
        cache_read_rate=rate,
        health_floor=floor,
        findings={d: s.value for d, s in sorted(worst.items())},
        rulebook_version=rulebook.version,
        sessions=session_count,
        requests=request_count,
        input_tokens=total_input,
        cache_read_tokens=total_read,
    )


def write_baseline(baseline: Baseline, path: Path) -> None:
    path.write_text(json.dumps(asdict(baseline), indent=2, sort_keys=True) + "\n", "utf-8")


def read_baseline(path: Path) -> Baseline:
    if not path.is_file():
        raise BaselineError(
            f"{path}: baseline not found — create one from a known-good run with "
            f"`tokenaudit check <traces> --baseline {path} --update`"
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("baseline_format") != BASELINE_FORMAT:
            raise BaselineError(
                f"{path}: baseline_format {raw.get('baseline_format')!r} is not "
                f"{BASELINE_FORMAT}; regenerate with --update"
            )
        return Baseline(**{k: raw[k] for k in Baseline.__dataclass_fields__})
    except BaselineError:
        raise
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise BaselineError(f"{path}: not a valid baseline file ({exc})") from exc
