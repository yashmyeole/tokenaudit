"""The demo app: samples prerender, uploads diagnose in memory, limits hold.

Skipped when the demo dependencies aren't installed (they are demo-only;
the library itself has none): pip install -r demo/requirements.txt
"""

import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from demo.app import app

ROOT = Path(__file__).resolve().parent.parent
DRIFT = ROOT / "corpus" / "synthetic" / "fixtures" / "prefix_drift.trace.json"

client = TestClient(app)


def test_index_serves_the_page_with_the_privacy_statement() -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "never written to disk" in response.text


def test_both_samples_are_prerendered_reports() -> None:
    for name in ("drift", "tradingagents"):
        response = client.get(f"/api/sample/{name}")
        assert response.status_code == 200, name
        assert "tokenaudit report" in response.text
    assert client.get("/api/sample/nope").status_code == 404


def test_tradingagents_sample_shows_the_real_hit_rates() -> None:
    text = client.get("/api/sample/tradingagents").text
    assert "9.3%" in text and "18.1%" in text  # the captured runs' real numbers


def test_upload_returns_a_report_with_the_finding() -> None:
    with DRIFT.open("rb") as handle:
        response = client.post(
            "/api/diagnose", files=[("traces", ("prefix_drift.trace.json", handle))]
        )
    assert response.status_code == 200
    assert "prefix_drift" in response.text
    assert "<script" not in response.text  # reports stay script-free


def test_raw_provider_log_upload_is_diagnosed() -> None:
    log = ROOT / "corpus" / "synthetic" / "fixtures" / "tool_schema_bloat_provider_log.trace.json"
    response = client.post(
        "/api/diagnose", files=[("traces", ("requests.jsonl", log.read_bytes()))]
    )
    assert response.status_code == 200
    assert "tool_schema_bloat" in response.text


def test_unrecognized_upload_is_refused_loudly_not_zeroed() -> None:
    response = client.post(
        "/api/diagnose", files=[("traces", ("notes.json", b'{"hello": "world"}'))]
    )
    assert response.status_code == 422
    assert "notes.json" in response.json()["detail"]


def test_non_utf8_upload_is_a_400() -> None:
    response = client.post("/api/diagnose", files=[("traces", ("blob.json", b"\xff\xfe\x00"))])
    assert response.status_code == 400


def test_oversized_upload_is_rejected_before_parsing() -> None:
    big = json.dumps({"resourceSpans": []}).encode() + b" " * (16 * 1024 * 1024)
    response = client.post("/api/diagnose", files=[("traces", ("big.json", big))])
    assert response.status_code == 413
