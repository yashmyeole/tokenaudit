"""Compare a fresh run against the committed baseline and decide pass/fail.

Failure conditions (exit code 1 in the CLI):

- a detector fires at HIGH severity that was not HIGH in the baseline;
- the cache read rate drops by more than ``max_regression`` percentage
  points;
- the cache health floor drops by more than ``max_regression`` points
  (only when both runs could compute one — an incomparable score is
  reported, never guessed at);
- with ``strict``, any detector firing that the baseline didn't have.

Everything else — improvements, disappeared findings, newly incomparable
metrics — is reported but does not fail the build. The gate's job is to
catch a change that silently made caching worse, not to demand the
baseline be re-blessed after every improvement.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tokenaudit.ci.baseline import Baseline

_HIGH = "high"


@dataclass(frozen=True)
class MetricDelta:
    name: str
    baseline: float | None
    current: float | None
    delta_points: float | None  # current - baseline, in percentage points


@dataclass(frozen=True)
class CheckReport:
    passed: bool
    failures: tuple[str, ...]
    warnings: tuple[str, ...]
    improvements: tuple[str, ...]
    metrics: tuple[MetricDelta, ...] = field(default_factory=tuple)


def _delta(name: str, base: float | None, cur: float | None) -> MetricDelta:
    points = round((cur - base) * 100, 2) if base is not None and cur is not None else None
    return MetricDelta(name=name, baseline=base, current=cur, delta_points=points)


def compare(
    baseline: Baseline, current: Baseline, max_regression: float, strict: bool = False
) -> CheckReport:
    """``max_regression`` is in percentage points (5.0 = five points)."""
    failures: list[str] = []
    warnings: list[str] = []
    improvements: list[str] = []

    metrics = (
        _delta("cache read rate", baseline.cache_read_rate, current.cache_read_rate),
        _delta("cache health floor", baseline.health_floor, current.health_floor),
    )
    for metric in metrics:
        if metric.delta_points is None:
            missing = "current run" if metric.current is None else "baseline"
            warnings.append(f"{metric.name}: not comparable ({missing} could not compute it)")
        elif metric.delta_points < -max_regression:
            failures.append(
                f"{metric.name} dropped {-metric.delta_points:.1f} points "
                f"({metric.baseline:.1%} -> {metric.current:.1%}), "
                f"more than the allowed {max_regression:.1f}"
            )
        elif metric.delta_points > max_regression:
            improvements.append(
                f"{metric.name} improved {metric.delta_points:.1f} points "
                f"({metric.baseline:.1%} -> {metric.current:.1%}) — "
                "consider refreshing the baseline with --update"
            )

    for detector_id, severity in current.findings.items():
        before = baseline.findings.get(detector_id)
        if severity == _HIGH and before != _HIGH:
            was = f"was {before}" if before else "new"
            failures.append(f"HIGH finding: {detector_id} ({was} in baseline)")
        elif before is None:
            message = f"new finding: {detector_id} ({severity})"
            (failures if strict else warnings).append(message)
    for detector_id, severity in baseline.findings.items():
        if detector_id not in current.findings:
            improvements.append(f"resolved since baseline: {detector_id} (was {severity})")

    if baseline.rulebook_version != current.rulebook_version:
        warnings.append(
            f"rulebook changed since the baseline ({baseline.rulebook_version} -> "
            f"{current.rulebook_version}); metric shifts may reflect rule updates, "
            "not your code"
        )

    return CheckReport(
        passed=not failures,
        failures=tuple(failures),
        warnings=tuple(warnings),
        improvements=tuple(improvements),
        metrics=metrics,
    )


def _pct(value: float | None) -> str:
    return f"{value:.1%}" if value is not None else "n/a"


def render_text(report: CheckReport, current: Baseline) -> str:
    lines = ["PASS: no cache cost regression" if report.passed else "FAIL: cache cost regression"]
    for metric in report.metrics:
        delta = f"{metric.delta_points:+.1f} pts" if metric.delta_points is not None else "n/a"
        lines.append(
            f"  {metric.name}: {_pct(metric.baseline)} -> {_pct(metric.current)} ({delta})"
        )
    lines.append(
        f"  examined: {current.sessions} session(s), {current.requests} requests, "
        f"{current.input_tokens:,} input tokens"
    )
    for failure in report.failures:
        lines.append(f"  FAIL {failure}")
    for warning in report.warnings:
        lines.append(f"  warn {warning}")
    for improvement in report.improvements:
        lines.append(f"  nice {improvement}")
    return "\n".join(lines)


def render_markdown(report: CheckReport, current: Baseline) -> str:
    """A PR-comment-sized summary (also suits ``$GITHUB_STEP_SUMMARY``)."""
    verdict = (
        ":white_check_mark: **No cache cost regression**"
        if report.passed
        else ":x: **Cache cost regression**"
    )
    lines = [
        f"### tokenaudit check — {verdict}",
        "",
        "| Metric | Baseline | Current | Delta |",
        "|---|---|---|---|",
    ]
    for metric in report.metrics:
        delta = f"{metric.delta_points:+.1f} pts" if metric.delta_points is not None else "n/a"
        lines.append(
            f"| {metric.name} | {_pct(metric.baseline)} | {_pct(metric.current)} | {delta} |"
        )
    lines += [
        "",
        f"{current.sessions} session(s), {current.requests} requests, "
        f"{current.input_tokens:,} input tokens examined.",
    ]
    for title, items in (
        ("Failures", report.failures),
        ("Warnings", report.warnings),
        ("Improvements", report.improvements),
    ):
        if items:
            lines += ["", f"**{title}**", ""]
            lines += [f"- {item}" for item in items]
    lines.append("")
    return "\n".join(lines)
