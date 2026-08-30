"""The CI gate: baseline snapshots, the comparison rules, and exit codes."""

from pathlib import Path

import pytest

from tokenaudit.__main__ import main
from tokenaudit.ci import (
    Baseline,
    BaselineError,
    compare,
    read_baseline,
    snapshot,
    write_baseline,
)
from tokenaudit.detect.engine import diagnose
from tokenaudit.ingest import ingest_path
from tokenaudit.rulebook import load_rulebook

FIXTURES = Path(__file__).resolve().parent.parent / "corpus" / "synthetic" / "fixtures"
HEALTHY = FIXTURES / "healthy_cached.trace.json"
DRIFT = FIXTURES / "prefix_drift.trace.json"


def _snapshot(trace: Path) -> Baseline:
    result = ingest_path(trace)
    return snapshot([(result, diagnose(result))], load_rulebook())


def _baseline(**overrides: object) -> Baseline:
    values: dict[str, object] = {
        "baseline_format": 1,
        "cache_read_rate": 0.50,
        "health_floor": 0.80,
        "findings": {},
        "rulebook_version": "2026.08.29",
        "sessions": 1,
        "requests": 10,
        "input_tokens": 100_000,
        "cache_read_tokens": 50_000,
    }
    values.update(overrides)
    return Baseline(**values)  # type: ignore[arg-type]


# --- snapshot + file round-trip -------------------------------------------


def test_snapshot_is_deterministic_and_round_trips(tmp_path: Path) -> None:
    first, second = _snapshot(HEALTHY), _snapshot(HEALTHY)
    assert first == second
    path = tmp_path / "baseline.json"
    write_baseline(first, path)
    assert read_baseline(path) == first


def test_snapshot_of_a_drifting_run_records_the_high_finding() -> None:
    current = _snapshot(DRIFT)
    assert current.findings.get("prefix_drift") == "high"
    assert current.input_tokens > 0


def test_reading_a_missing_or_malformed_baseline_raises(tmp_path: Path) -> None:
    with pytest.raises(BaselineError, match="--update"):
        read_baseline(tmp_path / "absent.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(BaselineError, match="not a valid baseline"):
        read_baseline(bad)


# --- comparison rules ------------------------------------------------------


def test_a_drop_within_the_allowance_passes_and_beyond_it_fails() -> None:
    baseline = _baseline()
    within = _baseline(cache_read_rate=0.45)  # exactly -5.0 points
    beyond = _baseline(cache_read_rate=0.44)  # -6.0 points
    assert compare(baseline, within, max_regression=5.0).passed
    report = compare(baseline, beyond, max_regression=5.0)
    assert not report.passed
    assert any("cache read rate dropped 6.0 points" in f for f in report.failures)


def test_health_floor_regression_fails_too() -> None:
    report = compare(_baseline(), _baseline(health_floor=0.70), max_regression=5.0)
    assert not report.passed
    assert any("health floor" in f for f in report.failures)


def test_new_high_finding_fails_even_with_no_metric_change() -> None:
    report = compare(_baseline(), _baseline(findings={"prefix_drift": "high"}), 5.0)
    assert not report.passed
    assert any("HIGH finding: prefix_drift (new in baseline)" in f for f in report.failures)


def test_a_finding_escalating_to_high_fails() -> None:
    baseline = _baseline(findings={"retry_storm": "medium"})
    report = compare(baseline, _baseline(findings={"retry_storm": "high"}), 5.0)
    assert not report.passed
    assert any("was medium" in f for f in report.failures)


def test_new_non_high_finding_warns_by_default_and_fails_in_strict() -> None:
    current = _baseline(findings={"sub_minimum": "medium"})
    default = compare(_baseline(), current, 5.0)
    assert default.passed
    assert any("new finding: sub_minimum" in w for w in default.warnings)
    assert not compare(_baseline(), current, 5.0, strict=True).passed


def test_resolved_findings_and_big_improvements_are_reported_not_failed() -> None:
    baseline = _baseline(findings={"prefix_drift": "high"}, cache_read_rate=0.30)
    report = compare(baseline, _baseline(cache_read_rate=0.50), 5.0)
    assert report.passed
    assert any("resolved since baseline: prefix_drift" in i for i in report.improvements)
    assert any("consider refreshing the baseline" in i for i in report.improvements)


def test_incomparable_health_and_rulebook_change_warn_without_failing() -> None:
    baseline = _baseline(health_floor=None, rulebook_version="2026.01.01")
    report = compare(baseline, _baseline(), 5.0)
    assert report.passed
    assert any("not comparable" in w for w in report.warnings)
    assert any("rulebook changed" in w for w in report.warnings)


# --- CLI end to end --------------------------------------------------------


def test_check_update_then_check_passes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    baseline = tmp_path / "baseline.json"
    assert main(["check", str(HEALTHY), "--baseline", str(baseline), "--update"]) == 0
    assert baseline.is_file()
    assert main(["check", str(HEALTHY), "--baseline", str(baseline)]) == 0
    assert "PASS: no cache cost regression" in capsys.readouterr().out


def test_check_fails_the_build_on_regression(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    baseline = tmp_path / "baseline.json"
    write_baseline(_snapshot(HEALTHY), baseline)
    markdown = tmp_path / "summary.md"
    code = main(["check", str(DRIFT), "--baseline", str(baseline), "--markdown", str(markdown)])
    assert code == 1
    out = capsys.readouterr().out
    assert "FAIL" in out and "prefix_drift" in out
    rendered = markdown.read_text(encoding="utf-8")
    assert "| Metric | Baseline | Current | Delta |" in rendered
    assert ":x:" in rendered


def test_check_without_a_baseline_file_exits_2_with_guidance(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["check", str(HEALTHY), "--baseline", str(tmp_path / "none.json")])
    assert code == 2
    assert "--update" in capsys.readouterr().err


def test_check_appends_to_github_step_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline = tmp_path / "baseline.json"
    write_baseline(_snapshot(HEALTHY), baseline)
    summary = tmp_path / "step_summary.md"
    summary.write_text("", encoding="utf-8")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert main(["check", str(HEALTHY), "--baseline", str(baseline)]) == 0
    assert "tokenaudit check" in summary.read_text(encoding="utf-8")
