"""Trace ingestion: OTel GenAI, OpenInference, and raw provider logs."""

from __future__ import annotations

from pathlib import Path

from tokenaudit.ingest.containers import UnknownTraceFormatError, load_text
from tokenaudit.ingest.dedup import dedup_layered
from tokenaudit.ingest.dialects import extract
from tokenaudit.ingest.provider_logs import load_provider_log, looks_like_provider_log
from tokenaudit.ingest.quality import assess_request, assess_session
from tokenaudit.ingest.sessionize import sessionize
from tokenaudit.model.records import IngestResult

__all__ = ["UnknownTraceFormatError", "ingest_path", "ingest_text"]


def ingest_text(text: str, source: str) -> IngestResult:
    """Ingest trace content already in memory (an upload, a stream).

    ``source`` is a label for results and error messages, not a path —
    nothing is read from or written to disk here, which is what lets the
    hosted demo honestly say traces are processed in memory and never
    stored.
    """
    if looks_like_provider_log(text):
        container = "provider-log"
        kept = load_provider_log(text, source)
        dropped: list[str] = []
        skipped, spans_total = 0, len(kept)
    else:
        container, raw_spans = load_text(text, source)
        requests = []
        skipped = 0
        for raw in raw_spans:
            request = extract(raw, source)
            if request is None:
                skipped += 1
            else:
                requests.append(request)
        kept, dropped = dedup_layered(requests)
        spans_total = len(raw_spans)
    assessed = [assess_request(r) for r in kept]
    sessions = tuple(assess_session(s) for s in sessionize(assessed, source))
    return IngestResult(
        source=source,
        container=container,
        sessions=sessions,
        spans_total=spans_total,
        spans_skipped=skipped,
        deduped_span_ids=tuple(dropped),
    )


def ingest_path(path: Path) -> IngestResult:
    """Read one trace file into normalized, deduplicated, quality-checked sessions."""
    return ingest_text(path.read_text(encoding="utf-8"), str(path))
