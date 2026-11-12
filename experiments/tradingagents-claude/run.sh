#!/usr/bin/env bash
# TradingAgents on Claude, before and after turning on prompt caching.
# See README.md for the design and what to expect.
#
# 6 runs, alternating arms, COOLDOWN seconds apart so one run's cache
# entries (5-minute lifetime) are gone before the next starts:
#
#   1 baseline NVDA:2026-08-14   2 patched NVDA:2026-08-14
#   3 baseline AAPL:2026-08-14   4 patched AAPL:2026-08-14
#   5 baseline NVDA:2026-08-21   6 patched NVDA:2026-08-21
#
#   baseline = TradingAgents at BASE_COMMIT, unchanged
#   patched  = same commit + automatic_caching.patch
#
# Usage:
#   export ANTHROPIC_API_KEY=...   # never committed, never leaves this shell
#   bash experiments/tradingagents-claude/run.sh
#
# Optional env: MODEL (default claude-sonnet-5), COOLDOWN (default 600)

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
CAPTURED="$REPO/corpus/captured"
WORK="$CAPTURED/.work"
CLONE="$WORK/TradingAgents"
RUNS="$HERE/runs"
BASE_COMMIT="9fa1196fffbcfd9de5146d2dd2869df84ab99838"
MODEL="${MODEL:-claude-sonnet-5}"
COOLDOWN="${COOLDOWN:-600}"

: "${ANTHROPIC_API_KEY:?Set ANTHROPIC_API_KEY in your environment first}"

mkdir -p "$WORK"
[ -d "$CLONE/.git" ] || git clone https://github.com/TauricResearch/TradingAgents.git "$CLONE"
if [ ! -x "$WORK/venv/bin/python" ]; then
  "$(command -v python3.12 || command -v python3)" -m venv "$WORK/venv"
fi
VPY="$WORK/venv/bin/python"

if ! git -C "$CLONE" cat-file -e "$BASE_COMMIT^{commit}" 2>/dev/null; then
  git -C "$CLONE" fetch --quiet origin "$BASE_COMMIT" \
    || git -C "$CLONE" fetch --quiet --unshallow origin
fi
git -C "$CLONE" checkout --quiet "$BASE_COMMIT"
git -C "$CLONE" branch -f claude-caching "$BASE_COMMIT"
git -C "$CLONE" checkout --quiet claude-caching
git -C "$CLONE" apply --check "$HERE/automatic_caching.patch"
git -C "$CLONE" apply "$HERE/automatic_caching.patch"
git -C "$CLONE" -c user.name=experiment -c user.email=experiment@local -c commit.gpgsign=false \
  commit --quiet -am "experiment: automatic prompt caching"
git -C "$CLONE" checkout --quiet "$BASE_COMMIT"

"$VPY" -m pip install --quiet --upgrade pip
"$VPY" -m pip install --quiet -e "$CLONE"
"$VPY" -m pip install --quiet opentelemetry-sdk \
  openinference-instrumentation-anthropic openinference-instrumentation-langchain

# Checks before spending money: exporter plumbing, then DNS.
"$VPY" "$CAPTURED/runner.py" --smoke --out "$WORK/smoke-claude" >/dev/null
echo "exporter plumbing OK"
"$VPY" -c "import socket; socket.getaddrinfo('api.anthropic.com', 443)" \
  || { echo "cannot resolve api.anthropic.com; is the machine online?"; exit 1; }

mkdir -p "$RUNS"

run_one() {  # n arm ticker date
  local n="$1" arm="$2" ticker="$3" date="$4"
  local out="$RUNS/$(printf '%02d' "$n")-$arm-$ticker-$date"
  if [ "$arm" = "patched" ]; then git -C "$CLONE" checkout --quiet claude-caching
  else git -C "$CLONE" checkout --quiet "$BASE_COMMIT"; fi
  echo "--- run $n/6: $arm $ticker $date -> $out"
  "$VPY" "$CAPTURED/runner.py" --provider anthropic --deep "$MODEL" --quick "$MODEL" \
    --ticker "$ticker" --date "$date" --out "$out" --rounds 1 \
    --ta-commit "$(git -C "$CLONE" rev-parse HEAD)"
}

cool() { echo "cooldown ${COOLDOWN}s..."; sleep "$COOLDOWN"; }

run_one 1 baseline NVDA 2026-08-14 ; cool
run_one 2 patched  NVDA 2026-08-14 ; cool
run_one 3 baseline AAPL 2026-08-14 ; cool
run_one 4 patched  AAPL 2026-08-14 ; cool
run_one 5 baseline NVDA 2026-08-21 ; cool
run_one 6 patched  NVDA 2026-08-21

git -C "$CLONE" checkout --quiet "$BASE_COMMIT"
"$VPY" "$CAPTURED/redact.py" --paths "$RUNS"

echo
echo "done. analyze with:"
echo "  tokenaudit diagnose $RUNS/*-baseline-*/trace.jsonl"
echo "  tokenaudit check $RUNS/*-baseline-*/trace.jsonl --baseline /tmp/claude-baseline.json --update"
echo "  tokenaudit check $RUNS/*-patched-*/trace.jsonl --baseline /tmp/claude-baseline.json"
