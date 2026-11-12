"""The ``tokenaudit`` command-line interface (also ``python -m tokenaudit``).

Subcommands: ``ingest`` (normalize traces to JSON or a summary),
``diagnose`` (findings as text, ``--json`` or an ``--html`` report),
``rulebook`` (render the provider caching rules), and ``check`` (the CI
gate: compare a run against a committed baseline and fail on regression).
Stays argparse-only so the package keeps zero runtime dependencies
(DECISIONS 006).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from dataclasses import asdict
from importlib.resources import files
from pathlib import Path
from typing import Any

from tokenaudit import __version__
from tokenaudit.ci import (
    BaselineError,
    compare,
    read_baseline,
    render_markdown,
    render_text,
    snapshot,
    write_baseline,
)
from tokenaudit.detect import SessionDiagnosis, diagnose, diagnose_across
from tokenaudit.ingest import UnknownTraceFormatError, ingest_path
from tokenaudit.model.records import IngestResult
from tokenaudit.report import render_html
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


def _without_segments(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _without_segments(v) for k, v in value.items() if k != "segments"}
    if isinstance(value, list | tuple):
        return [_without_segments(v) for v in value]
    return value


def _across(results: list[IngestResult]) -> SessionDiagnosis | None:
    if sum(len(r.sessions) for r in results) < 2:
        return None
    return diagnose_across(results)


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
    parser.add_argument("--version", action="version", version=f"tokenaudit {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)
    ingest = subparsers.add_parser("ingest", help="normalize trace files")
    ingest.add_argument("paths", nargs="+", type=Path)
    ingest.add_argument("--summary", action="store_true", help="human summary instead of JSON")
    diag = subparsers.add_parser("diagnose", help="diagnose token waste in trace files")
    diag.add_argument("paths", nargs="*", type=Path)
    diag.add_argument("--json", action="store_true", help="machine-readable output")
    diag.add_argument("--html", type=Path, help="also write a self-contained HTML report")
    diag.add_argument(
        "--sample", action="store_true", help="diagnose the bundled sample trace (a demo)"
    )
    subparsers.add_parser("rulebook", help="render the provider caching rulebook as markdown")
    check = subparsers.add_parser(
        "check", help="CI gate: fail when caching regressed against a committed baseline"
    )
    check.add_argument("paths", nargs="+", type=Path)
    check.add_argument(
        "--baseline", type=Path, required=True, help="baseline JSON (commit it to your repo)"
    )
    check.add_argument(
        "--update", action="store_true", help="write the baseline from this run and exit"
    )
    check.add_argument(
        "--max-regression",
        type=float,
        default=5.0,
        metavar="POINTS",
        help="allowed drop in cache read rate / health floor, in percentage points (default 5.0)",
    )
    check.add_argument("--strict", action="store_true", help="also fail on new non-HIGH findings")
    check.add_argument(
        "--markdown", type=Path, help="also write a PR-comment-sized markdown summary"
    )
    args = parser.parse_args(argv)

    if args.command == "rulebook":
        print(load_rulebook().render_markdown())
        return 0

    paths = list(args.paths)
    if args.command == "diagnose" and args.sample:
        paths.append(Path(str(files("tokenaudit.examples").joinpath("sample.trace.json"))))
    if not paths:
        parser.error("no trace paths given (or use: tokenaudit diagnose --sample)")
    results = []
    try:
        for path in paths:
            for trace_file in _trace_files(path):
                results.append(ingest_path(trace_file))
    except UnknownTraceFormatError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.command == "check":
        rulebook = load_rulebook()
        current = snapshot(
            [(result, diagnose(result)) for result in results], rulebook, _across(results)
        )
        if current.input_tokens == 0:
            print("error: no input tokens in these traces; nothing to compare", file=sys.stderr)
            return 2
        if args.update:
            write_baseline(current, args.baseline)
            print(f"wrote baseline {args.baseline} — commit it to your repo")
            return 0
        try:
            baseline = read_baseline(args.baseline)
        except BaselineError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        report = compare(baseline, current, args.max_regression, strict=args.strict)
        print(render_text(report, current))
        markdown = render_markdown(report, current)
        if args.markdown:
            args.markdown.write_text(markdown, encoding="utf-8")
        summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary_path:
            with open(summary_path, "a", encoding="utf-8") as handle:
                handle.write(markdown)
        return 0 if report.passed else 1

    if args.command == "diagnose":
        per_result = [(result, diagnose(result)) for result in results]
        across = _across(results)
        if args.html:
            html = render_html(per_result, load_rulebook(), across)
            args.html.write_text(html, encoding="utf-8")
            print(f"wrote {args.html}", file=sys.stderr)
        if args.json:
            payload = [asdict(d) for _, diagnoses in per_result for d in diagnoses]
            if across is not None:
                payload.append(asdict(across))
            print(json.dumps(payload, indent=2, default=str))
        else:
            for result, diagnoses in per_result:
                print(result.source)
                for diagnosis in diagnoses:
                    print(_render_diagnosis(diagnosis))
            if across is not None:
                print("across sessions")
                print(_render_diagnosis(across))
        return 0

    if args.summary:
        for result in results:
            print(_summarize(result))
    else:
        print(json.dumps([_without_segments(asdict(r)) for r in results], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
