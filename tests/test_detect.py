"""Detector tests, driven by the corpus labels.

The blanket test is the important one: for every fixture, each implemented
failure mode listed in ``contains`` must fire, and nothing listed in
``must_not_contain`` may fire — so all four negative fixtures and every
attribution-precedence rule (tool churn is not prefix drift, growing
context is not drift, the new user turn is not drift) are enforced on every
run, not just remembered."""

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from tokenaudit.detect.engine import diagnose, diagnose_across
from tokenaudit.detect.findings import Severity
from tokenaudit.detect.rules.misordered_shared_context import MisorderedSharedContextDetector
from tokenaudit.ingest import ingest_path
from tokenaudit.model.records import BlockDigest, Request, Segment, Session, TokenUsage
from tokenaudit.rulebook import load_rulebook

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "corpus" / "synthetic" / "fixtures"
CAPTURED = ROOT / "corpus" / "captured" / "runs" / "tradingagents"

PER_SESSION = {
    "prefix_drift",
    "sub_minimum",
    "model_switch",
    "ttl_expiry",
    "breakpoint_limit",
    "lookback_overflow",
    "tool_schema_churn",
    "context_bloat",
    "retry_storm",
    "misordered_shared_context",
    "tool_schema_bloat",
    "missing_cache_markers",
    "unread_cache_writes",
}
IMPLEMENTED = PER_SESSION | {"cross_session_drift"}


def _diagnose_fixture(name: str) -> Any:
    (session_diagnosis,) = diagnose(ingest_path(FIXTURES / f"{name}.trace.json"))
    return session_diagnosis


def _fired(name: str) -> set[str]:
    result = ingest_path(FIXTURES / f"{name}.trace.json")
    diagnoses = [*diagnose(result), diagnose_across([result])]
    return {f.detector_id for d in diagnoses for f in d.findings}


def test_every_fixture_against_its_label() -> None:
    for label_path in sorted(FIXTURES.glob("*.label.json")):
        label = json.loads(label_path.read_text(encoding="utf-8"))
        fired = _fired(label["fixture"])
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
    assert set(diagnosis.checked) == PER_SESSION - {"tool_schema_bloat"}
    assert set(diagnosis.abstained) == {"tool_schema_bloat"}  # this agent has no tools


def test_misordered_shared_context_finding_is_verifiable() -> None:
    diagnosis = _diagnose_fixture("misordered_shared_context")
    (finding,) = diagnosis.findings
    assert finding.detector_id == "misordered_shared_context"
    assert finding.evidence["requests_affected"] == 2
    assert finding.evidence["example_offset_tokens"] > 0
    assert finding.evidence["example_shared_tokens"] >= 2600
    assert finding.impact is not None
    assert finding.impact.tokens_low == 0  # the trace never reports cache reads
    assert finding.impact.tokens_high > 4000
    assert "cached reads at 0.1x input" in finding.impact.assumption
    assert "gpt-5.6" in finding.impact.assumption


def test_tool_schema_bloat_finding_is_verifiable() -> None:
    diagnosis = _diagnose_fixture("tool_schema_bloat")
    (finding,) = diagnosis.findings
    assert finding.detector_id == "tool_schema_bloat"
    assert finding.severity is Severity.MEDIUM
    assert finding.evidence["unused_tool_count"] == 10
    assert "search_docs" not in finding.evidence["unused_tools"]
    assert finding.evidence["requests"] == 6
    assert finding.impact is not None
    # one cache write at 1.25x, then five reads at 0.1x
    assert finding.impact.tokens_low < finding.impact.tokens_high
    assert finding.impact.usd_high is not None


def test_tool_schema_bloat_counts_a_tool_used_once_as_used() -> None:
    diagnosis = _diagnose_fixture("tool_schema_rotating")
    assert diagnosis.findings == ()
    assert "tool_schema_bloat" in diagnosis.checked


def test_tool_schema_bloat_abstains_without_tool_calls() -> None:
    from tokenaudit.detect.rules.tool_schema_bloat import ToolSchemaBloatDetector

    (session,) = ingest_path(FIXTURES / "tool_schema_bloat.trace.json").sessions
    blind = replace(session, requests=tuple(replace(r, tool_calls=None) for r in session.requests))
    with pytest.raises(Exception, match="tool calls"):
        ToolSchemaBloatDetector().check(blind, load_rulebook())


def test_missing_cache_markers_finding_is_verifiable() -> None:
    diagnosis = _diagnose_fixture("missing_cache_markers")
    (finding,) = diagnosis.findings
    assert finding.detector_id == "missing_cache_markers"
    assert finding.severity is Severity.HIGH
    assert finding.evidence["requests_affected"] == 4
    assert finding.evidence["largest_repeated_prefix_tokens"] >= 2600
    assert finding.evidence["distinct_prefixes"] == 1
    assert finding.impact is not None
    # four repeats at 0.9x, less one 0.25x write premium
    assert finding.impact.tokens_low < finding.impact.tokens_high
    assert finding.fix.doc_url is not None


