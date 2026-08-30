"""Trace ingestion: OTel GenAI, OpenInference, and raw provider SDK logs."""

from __future__ import annotations

from pathlib import Path

from tokenaudit.ingest.containers import UnknownTraceFormatError, load_file
from tokenaudit.ingest.dedup import dedup_layered
from tokenaudit.ingest.dialects import extract
from tokenaudit.ingest.quality import assess_request, assess_session
from tokenaudit.ingest.sessionize import sessionize
from tokenaudit.model.records import IngestResult

__all__ = ["UnknownTraceFormatError", "ingest_path"]


def ingest_path(path: Path) -> IngestResult:
    """Read one trace file into normalized, deduplicated, quality-checked sessions."""
    container, raw_spans = load_file(path)
    source = str(path)
    requests = []
    skipped = 0
    for raw in raw_spans:
        request = extract(raw, source)
        if request is None:
            skipped += 1
        else:
            requests.append(request)
    kept, dropped = dedup_layered(requests)
    assessed = [assess_request(r) for r in kept]
    sessions = tuple(assess_session(s) for s in sessionize(assessed, source))
    return IngestResult(
        source=source,
        container=container,
        sessions=sessions,
        spans_total=len(raw_spans),
        spans_skipped=skipped,
        deduped_span_ids=tuple(dropped),
    )
