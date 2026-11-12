"""Report layer: health-score math, corpus records, and the HTML render."""

from pathlib import Path

from tokenaudit.detect.engine import diagnose
from tokenaudit.detect.findings import SessionDiagnosis
from tokenaudit.ingest import ingest_path
from tokenaudit.model.records import IngestResult, Session
from tokenaudit.report import describe, health_score, render_html, render_score
from tokenaudit.rulebook import load_rulebook

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "corpus" / "synthetic" / "fixtures"


def _one(name: str) -> tuple[IngestResult, Session, SessionDiagnosis]:
    result = ingest_path(FIXTURES / f"{name}.trace.json")
    (diagnosis,) = diagnose(result)
    (session,) = result.sessions
    return result, session, diagnosis


def test_health_score_is_a_floor_with_stated_notes() -> None:
    _, session, diagnosis = _one("prefix_drift")
    score = health_score(session, diagnosis, load_rulebook())
    # every call missed cache: nothing realized, plenty achievable -> 0%
    assert score.realized_tokens == 0
    assert score.potential_high > 0
    assert score.score_low == 0.0
    assert any("floor" in note for note in score.notes)
    assert render_score(score) == "0%"


def test_health_score_healthy_session_is_high() -> None:
    _, session, diagnosis = _one("healthy_cached")
    score = health_score(session, diagnosis, load_rulebook())
    assert diagnosis.findings == ()
    assert score.realized_tokens > 0
    assert score.score_low == 1.0  # savings realized, nothing found on the table


def test_corpus_record_attaches_measured_counts() -> None:
    line = describe("prefix_drift")
    assert line is not None
    assert "4/5" in line and "0 false alarms" in line
    assert describe("not_a_detector") is None


def test_html_report_is_self_contained() -> None:
    result, _, _ = _one("prefix_drift")
    html = render_html([(result, diagnose(result))], load_rulebook())
    assert html.startswith("<!doctype html>")
    assert "prefix_drift" in html and "cache health" in html
    assert "corpus record" in html
    # self-contained: no external fetches of any kind
    for marker in ("http://", "https://cdn", "<script", "src=", "@import"):
        assert marker not in html, marker


def test_cli_sample_and_html(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    from tokenaudit.__main__ import main

    out = tmp_path / "report.html"
    code = main(["diagnose", "--sample", "--html", str(out)])
    assert code == 0
    stdout = capsys.readouterr().out
    assert "prefix_drift" in stdout
    assert out.read_text(encoding="utf-8").startswith("<!doctype html>")


def test_session_summary_separates_cache_writes_from_full_price() -> None:
    fixtures = Path(__file__).parent.parent / "corpus" / "synthetic" / "fixtures"
    result = ingest_path(fixtures / "unread_cache_writes.trace.json")
    html = render_html([(result, diagnose(result))], load_rulebook())
    session = result.sessions[0]
    written = sum(r.usage.cache_creation or 0 for r in session.requests)
    total = sum(r.usage.input_total or 0 for r in session.requests)
    assert f"{written:,} written to cache" in html
    assert f"{total - written:,} billed at full input price" in html