def test_missing_cache_markers_ignores_repeats_after_the_cache_would_expire() -> None:
    diagnosis = _diagnose_fixture("unmarked_far_apart")
    assert diagnosis.findings == ()
    assert "missing_cache_markers" in diagnosis.checked


def test_missing_cache_markers_counts_tools_and_needs_the_same_tools() -> None:
    from tokenaudit.detect.base import shared_prefix
    from tokenaudit.model.records import ToolDef

    (session,) = ingest_path(FIXTURES / "missing_cache_markers.trace.json").sessions
    a, b = session.requests[:2]
    tools = (ToolDef("search", 800),)
    plain, _ = shared_prefix(a, b)
    with_tools, _ = shared_prefix(replace(a, tool_defs=tools), replace(b, tool_defs=tools))
    assert with_tools == plain + 800
    assert shared_prefix(replace(a, tool_defs=tools), b) == (0, ())


def test_unread_cache_writes_finding_is_verifiable() -> None:
    diagnosis = _diagnose_fixture("unread_cache_writes")
    (finding,) = diagnosis.findings
    assert finding.detector_id == "unread_cache_writes"
    assert finding.severity is Severity.HIGH  # nothing was read, so caching only cost money
    assert finding.evidence["requests_affected"] == 5
    assert finding.evidence["read_saving_tokens"] == 0
    assert finding.impact is not None
    # the 0.25x write premium on everything written
    assert finding.impact.tokens_low == round(finding.evidence["unread_write_tokens"] * 0.25)


def test_unread_cache_writes_ignores_writes_that_get_read() -> None:
    diagnosis = _diagnose_fixture("cache_writes_reused")
    assert diagnosis.findings == ()
    assert "unread_cache_writes" in diagnosis.checked


def test_unread_cache_writes_is_clean_when_nothing_asked_for_caching() -> None:
    diagnosis = _diagnose_fixture("missing_cache_markers")
    assert "unread_cache_writes" in diagnosis.checked


def test_cross_session_drift_finding_is_verifiable() -> None:
    result = ingest_path(FIXTURES / "cross_session_drift.trace.json")
    assert len(result.sessions) == 4
    across = diagnose_across([result])
    (finding,) = across.findings
    assert finding.detector_id == "cross_session_drift"
    assert finding.severity is Severity.HIGH  # the sessions ran a minute apart
    assert finding.evidence["sessions"] == 4
    assert finding.evidence["variants"] == 4
    assert finding.evidence["shared_head_chars"] == 0
    assert finding.evidence["volatile_chars"] < 100
    assert finding.evidence["shared_tail_tokens"] >= 1400
    assert finding.evidence["sessions_within_cache_window"] == 3
    assert finding.impact is not None
    assert finding.impact.tokens_low == finding.impact.tokens_high


def test_cross_session_drift_abstains_on_one_session() -> None:
    across = diagnose_across([ingest_path(FIXTURES / "healthy_cached.trace.json")])
    assert across.findings == ()
    assert "cross_session_drift" in across.abstained


def _block(role: str, text: str) -> BlockDigest:
    def digest(value: str) -> str:
        return hashlib.sha256(f"{role}\x00{value}".encode()).hexdigest()

    lines = text.split("\n")
    segments = tuple(
        Segment(digest(line)[:16], len(line) + (1 if i < len(lines) - 1 else 0))
        for i, line in enumerate(lines)
    )
    return BlockDigest(role, digest(text), len(text), segments=segments)


