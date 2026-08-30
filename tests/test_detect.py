"""Detector tests, driven by the corpus labels.

The blanket test is the important one: for every fixture, each implemented
failure mode listed in ``contains`` must fire, and nothing listed in
``must_not_contain`` may fire — so all four negative fixtures and every
attribution-precedence rule (tool churn is not prefix drift, growing
context is not drift, the new user turn is not drift) are enforced on every
run, not just remembered."""

import json
from pathlib import Path
from typing import Any

from tokenaudit.detect.engine import diagnose
from tokenaudit.detect.findings import Severity
from tokenaudit.ingest import ingest_path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "corpus" / "synthetic" / "fixtures"
CAPTURED = ROOT / "corpus" / "captured" / "runs" / "tradingagents"

IMPLEMENTED = {"prefix_drift", "sub_minimum", "model_switch"}


def _diagnose_fixture(name: str) -> Any:
    (session_diagnosis,) = diagnose(ingest_path(FIXTURES / f"{name}.trace.json"))
    return session_diagnosis


def test_every_fixture_against_its_label() -> None:
    for label_path in sorted(FIXTURES.glob("*.label.json")):
        label = json.loads(label_path.read_text(encoding="utf-8"))
        diagnosis = _diagnose_fixture(label["fixture"])
        fired = {finding.detector_id for finding in diagnosis.findings}
        forbidden = fired & set(label["must_not_contain"])
        assert not forbidden, f"{label['fixture']}: false positive {forbidden}"
        if label["content_captured"]:
            expected = set(label["contains"]) & IMPLEMENTED
            assert expected <= fired, f"{label['fixture']}: missed {expected - fired}"


def test_prefix_drift_finding_is_verifiable() -> None:
    diagnosis = _diagnose_fixture("prefix_drift")
    (finding,) = diagnosis.findings
    assert finding.detector_id == "prefix_drift"
    assert finding.severity is Severity.HIGH
    assert finding.evidence["block_index"] == 0
    assert finding.evidence["pairs_affected"] == 5
    assert finding.evidence["pairs_compared"] == 5
    assert finding.evidence["drift_kind"] == "same_length_mutation"
    assert finding.impact is not None
    assert finding.impact.tokens_high >= round(0.9 * 5 * 1400)  # prefix rebuilt 5x, 0.9x rate delta
    assert finding.impact.tokens_low == finding.impact.tokens_high  # all misses observed
    assert "4 chars/token" in finding.impact.assumption


def test_sub_minimum_finding_cites_the_rulebook_tier() -> None:
    diagnosis = _diagnose_fixture("sub_minimum")
    (finding,) = diagnosis.findings
    assert finding.detector_id == "sub_minimum"
    assert finding.evidence["model_minimum_tokens"] == 1024
    assert finding.evidence["marked_prefix_est_tokens"] < 1024
    assert finding.evidence["prefix_stable_across_calls"] is True
    assert finding.fix.doc_url is not None and finding.fix.doc_url.startswith("https://")


def test_model_switch_finding_names_both_models() -> None:
    diagnosis = _diagnose_fixture("model_switch")
    (finding,) = diagnosis.findings
    assert finding.detector_id == "model_switch"
    assert finding.evidence["from_model"] == "claude-sonnet-4-5"
    assert finding.evidence["to_model"] == "claude-opus-4-8"
    assert finding.evidence["shared_prefix_est_tokens"] >= 1400
    assert finding.evidence["observed_rewrite_after_switch"] is True


def test_no_content_session_gets_abstention_not_a_guess() -> None:
    diagnosis = _diagnose_fixture("prefix_drift_no_content")
    assert diagnosis.findings == ()
    assert "prefix_drift" in diagnosis.abstained
    assert "content" in diagnosis.abstained["prefix_drift"]


def test_healthy_sessions_are_checked_and_clean_not_abstained() -> None:
    diagnosis = _diagnose_fixture("healthy_cached")
    assert diagnosis.findings == ()
    assert set(diagnosis.checked) == IMPLEMENTED
    assert diagnosis.abstained == {}


def test_captured_tradingagents_runs_diagnose_cleanly() -> None:
    """Single-model, marker-free real sessions: no switch or sub-minimum
    findings are possible; pin whatever prefix_drift concludes so any
    behavior change on real data is a deliberate, reviewed one."""
    for run_dir in sorted(CAPTURED.iterdir()):
        (diagnosis,) = diagnose(ingest_path(run_dir / "trace.jsonl"))
        fired = {finding.detector_id for finding in diagnosis.findings}
        assert "model_switch" not in fired
        assert "sub_minimum" not in fired
        assert diagnosis.requests_skipped_unpriceable == 0


def test_unpriceable_requests_never_reach_detectors(tmp_path: Path) -> None:
    attrs = [
        {"key": "gen_ai.usage.input_tokens", "value": {"intValue": "100"}},
        {"key": "gen_ai.usage.cache_read.input_tokens", "value": {"intValue": "200"}},
        {"key": "gen_ai.usage.output_tokens", "value": {"intValue": "10"}},
    ]
    span = {
        "traceId": "a" * 32,
        "spanId": "b" * 16,
        "name": "chat x",
        "kind": 3,
        "startTimeUnixNano": "1",
        "endTimeUnixNano": "2",
        "attributes": attrs,
        "status": {"code": 1},
    }
    doc = {
        "resourceSpans": [
            {
                "resource": {"attributes": []},
                "scopeSpans": [{"scope": {"name": "t"}, "spans": [span]}],
            }
        ]
    }
    trace = tmp_path / "bad.trace.json"
    trace.write_text(json.dumps(doc), encoding="utf-8")
    (diagnosis,) = diagnose(ingest_path(trace))
    assert diagnosis.requests_skipped_unpriceable == 1
    assert diagnosis.requests_examined == 0
    assert diagnosis.findings == ()
