"""Pricing a finding against the bill under the fix."""

from pathlib import Path

from tokenaudit.counterfactual import impact, rates
from tokenaudit.detect.engine import diagnose
from tokenaudit.ingest import ingest_path
from tokenaudit.model.records import Request, TokenUsage
from tokenaudit.rulebook import load_rulebook

FIXTURES = Path(__file__).resolve().parent.parent / "corpus" / "synthetic" / "fixtures"


def _with_usage(read: int, write: int) -> Request:
    return Request(
        span_id="s",
        trace_id="t",
        parent_id=None,
        name="chat",
        schema="otel-genai-current",
        provider="anthropic",
        requested_model="claude-sonnet-4-5",
        response_model="claude-sonnet-4-5",
        start_unix_nano=0,
        end_unix_nano=1,
        usage=TokenUsage(input_total=1000, cache_read=read, cache_creation=write, output=10),
        blocks=None,
        tool_schema_sha256=None,
        params_sha256=None,
        conversation_id=None,
        source="unit",
    )


def test_waste_depends_on_how_the_tokens_were_billed() -> None:
    rate = rates(load_rulebook().lookup("claude-sonnet-4-5"))
    assert rate.waste(_with_usage(0, 0)) == 0.9  # full price instead of 0.1x
    assert round(rate.waste(_with_usage(0, 500)), 2) == 1.15  # 1.25x write instead


def test_per_model_read_rates() -> None:
    book = load_rulebook()
    assert round(rates(book.lookup("claude-opus-5-5")).waste(), 3) == 0.95
    assert round(rates(book.lookup("claude-fable-5-2")).waste(), 3) == 0.975
    unknown = rates(book.lookup("not-a-model"))
    assert not unknown.known and unknown.waste() == 0.9
    assert "not in the rulebook" in unknown.note


def test_dollars_only_with_a_verified_flat_price() -> None:
    book = load_rulebook()
    priced = impact(1000, 2000, rates(book.lookup("gpt-5.4-mini")), book, "test")
    assert (priced.usd_low, priced.usd_high) == (0.00075, 0.0015)
    assert "$0.75/MTok" in priced.assumption
    tiered = impact(1000, 2000, rates(book.lookup("gpt-5.6")), book, "test")
    assert tiered.usd_low is None and tiered.usd_high is None


def test_prefix_drift_counts_the_write_premium() -> None:
    (diagnosis,) = diagnose(ingest_path(FIXTURES / "prefix_drift.trace.json"))
    (finding,) = diagnosis.findings
    assert finding.impact is not None
    # every call re-wrote the ~1,400-token prefix at 1.25x instead of reading it at 0.1x
    assert finding.impact.tokens_high >= round(1.15 * 5 * 1400)
    assert finding.impact.usd_high is not None and finding.impact.usd_high > 0


def test_imports_on_its_own() -> None:
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-c", "import tokenaudit.counterfactual"], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
