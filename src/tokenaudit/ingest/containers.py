"""Container parsers: file bytes -> raw spans, or a loud refusal.

Two on-disk containers are supported, both present in this repo's corpus:

- **OTLP/JSON**: the protobuf JSON mapping (``resourceSpans`` ->
  ``scopeSpans`` -> ``spans``), with int64 values encoded as decimal strings
  and attributes as ``{key, value: {intValue|stringValue|boolValue|...}}``.
- **OTel SDK JSONL**: one ``ReadableSpan.to_json()`` object per line — flat
  ``attributes`` dict, ``0x``-prefixed hex ids, ISO-8601 timestamps. This is
  what the Python SDK itself renders and what the captured runs use.

Anything else raises :class:`UnknownTraceFormatError`. An unrecognized file
must fail loudly: silently producing zero requests from a misread file is
indistinguishable from a healthy empty trace, and those lead to opposite
conclusions.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

AttrValue = str | int | float | bool


class UnknownTraceFormatError(ValueError):
    """The file matches no supported trace container."""


@dataclass(frozen=True)
class RawSpan:
    """A container-agnostic span, before dialect interpretation."""

    trace_id: str
    span_id: str
    parent_id: str | None
    name: str
    start_unix_nano: int
    end_unix_nano: int
    attributes: dict[str, AttrValue]
    resource: dict[str, AttrValue]


def load_file(path: Path) -> tuple[str, list[RawSpan]]:
    """Parse a trace file; returns ``(container_id, spans)`` or raises."""
    text = path.read_text(encoding="utf-8")
    stripped = text.lstrip()
    if not stripped:
        raise UnknownTraceFormatError(f"{path}: empty file")
    if stripped.startswith("{") and '"resourceSpans"' in stripped[:2000]:
        return "otlp-json", _load_otlp(text, path)
    if stripped.startswith("{"):
        return "otel-sdk-jsonl", _load_sdk_jsonl(text, path)
    raise UnknownTraceFormatError(f"{path}: not JSON trace data")


# --------------------------------------------------------------------------- #
# OTLP/JSON
# --------------------------------------------------------------------------- #


def _otlp_value(value: dict[str, Any]) -> AttrValue:
    if "intValue" in value:
        return int(value["intValue"])  # int64 is a decimal string in proto JSON
    if "boolValue" in value:
        return bool(value["boolValue"])
    if "doubleValue" in value:
        return float(value["doubleValue"])
    if "stringValue" in value:
        return str(value["stringValue"])
    raise UnknownTraceFormatError(f"unsupported OTLP attribute value: {value!r}")


def _otlp_attrs(items: list[dict[str, Any]]) -> dict[str, AttrValue]:
    return {a["key"]: _otlp_value(a["value"]) for a in items}


def _load_otlp(text: str, path: Path) -> list[RawSpan]:
    try:
        documents = [json.loads(text)]
    except json.JSONDecodeError:
        try:  # some exporters write one OTLP export request per line
            documents = [json.loads(line) for line in text.splitlines() if line.strip()]
        except json.JSONDecodeError as exc:
            raise UnknownTraceFormatError(f"{path}: invalid JSON ({exc})") from exc

    spans: list[RawSpan] = []
    for doc in documents:
        for rs in doc.get("resourceSpans", []):
            resource = _otlp_attrs(rs.get("resource", {}).get("attributes", []))
            for ss in rs.get("scopeSpans", []):
                for span in ss.get("spans", []):
                    parent = str(span.get("parentSpanId", "") or "") or None
                    spans.append(
                        RawSpan(
                            trace_id=str(span["traceId"]).lower(),
                            span_id=str(span["spanId"]).lower(),
                            parent_id=parent.lower() if parent else None,
                            name=str(span.get("name", "")),
                            start_unix_nano=int(span["startTimeUnixNano"]),
                            end_unix_nano=int(span["endTimeUnixNano"]),
                            attributes=_otlp_attrs(span.get("attributes", [])),
                            resource=resource,
                        )
                    )
    if not spans:
        raise UnknownTraceFormatError(f"{path}: OTLP document contains no spans")
    return spans


# --------------------------------------------------------------------------- #
# OTel SDK JSONL
# --------------------------------------------------------------------------- #


def _hex_id(value: str | None) -> str | None:
    if not value:
        return None
    return value.removeprefix("0x").lower()


def _iso_to_nano(value: str) -> int:
    return round(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1e9)


def _clean_attrs(attrs: dict[str, Any]) -> dict[str, AttrValue]:
    out: dict[str, AttrValue] = {}
    for key, value in attrs.items():
        if isinstance(value, str | int | float | bool):
            out[key] = value
        else:  # tuples/lists render as lists in to_json; keep a stable string
            out[key] = json.dumps(value, separators=(",", ":"))
    return out


def _load_sdk_jsonl(text: str, path: Path) -> list[RawSpan]:
    spans: list[RawSpan] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise UnknownTraceFormatError(f"{path}:{lineno}: invalid JSON line ({exc})") from exc
        context = obj.get("context")
        if not isinstance(context, dict) or "attributes" not in obj:
            raise UnknownTraceFormatError(
                f"{path}:{lineno}: not an OTel SDK span object (missing context/attributes)"
            )
        trace_id = _hex_id(context.get("trace_id"))
        span_id = _hex_id(context.get("span_id"))
        if trace_id is None or span_id is None:
            raise UnknownTraceFormatError(f"{path}:{lineno}: span without trace/span id")
        resource = obj.get("resource", {}).get("attributes", {})
        spans.append(
            RawSpan(
                trace_id=trace_id,
                span_id=span_id,
                parent_id=_hex_id(obj.get("parent_id")),
                name=str(obj.get("name", "")),
                start_unix_nano=_iso_to_nano(obj["start_time"]),
                end_unix_nano=_iso_to_nano(obj["end_time"]),
                attributes=_clean_attrs(obj.get("attributes") or {}),
                resource=_clean_attrs(resource if isinstance(resource, dict) else {}),
            )
        )
    if not spans:
        raise UnknownTraceFormatError(f"{path}: no spans found")
    return spans
