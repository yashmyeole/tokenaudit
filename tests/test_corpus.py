"""Integrity tests for the synthetic corpus.

These tests keep the committed fixtures honest: every trace has a label,
labels partition the failure-mode space, the three schema dialects of the
same session agree on totals, and regenerating the corpus reproduces the
committed bytes exactly (so the generator and the fixtures can never drift
apart silently).
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "corpus" / "synthetic" / "fixtures"
GENERATOR = ROOT / "corpus" / "synthetic" / "generate.py"

# Keep in sync with FAILURE_MODES in generate.py; the determinism test below
# regenerates from the generator, so a drift here fails loudly.
FAILURE_MODES = {
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
    "cross_session_drift",
}


def _load(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def _labels() -> list[dict[str, Any]]:
    return [_load(p) for p in sorted(FIXTURES.glob("*.label.json"))]


def _span_attrs(trace: dict[str, Any]) -> list[dict[str, int | str | bool]]:
    """Flatten every span's attributes into simple dicts."""
    spans: list[dict[str, int | str | bool]] = []
    for rs in trace["resourceSpans"]:
        for ss in rs["scopeSpans"]:
            for span in ss["spans"]:
                attrs: dict[str, int | str | bool] = {}
                for a in span["attributes"]:
                    value = a["value"]
                    if "intValue" in value:
                        attrs[a["key"]] = int(value["intValue"])
                    elif "boolValue" in value:
                        attrs[a["key"]] = bool(value["boolValue"])
                    else:
                        attrs[a["key"]] = str(value["stringValue"])
                spans.append(attrs)
    return spans


def test_manifest_matches_files() -> None:
    manifest = _load(FIXTURES / "manifest.json")
    listed = {f["fixture"] for f in manifest["fixtures"]}
    on_disk = {p.name.removesuffix(".trace.json") for p in FIXTURES.glob("*.trace.json")}
    assert listed == on_disk


def test_every_trace_has_a_label() -> None:
    traces = {p.name.removesuffix(".trace.json") for p in FIXTURES.glob("*.trace.json")}
    labels = {p.name.removesuffix(".label.json") for p in FIXTURES.glob("*.label.json")}
    assert traces == labels
    assert len(traces) >= 13


def test_labels_partition_the_failure_mode_space() -> None:
    for label in _labels():
        contains = set(label["contains"])
        must_not = set(label["must_not_contain"])
        assert contains <= FAILURE_MODES, label["fixture"]
        assert contains.isdisjoint(must_not), label["fixture"]
        assert contains | must_not == FAILURE_MODES, label["fixture"]


def test_negative_fixtures_contain_nothing() -> None:
    negatives = [lb for lb in _labels() if lb["fixture"].startswith(("healthy", "openai_healthy"))]
    assert len(negatives) == 4
    for label in negatives:
        assert label["contains"] == [], label["fixture"]


def test_current_gen_accounting_is_nested() -> None:
    """Current semconv: gen_ai.usage.input_tokens INCLUDES cache tokens."""
    for label in _labels():
        if label["schema"] != "otel-genai-current":
            continue
        trace = _load(FIXTURES / f"{label['fixture']}.trace.json")
        for attrs in _span_attrs(trace):
            read = int(attrs.get("gen_ai.usage.cache_read.input_tokens", 0))
            written = int(attrs.get("gen_ai.usage.cache_creation.input_tokens", 0))
            total = int(attrs["gen_ai.usage.input_tokens"])
            assert total >= read + written, label["fixture"]


def test_schema_dialects_agree_on_totals() -> None:
    """The same session emitted in three dialects must describe the same money."""
    variants = [
        "prefix_drift",
        "prefix_drift_legacy_flat",
        "prefix_drift_openinference",
        "prefix_drift_provider_log",
    ]
    totals = [_load(FIXTURES / f"{v}.label.json")["totals"] for v in variants]
    assert totals[0] == totals[1] == totals[2] == totals[3]


def test_legacy_prompt_tokens_exclude_cache() -> None:
    """The legacy passthrough trap: prompt_tokens + cache fields == true total."""
    label = _load(FIXTURES / "prefix_drift_legacy_flat.label.json")
    trace = _load(FIXTURES / "prefix_drift_legacy_flat.trace.json")
    summed = 0
    for attrs in _span_attrs(trace):
        summed += (
            int(attrs["gen_ai.usage.prompt_tokens"])
            + int(attrs["gen_ai.usage.cache_read_input_tokens"])
            + int(attrs["gen_ai.usage.cache_creation_input_tokens"])
        )
    assert summed == label["totals"]["input_tokens"]


def test_no_content_variant_has_no_messages() -> None:
    trace = _load(FIXTURES / "prefix_drift_no_content.trace.json")
    for attrs in _span_attrs(trace):
        assert "gen_ai.input.messages" not in attrs
        assert "gen_ai.output.messages" not in attrs


def test_all_fixtures_are_marked_synthetic() -> None:
    for path in FIXTURES.glob("*.trace.json"):
        records = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(records, list):  # raw provider log
            assert all(record["synthetic"] is True for record in records), path.name
            continue
        trace = _load(path)
        resource_attrs = {
            a["key"]: a["value"] for a in trace["resourceSpans"][0]["resource"]["attributes"]
        }
        assert resource_attrs["tokenaudit.synthetic"] == {"boolValue": True}, path.name


def test_regeneration_is_deterministic(tmp_path: Path) -> None:
    """Running the generator reproduces the committed corpus byte-for-byte."""
    subprocess.run(
        [sys.executable, str(GENERATOR), "--out", str(tmp_path)],
        check=True,
        capture_output=True,
    )
    committed = sorted(p.name for p in FIXTURES.iterdir() if p.is_file())
    regenerated = sorted(p.name for p in tmp_path.iterdir() if p.is_file())
    assert committed == regenerated
    for name in committed:
        assert (tmp_path / name).read_bytes() == (FIXTURES / name).read_bytes(), name
