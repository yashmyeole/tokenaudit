#!/usr/bin/env bash
# Capture real TradingAgents runs with OpenTelemetry — zero code modification.
#
# Runs on the machine that holds the API key. Everything heavy happens in a
# throwaway virtualenv under corpus/captured/.work (gitignored); only redacted
# traces, manifests and decisions land in corpus/captured/runs/.
#
# Usage:
#   export OPENAI_API_KEY=...        # never committed, never leaves this shell
#   bash corpus/captured/capture_tradingagents.sh              # 3 default runs
#   bash corpus/captured/capture_tradingagents.sh NVDA:2026-08-14 AAPL:2026-08-21
#
# Optional env:
#   TOKENAUDIT_DEEP_LLM / TOKENAUDIT_QUICK_LLM  (default gpt-5.4-mini, cheap)
#   TA_REF     git ref of TradingAgents to check out (default: default branch)
#   FRESH=1    rebuild the .work venv and clone from scratch

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$HERE/.work"
RUNS="$HERE/runs/tradingagents"
CLONE="$WORK/TradingAgents"

: "${OPENAI_API_KEY:?Set OPENAI_API_KEY in your environment first}"
DEEP="${TOKENAUDIT_DEEP_LLM:-gpt-5.4-mini}"
QUICK="${TOKENAUDIT_QUICK_LLM:-gpt-5.4-mini}"

PAIRS=("$@")
if [ ${#PAIRS[@]} -eq 0 ]; then
  PAIRS=("NVDA:2026-08-14" "AAPL:2026-08-14" "NVDA:2026-08-21")
fi

# --- pick a python (TradingAgents targets 3.12) ------------------------------
PY=""
for candidate in python3.12 python3.13 python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then PY="$candidate"; break; fi
done
[ -n "$PY" ] || { echo "no python3 found; brew install python@3.12"; exit 1; }
echo "using $PY ($($PY --version 2>&1)); TradingAgents targets 3.12"

# --- workspace ---------------------------------------------------------------
if [ "${FRESH:-0}" = "1" ]; then rm -rf "$WORK"; fi
mkdir -p "$WORK" "$RUNS"

if [ ! -d "$CLONE/.git" ]; then
  git clone --depth 50 https://github.com/TauricResearch/TradingAgents.git "$CLONE"
fi
if [ -n "${TA_REF:-}" ]; then git -C "$CLONE" checkout --quiet "$TA_REF"; fi
TA_COMMIT="$(git -C "$CLONE" rev-parse HEAD)"
echo "TradingAgents at $TA_COMMIT"

if [ ! -x "$WORK/venv/bin/python" ]; then "$PY" -m venv "$WORK/venv"; fi
VPY="$WORK/venv/bin/python"
"$VPY" -m pip install --quiet --upgrade pip
echo "installing TradingAgents + instrumentation into the capture venv..."
"$VPY" -m pip install --quiet -e "$CLONE"
"$VPY" -m pip install --quiet \
  opentelemetry-sdk \
  openinference-instrumentation-openai \
  openinference-instrumentation-langchain

# --- plumbing check before spending money ------------------------------------
"$VPY" "$HERE/runner.py" --smoke --out "$WORK/smoke" >/dev/null
echo "exporter plumbing OK (smoke span written)"

# --- capture -----------------------------------------------------------------
i=0
for pair in "${PAIRS[@]}"; do
  i=$((i + 1))
  ticker="${pair%%:*}"
  date="${pair##*:}"
  out="$RUNS/run-$(printf '%02d' "$i")-$ticker-$date"
  echo "--- run $i/${#PAIRS[@]}: $ticker on $date -> $out"
  TOKENAUDIT_RUN="$out" "$VPY" "$HERE/runner.py" \
    --ticker "$ticker" --date "$date" --out "$out" \
    --deep "$DEEP" --quick "$QUICK" --rounds 1 --ta-commit "$TA_COMMIT"
done

# --- redact before anything can be committed ---------------------------------
"$VPY" "$HERE/redact.py" --paths "$RUNS"

echo
echo "done. captured runs:"
ls -la "$RUNS"
echo
echo "next: review a trace, then commit:"
echo "  git add corpus/captured/runs && git commit -m 'corpus: captured TradingAgents runs (redacted)'"
