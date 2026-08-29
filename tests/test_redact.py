"""The redaction pass must scrub key-shaped strings and home paths."""

import json
import subprocess
import sys
from pathlib import Path

REDACT = Path(__file__).resolve().parent.parent / "corpus" / "captured" / "redact.py"


def test_redact_scrubs_keys_and_home(tmp_path: Path) -> None:
    trace = tmp_path / "trace.jsonl"
    record = {
        "auth": "sk-abcdef1234567890ABCDEF1234",
        "header": "Bearer abcdefghijklmnop1234",
        "path": "/Users/tester/project/file.py",
        "count": 7,
    }
    trace.write_text(json.dumps(record) + "\n", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(REDACT), "--paths", str(tmp_path), "--home", "/Users/tester"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "0 leaks" in result.stdout

    cleaned = json.loads(trace.read_text(encoding="utf-8"))
    assert cleaned["auth"] == "sk-REDACTED"
    assert cleaned["header"] == "sk-REDACTED"
    assert cleaned["path"] == "~/project/file.py"
    assert cleaned["count"] == 7


def test_redact_is_idempotent(tmp_path: Path) -> None:
    trace = tmp_path / "trace.jsonl"
    trace.write_text(json.dumps({"auth": "sk-abcdef1234567890ABCDEF1234"}) + "\n", encoding="utf-8")
    args = [sys.executable, str(REDACT), "--paths", str(tmp_path), "--home", "/Users/tester"]
    subprocess.run(args, capture_output=True, check=True)
    first = trace.read_bytes()
    subprocess.run(args, capture_output=True, check=True)
    assert trace.read_bytes() == first
