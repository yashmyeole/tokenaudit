#!/usr/bin/env bash
# Re-run ONLY the two control runs (03 and 08). The first attempt recorded
# nothing: the usage tee patched httpx's sync client, but langchain-openai
# drives the async client, so zero requests were observed. exp_runner.py now
# patches both and warns loudly if the tee stays empty. The six traced runs
# are untouched and remain valid.
#
# Usage:
#   export OPENAI_API_KEY=...
#   bash experiments/tradingagents-cache-fix/run_controls.sh
#
# ~35 minutes: run, 15-min cooldown, run. Same cache-isolation rules.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
CAPTURED="$REPO/corpus/captured"
WORK="$CAPTURED/.work"
CLONE="$WORK/TradingAgents"
RUNS="$HERE/runs"
BASE_COMMIT="a33fd4c0f134485a43553a2c23a63cb14adbd88f"
COOLDOWN="${COOLDOWN:-900}"

: "${OPENAI_API_KEY:?Set OPENAI_API_KEY in your environment first}"
VPY="$WORK/venv/bin/python"
[ -x "$VPY" ] || { echo "capture venv missing; run run_experiment.sh first"; exit 1; }

git -C "$CLONE" checkout --quiet "$BASE_COMMIT"

run_control() {  # n
  local out="$RUNS/$(printf '%02d' "$1")-baseline-control-NVDA-2026-08-14"
  rm -rf "$out"
  echo "--- control run $1 -> $out"
  "$VPY" "$HERE/exp_runner.py" --arm baseline --ticker NVDA --date 2026-08-14 \
    --out "$out" --rounds 1 --ta-commit "$BASE_COMMIT" --control
}

echo "cooldown ${COOLDOWN}s first (isolate from anything recent)..."
sleep "$COOLDOWN"
run_control 3
echo "cooldown ${COOLDOWN}s (cache isolation)..."
sleep "$COOLDOWN"
run_control 8

"$VPY" "$CAPTURED/redact.py" --paths "$RUNS"

echo
echo "done. regenerate results with:"
echo "  python experiments/tradingagents-cache-fix/analyze.py"
