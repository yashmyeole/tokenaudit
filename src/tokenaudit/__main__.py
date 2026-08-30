"""Command-line entry point: ``python -m tokenaudit ingest <paths>``.

Prints the normalized ingest result as JSON (default) or a human summary
(``--summary``). A dedicated console script with a fuller CLI arrives with
the report phase; this stays argparse-only so the package keeps zero
runtime dependencies.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from tokenaudit.detect import SessionDiagnosis, diagnose
from tokenaudit.ingest import UnknownTraceFormatError, ingest_path
from tokenaudit.model.records import IngestResult
from tokenaudit.rulebook import load_rulebook


def _trace_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    if path.is_dir():
        found = sorted(p for p in path.rglob("*") if p.name.endswith((".trace.json", ".jsonl")))
        if not found:
            raise UnknownTraceFormatError(f"{path}: no trace files found")
        return found
    raise UnknownTraceFormatError(f"{path}: no such file or directory")


def _summarize(result: IngestResult) -> str:
    lines = [
        f"{result.source}  [{result.container}]  "
        f"spans={result.spans_total} skipped={result.spans_skipped} "
        f"deduped={len(result.deduped_span_ids)}"
    ]
    for session in result.sessions:
        requests = session.requests
        known_input = [r.usage.input_total for r in requests if r.usage.input_total is not None]
        known_read = [r.usage.cache_read for r in requests if r.usage.cache_read is not None]
        total_input = sum(known_input)
        total_read = sum(known_read)
        hit = f"{total_read / total_input * 100:.1f}%" if total_input and known_read else "n/a"
        flags = Counter(flag for r in requests for flag in r.quality)
        unpriceable = sum(1 for r in requests if r.unpriceable)
        lines.append(
            f"  session {session.session_id} ({session.grouping}): "
            f"{len(requests)} requests, input={total_input}, "
            f"cache_read={total_read} ({hit}), unpriceable={unpriceable}"
        )
        if flags:
            rendered = ", ".join(f"{name} x{count}" for name, count in sorted(flags.items()))
            lines.append(f"    flags: {rendered}")
    return "\n".join(lines)


def _render_diagnosis(diagnosis: SessionDiagnosis) -> str:
    lines = [
        f"session {diagnosis.session_id}  "
        f"({diagnosis.requests_examined} requests examined, "
        f"{diagnosis.requests_skipped_unpriceable} unpriceable skipped)"
    ]
    if not diagnosis.findings:
        lines.append("  no findings")
    for finding in diagnosis.findings:
        lines.append(
            f"  [{finding.severity.value.upper()}] {finding.detector_id}: {finding.summary}"
        )
        if finding.impact is not None:
            impact = finding.impact
            tokens = (
                f"~{impact.tokens_low:,} tokens"
                if impact.tokens_low == impact.tokens_high
                else f"~{impact.tokens_low:,}-{impact.tokens_high:,} tokens"
            )
            lines.append(f"      waste: {tokens} full-price equivalent ({impact.assumption})")
        lines.append(f"      fix: {finding.fix.summary}")
    clean = [d for d in diagnosis.checked if d not in {f.detector_id for f in diagnosis.findings}]
    if clean:
        lines.append(f"  checked, clean: {', '.join(clean)}")
    for detector_id, reason in diagnosis.abstained.items():
        lines.append(f"  not checked: {detector_id} ({reason})")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tokenaudit")
    subparsers = parser.add_subparsers(dest="command", required=True)
    ingest = subparsers.add_parser("ingest", help="normalize trace files")
    ingest.add_argument("paths", nargs="+", type=Path)
    ingest.add_argument("--summary", action="store_true", help="human summary instead of JSON")
    diag = subparsers.add_parser("diagnose", help="diagnose token waste in trace files")
    diag.add_argument("paths", nargs="+", type=Path)
    diag.add_argument("--json", action="store_true", help="machine-readable output")
    subparsers.add_parser("rulebook", help="render the provider caching rulebook as markdown")
    args = parser.parse_args(argv)

    if args.command == "rulebook":
        print(load_rulebook().render_markdown())
        return 0

    results = []
    try:
        for path in args.paths:
            for trace_file in _trace_files(path):
                results.append(ingest_path(trace_file))
    except UnknownTraceFormatError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.command == "diagnose":
        per_result = [(result, diagnose(result)) for result in results]
        if args.json:
            payload = [asdict(d) for _, diagnoses in per_result for d in diagnoses]
            print(json.dumps(payload, indent=2, default=str))
        else:
            for result, diagnoses in per_result:
                print(result.source)
                for diagnosis in diagnoses:
                    print(_render_diagnosis(diagnosis))
        return 0

    if args.summary:
        for result in results:
            print(_summarize(result))
    else:
        print(json.dumps([asdict(result) for result in results], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
