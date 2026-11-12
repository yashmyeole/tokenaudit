"""The published evaluation results must match what the code actually does."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVAL = ROOT / "eval"


def test_published_results_match_a_fresh_run(tmp_path: Path) -> None:
    subprocess.run(
        [
            sys.executable,
            str(EVAL / "run_eval.py"),
            "--out",
            str(tmp_path),
            "--record-out",
            str(tmp_path),
        ],
        check=True,
        capture_output=True,
    )
    for name in ("results.md", "results.json"):
        assert (tmp_path / name).read_bytes() == (EVAL / name).read_bytes(), (
            f"{name} drifted; regenerate with: python eval/run_eval.py"
        )
    packaged = ROOT / "src" / "tokenaudit" / "detect" / "corpus_record.json"
    assert (tmp_path / "corpus_record.json").read_bytes() == packaged.read_bytes(), (
        "packaged corpus record drifted; regenerate with: python eval/run_eval.py"
    )


def test_results_show_no_misses_and_no_false_alarms() -> None:
    import json

    tallies = json.loads((EVAL / "results.json").read_text(encoding="utf-8"))
    assert len(tallies) == 11
    for detector_id, tally in tallies.items():
        assert tally["positives_missed"] == 0, detector_id
        assert tally["false_alarms"] == 0, detector_id
