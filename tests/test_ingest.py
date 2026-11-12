"""Ingestion tests: every corpus dialect in, one normalized shape out.

The strongest assertions here are golden: the three captured TradingAgents
runs must reproduce the token totals and cache hit rates measured at capture
time, after layered-span deduplication — the numbers a naive reader would
double-count.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from tokenaudit.ingest import UnknownTraceFormatError, ingest_path
from tokenaudit.ingest.quality import (
    FLAG_CACHE_EXCEEDS_INPUT,
    FLAG_NO_CONTENT,
    FLAG_PLACEHOLDER_OUTPUT,
)

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "corpus" / "synthetic" / "fixtures"
CAPTURED = ROOT / "corpus" / "captured" / "runs" / "tradingagents"

# Measured at capture time (2026-08-29, TradingAgents @ a33fd4c, gpt-5.4-mini):
# requests after dedup, total prompt tokens, cache-read tokens.
CAPTURED_GOLDEN = {
    "run-01-NVDA-2026-08-14": (16, 82_479, 7_680),
    "run-02-AAPL-2026-08-14": (15, 83_138, 2_560),
    "run-03-NVDA-2026-08-21": (18, 95_994, 17_408),
}


def _label(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / f"{name}.label.json").read_text(encoding="utf-8"))
    return data


def test_every_fixture_ingests_to_its_labeled_totals() -> None:
    for label_path in sorted(FIXTURES.glob("*.label.json")):
        label = _label(label_path.name.removesuffix(".label.json"))
        result = ingest_path(FIXTURES / f"{label['fixture']}.trace.json")
        requests = result.requests
        totals = label["totals"]
        assert len(requests) == totals["calls"], label["fixture"]
        assert result.deduped_span_ids == (), label["fixture"]
        assert sum(r.usage.input_total or 0 for r in requests) == totals["input_tokens"]
        assert sum(r.usage.cache_read or 0 for r in requests) == totals["cache_read_tokens"]
        assert sum(r.usage.cache_creation or 0 for r in requests) == totals["cache_creation_tokens"]
        assert sum(r.usage.output or 0 for r in requests) == totals["output_tokens"]


def test_dialect_variants_normalize_identically() -> None:
    variants = [
        "prefix_drift",
        "prefix_drift_legacy_flat",
        "prefix_drift_openinference",
        "prefix_drift_no_content",
        "prefix_drift_provider_log",
    ]
    sequences = []
    for name in variants:
        requests = ingest_path(FIXTURES / f"{name}.trace.json").requests
        sequences.append(
            [
                (
                    r.usage.input_total,
                    r.usage.cache_read or 0,
                    r.usage.cache_creation or 0,
                    r.usage.output,
                )
                for r in requests
            ]
        )
    assert all(sequence == sequences[0] for sequence in sequences)


def test_raw_provider_log_reads_like_the_trace() -> None:
    traced = ingest_path(FIXTURES / "prefix_drift.trace.json")
    logged = ingest_path(FIXTURES / "prefix_drift_provider_log.trace.json")
    assert logged.container == "provider-log"
    assert [r.blocks for r in logged.requests] == [r.blocks for r in traced.requests]
    assert [r.start_unix_nano for r in logged.requests] == [
        r.start_unix_nano for r in traced.requests
    ]
    assert all("exclusive_counts_converted" in r.quality for r in logged.requests)


def test_captured_runs_reproduce_capture_time_measurements() -> None:
    assert CAPTURED.is_dir(), "captured runs are part of the repo; did a checkout miss them?"
    for run, (calls, prompt_total, cache_read_total) in CAPTURED_GOLDEN.items():
        result = ingest_path(CAPTURED / run / "trace.jsonl")
        assert result.container == "otel-sdk-jsonl"
        requests = result.requests
        assert len(requests) == calls, run
        # every call was double-reported by the LangChain + SDK layers:
        assert len(result.deduped_span_ids) == calls, run
        assert sum(r.usage.input_total or 0 for r in requests) == prompt_total, run
        assert sum(r.usage.cache_read or 0 for r in requests) == cache_read_total, run
        # dedup must keep the layer that knows cache facts (the SDK layer):
        assert all(r.usage.cache_read is not None for r in requests), run


def test_retry_storm_duplicates_are_not_deduped() -> None:
    result = ingest_path(FIXTURES / "retry_storm.trace.json")
    assert len(result.requests) == 5
    assert result.deduped_span_ids == ()


def test_prefix_drift_visible_in_block_hashes() -> None:
    drifting = ingest_path(FIXTURES / "prefix_drift.trace.json").requests
    hashes = {r.blocks[0].sha256 for r in drifting if r.blocks}
    assert len(hashes) == len(drifting)  # block 0 differs on every call
    stable = ingest_path(FIXTURES / "healthy_cached.trace.json").requests
    assert len({r.blocks[0].sha256 for r in stable if r.blocks}) == 1


def test_no_content_is_flagged_but_priceable() -> None:
    requests = ingest_path(FIXTURES / "prefix_drift_no_content.trace.json").requests
    for request in requests:
        assert request.blocks is None
        assert FLAG_NO_CONTENT in request.quality
        assert not request.unpriceable


def test_unknown_formats_are_refused(tmp_path: Path) -> None:
    garbage = tmp_path / "notes.jsonl"
    garbage.write_text("this is not a trace\n", encoding="utf-8")
    with pytest.raises(UnknownTraceFormatError):
        ingest_path(garbage)
    wrong_json = tmp_path / "wrong.trace.json"
    wrong_json.write_text('{"rows": [1, 2, 3]}\n', encoding="utf-8")
    with pytest.raises(UnknownTraceFormatError):
        ingest_path(wrong_json)


def _otlp_span(attrs: dict[str, int | str]) -> str:
    encoded = []
    for key, value in attrs.items():
        if isinstance(value, int):
            encoded.append({"key": key, "value": {"intValue": str(value)}})
        else:
            encoded.append({"key": key, "value": {"stringValue": value}})
    doc = {
        "resourceSpans": [
            {
                "resource": {"attributes": []},
                "scopeSpans": [
                    {
                        "scope": {"name": "t"},
                        "spans": [
                            {
                                "traceId": "a" * 32,
                                "spanId": "b" * 16,
                                "name": "chat x",
                                "kind": 3,
                                "startTimeUnixNano": "1",
                                "endTimeUnixNano": "2",
                                "attributes": encoded,
                                "status": {"code": 1},
                            }
                        ],
                    }
                ],
            }
        ]
    }
    return json.dumps(doc)


def test_impossible_accounting_is_unpriceable(tmp_path: Path) -> None:
    trace = tmp_path / "bad.trace.json"
    trace.write_text(
        _otlp_span(
            {
                "gen_ai.usage.input_tokens": 100,
                "gen_ai.usage.cache_read.input_tokens": 200,
                "gen_ai.usage.output_tokens": 10,
            }
        ),
        encoding="utf-8",
    )
    (request,) = ingest_path(trace).requests
    assert request.unpriceable
    assert FLAG_CACHE_EXCEEDS_INPUT in request.quality


def test_placeholder_outputs_are_flagged(tmp_path: Path) -> None:
    lines = []
    for i in range(6):
        lines.append(
            json.dumps(
                {
                    "name": "Response",
                    "context": {"trace_id": f"0x{'c' * 32}", "span_id": f"0x{i:016x}"},
                    "parent_id": None,
                    "start_time": f"2026-08-29T23:00:{i:02d}.000000Z",
                    "end_time": f"2026-08-29T23:00:{i:02d}.500000Z",
                    "attributes": {
                        "llm.token_count.prompt": 5_000,
                        "llm.token_count.completion": 1,
                    },
                    "resource": {"attributes": {}},
                }
            )
        )
    trace = tmp_path / "placeholders.jsonl"
    trace.write_text("\n".join(lines) + "\n", encoding="utf-8")
    requests = ingest_path(trace).requests
    assert len(requests) == 6
    assert all(FLAG_PLACEHOLDER_OUTPUT in r.quality for r in requests)


def test_ingest_output_never_prints_line_digests(capsys: pytest.CaptureFixture[str]) -> None:
    from tokenaudit.__main__ import main

    fixture = FIXTURES / "misordered_shared_context.trace.json"
    result = ingest_path(fixture)
    block = result.requests[0].blocks[0]  # type: ignore[index]
    assert block.segments and sum(s.chars for s in block.segments) == block.chars
    assert main(["ingest", str(fixture)]) == 0
    printed = capsys.readouterr().out
    assert '"sha256"' in printed and "segments" not in printed


EXPERIMENT = ROOT / "experiments" / "tradingagents-cache-fix" / "runs"


def test_control_runs_logged_raw_usage_now_ingest() -> None:
    if not EXPERIMENT.is_dir():
        pytest.skip("experiment runs live in the repo, not the sdist")
    rates = []
    for run in ("03-baseline-control-NVDA-2026-08-14", "08-baseline-control-NVDA-2026-08-14"):
        result = ingest_path(EXPERIMENT / run / "usage.jsonl")
        assert result.container == "provider-log"
        requests = result.requests
        assert all("no_timestamp" in r.quality and r.blocks is None for r in requests)
        read = sum(r.usage.cache_read or 0 for r in requests)
        rates.append(round(100 * read / sum(r.usage.input_total or 0 for r in requests), 1))
    assert rates == [8.6, 3.4]  # the control figures published in the case study


def test_chat_completions_log_without_timestamps(tmp_path: Path) -> None:
    from tokenaudit.detect import diagnose

    system = "You are a careful assistant.\n" + "\n".join(f"rule {n}" for n in range(500))
    lines = []
    for n in range(3):
        request = {
            "model": "gpt-5.4-mini",
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": f"question {n}"},
            ],
        }
        response = {
            "id": f"chatcmpl-{n}",
            "object": "chat.completion",
            "model": "gpt-5.4-mini",
            "usage": {
                "prompt_tokens": 1300,
                "prompt_tokens_details": {"cached_tokens": 1280 if n else 0},
                "completion_tokens": 40,
            },
        }
        lines.append(json.dumps({"request": request, "response": response}))
    path = tmp_path / "chat.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = ingest_path(path)
    requests = result.requests
    assert [r.usage.cache_read for r in requests] == [0, 1280, 1280]
    assert all(r.blocks and r.blocks[0].role == "system" for r in requests)
    (diagnosis,) = diagnose(result)
    assert "timestamps" in diagnosis.abstained["ttl_expiry"]
    assert "timestamps" in diagnosis.abstained["misordered_shared_context"]
