"""Scrub captured trace files before they are committed.

Captured traces contain prompts and tool outputs from a public project run on
public market data — fine to publish — but they pass through a machine that
holds real credentials, so this pass removes anything secret-shaped or
machine-identifying before anything reaches git:

- API-key-shaped strings (``sk-...`` and Bearer tokens) -> ``sk-REDACTED``
- the capturing user's home directory prefix -> ``~``

The pass is recursive over JSON structure, idempotent, and rewrites files only
when something changed. It exits non-zero if any key-shaped string survives,
so the capture script fails loudly rather than committing a leak.

Usage: ``python redact.py --paths corpus/captured/runs [--home /Users/name]``
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

KEY_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{16,}"),
]
LEFTOVER = re.compile(r"sk-[A-Za-z0-9_-]{16,}")


def scrub_text(text: str, home: str) -> str:
    for pattern in KEY_PATTERNS:
        text = pattern.sub("sk-REDACTED", text)
    if home and home != "/":
        text = text.replace(home, "~")
    return text


def scrub_value(value: Any, home: str) -> Any:
    if isinstance(value, str):
        return scrub_text(value, home)
    if isinstance(value, list):
        return [scrub_value(v, home) for v in value]
    if isinstance(value, dict):
        return {k: scrub_value(v, home) for k, v in value.items()}
    return value


def scrub_file(path: Path, home: str) -> int:
    """Scrub one file in place; returns 1 if the file was modified."""
    original = path.read_text(encoding="utf-8")
    if path.suffix == ".jsonl":
        lines = []
        for line in original.splitlines():
            if line.strip():
                lines.append(json.dumps(scrub_value(json.loads(line), home), separators=(",", ":")))
        cleaned = "\n".join(lines) + "\n" if lines else ""
    elif path.suffix == ".json":
        cleaned = (
            json.dumps(scrub_value(json.loads(original), home), indent=2, sort_keys=True) + "\n"
        )
    else:
        cleaned = scrub_text(original, home)
    if cleaned != original:
        path.write_text(cleaned, encoding="utf-8")
        return 1
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    parser.add_argument("--paths", nargs="+", type=Path, required=True)
    parser.add_argument("--home", default=str(Path.home()))
    args = parser.parse_args()

    files: list[Path] = []
    for p in args.paths:
        if p.is_dir():
            files.extend(sorted(q for q in p.rglob("*") if q.is_file()))
        elif p.is_file():
            files.append(p)

    changed = sum(scrub_file(f, args.home) for f in files)

    leaks = [
        f
        for f in files
        if LEFTOVER.search(f.read_text(encoding="utf-8").replace("sk-REDACTED", ""))
    ]
    if leaks:
        print(f"ERROR: key-shaped strings survived redaction in: {[str(f) for f in leaks]}")
        sys.exit(2)
    print(f"redact: {len(files)} files checked, {changed} modified, 0 leaks")


if __name__ == "__main__":
    main()