def _request(i: int, text: str) -> Request:
    return Request(
        span_id=f"s{i}",
        trace_id="t",
        parent_id=None,
        name="chat",
        schema="otel-genai-current",
        provider="openai",
        requested_model="gpt-5.6",
        response_model="gpt-5.6",
        start_unix_nano=i * 10_000_000_000,
        end_unix_nano=i * 10_000_000_000 + 1,
        usage=TokenUsage(input_total=len(text) // 4, cache_read=0, cache_creation=0, output=10),
        blocks=(_block("user", text),),
        tool_schema_sha256=None,
        params_sha256=None,
        conversation_id="c",
        source="unit",
    )


def test_a_changed_timestamp_is_drift_not_misordering() -> None:
    body = "\n".join(f"policy line {n}: keep the same wording every call" for n in range(900))
    session = Session(
        "c",
        "conversation_id",
        tuple(_request(i, f"[generated 09:0{i}:00]\n{body}") for i in range(3)),
    )
    assert MisorderedSharedContextDetector().check(session, load_rulebook()) == []


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
        # bear, research manager, the three risk debaters and the portfolio manager
        (misordered,) = [
            f for f in diagnosis.findings if f.detector_id == "misordered_shared_context"
        ]
        assert misordered.evidence["requests_affected"] == 6
        # each analyst only gets its own tools and calls all of them
        assert "tool_schema_bloat" in diagnosis.checked
        assert "tool_schema_bloat" not in fired


def test_captured_runs_show_the_date_in_the_analyst_prompt() -> None:
    results = [ingest_path(run / "trace.jsonl") for run in sorted(CAPTURED.iterdir())]
    (finding,) = diagnose_across(results).findings
    assert finding.detector_id == "cross_session_drift"
    assert finding.evidence["block_role"] == "system"
    assert finding.evidence["sessions"] == 3
    assert finding.evidence["shared_head_chars"] == 0


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


def test_ttl_expiry_names_the_gap_and_the_window() -> None:
    diagnosis = _diagnose_fixture("ttl_expiry")
    (finding,) = diagnosis.findings
    assert finding.detector_id == "ttl_expiry"
    (event,) = finding.evidence["events"]
    assert event["gap_seconds"] == 420
    assert event["ttl_seconds"] == 300
    assert finding.evidence["ttl_semantics"] == "from_request_start"


def test_breakpoint_limit_counts_markers_against_the_rulebook() -> None:
    diagnosis = _diagnose_fixture("breakpoint_limit")
    (finding,) = diagnosis.findings
    assert finding.detector_id == "breakpoint_limit"
    assert finding.evidence["marked_blocks"] == 6
    assert finding.evidence["max_explicit_breakpoints"] == 4
    assert finding.evidence["unhonored_est_tokens"] >= 1400  # two ~700-token docs


def test_lookback_overflow_measures_the_distance() -> None:
    diagnosis = _diagnose_fixture("lookback_overflow")
    (finding,) = diagnosis.findings
    assert finding.detector_id == "lookback_overflow"
    events = finding.evidence["events"]
    assert len(events) == 2
    assert all(e["prefix_distance"] >= e["lookback_positions"] for e in events)


def test_tool_schema_churn_detects_alternation_not_drift() -> None:
    diagnosis = _diagnose_fixture("tool_schema_churn")
    (finding,) = diagnosis.findings
    assert finding.detector_id == "tool_schema_churn"
    assert finding.evidence["distinct_versions"] == 2
    assert finding.evidence["flips"] == 2
    assert finding.evidence["detected_via"] == "leading_block_hash"


def test_context_bloat_works_from_usage_alone() -> None:
    diagnosis = _diagnose_fixture("context_bloat")
    (finding,) = diagnosis.findings
    assert finding.detector_id == "context_bloat"
    assert finding.evidence["cache_read_ratio"] == 0.0
    assert finding.evidence["input_last"] > 2 * finding.evidence["input_first"]
    assert finding.impact is not None and "no estimates" not in finding.impact.assumption
    # and it runs even when content capture is off:
    no_content = _diagnose_fixture("prefix_drift_no_content")
    assert "context_bloat" in no_content.checked


def test_retry_storm_counts_duplicates_from_usage() -> None:
    diagnosis = _diagnose_fixture("retry_storm")
    (finding,) = diagnosis.findings
    assert finding.detector_id == "retry_storm"
    assert finding.evidence["burst_size"] == 4
    assert len(finding.evidence["duplicate_span_ids"]) == 3
    assert finding.impact is not None
    assert finding.impact.tokens_low == finding.impact.tokens_high  # usage-derived, no range


def test_cross_session_drift_needs_identical_blocks_before_the_variant() -> None:
    from tokenaudit.detect.rules.cross_session_drift import CrossSessionDriftDetector

    tail = "\n".join(f"company profile line {n}: unchanged reference text" for n in range(400))
    sessions = []
    for s, day in enumerate(("14", "21")):
        request = _request(s, "")
        blocks = (
            _block("system", f"agent setup for run {s}"),
            _block("tool", f"fetched 2026-08-{day}\n{tail}"),
            _block("user", "next step"),
        )
        session_request = replace(request, blocks=blocks, conversation_id=f"c{s}")
        sessions.append(Session(f"c{s}", "conversation_id", (session_request,)))
    assert CrossSessionDriftDetector().check_sessions(sessions, load_rulebook()) == []


EXPERIMENT = ROOT / "experiments" / "tradingagents-cache-fix" / "runs"


def _arm(arm: str) -> list[Any]:
    if not EXPERIMENT.is_dir():
        pytest.skip("experiment runs live in the repo, not the sdist")
    runs = sorted(
        d for d in EXPERIMENT.iterdir() if f"-{arm}-" in d.name and "control" not in d.name
    )
    return [ingest_path(next(d.glob("*.jsonl"))) for d in runs]


def test_the_august_patch_moves_both_findings_the_right_way() -> None:
    def misordered(results: list[Any]) -> list[int]:
        counts = []
        for result in results:
            (diagnosis,) = diagnose(result)
            found = [f for f in diagnosis.findings if f.detector_id == "misordered_shared_context"]
            counts.append(found[0].evidence["requests_affected"] if found else 0)
        return counts

    baseline, patched = _arm("baseline"), _arm("patched")
    assert misordered(baseline) == [6, 6, 6]
    assert misordered(patched) == [3, 3, 3]  # research manager, trader, portfolio manager
    assert [f.detector_id for f in diagnose_across(baseline).findings] == ["cross_session_drift"]
    assert diagnose_across(patched).findings == ()  # the patch moved the date to the end
