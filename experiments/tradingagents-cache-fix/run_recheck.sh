#!/usr/bin/env bash
# Re-check the debate-prompt reorder on current TradingAgents (v0.7.1+).
#
# One baseline/patched pair by default, 15 minutes apart so gpt-5.4-mini's
# implicit cache can't carry one run into the next. PAIRS=2 adds a second
# pair in the opposite order. Each run gets its own empty decision log, since
# upstream now feeds past decisions for the same ticker into later runs.
#
#   export OPENAI_API_KEY=...
#   bash experiments/tradingagents-cache-fix/run_recheck.sh
#   python experiments/tradingagents-cache-fix/analyze.py \
#     --runs experiments/tradingagents-cache-fix/runs-v0.7 \
#     --out experiments/tradingagents-cache-fix/results-v0.7.md
#
# Env: TICKER (NVDA), DATE (2026-08-14), PAIRS (1), COOLDOWN (900 seconds)

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
CAPTURED="$REPO/corpus/captured"
WORK="$CAPTURED/.work"
CLONE="$WORK/TradingAgents"
RUNS="$HERE/runs-v0.7"
BASE_COMMIT="9fa1196fffbcfd9de5146d2dd2869df84ab99838"
PATCH="$HERE/cache_friendly_prompts_v0.7.patch"
TICKER="${TICKER:-NVDA}"
DATE="${DATE:-2026-08-14}"
PAIRS="${PAIRS:-1}"
COOLDOWN="${COOLDOWN:-900}"

: "${OPENAI_API_KEY:?Set OPENAI_API_KEY in your environment first}"

mkdir -p "$WORK"
[ -d "$CLONE/.git" ] || git clone https://github.com/TauricResearch/TradingAgents.git "$CLONE"

# TradingAgents now needs Python 3.11+.
VPY="$WORK/venv/bin/python"
if ! { [ -x "$VPY" ] && "$VPY" -c 'import sys; sys.exit(sys.version_info < (3, 11))'; }; then
  PY="$(command -v python3.12 || command -v python3.11 || true)"
  [ -n "$PY" ] || { echo "need python3.11 or newer"; exit 1; }
  rm -rf "$WORK/venv"
  "$PY" -m venv "$WORK/venv"
fi

if ! git -C "$CLONE" cat-file -e "$BASE_COMMIT^{commit}" 2>/dev/null; then
  git -C "$CLONE" fetch --quiet origin main
fi
git -C "$CLONE" checkout --quiet "$BASE_COMMIT"
git -C "$CLONE" branch -f cache-fix-v0.7 "$BASE_COMMIT"
git -C "$CLONE" checkout --quiet cache-fix-v0.7
git -C "$CLONE" apply --check "$PATCH"
git -C "$CLONE" apply "$PATCH"
git -C "$CLONE" -c user.name=experiment -c user.email=experiment@local \
  commit --quiet -am "experiment: debate prompt reorder"
git -C "$CLONE" checkout --quiet "$BASE_COMMIT"

"$VPY" -m pip install --quiet --upgrade pip
"$VPY" -m pip install --quiet -e "$CLONE"
"$VPY" -m pip install --quiet opentelemetry-sdk \
  openinference-instrumentation-openai openinference-instrumentation-langchain

"$VPY" "$CAPTURED/runner.py" --smoke --out "$WORK/smoke-exp" >/dev/null
echo "exporter plumbing OK"
"$VPY" -c 'import socket; socket.getaddrinfo("api.openai.com", 443)' \
  || { echo "cannot resolve api.openai.com; is the machine online?"; exit 1; }

mkdir -p "$RUNS" "$WORK/memory"

run_one() {  # n arm
  local n="$1" arm="$2"
  local name; name="$(printf '%02d' "$n")-$arm-$TICKER-$DATE"
  if [ "$arm" = "patched" ]; then git -C "$CLONE" checkout --quiet cache-fix-v0.7
  else git -C "$CLONE" checkout --quiet "$BASE_COMMIT"; fi
  echo "--- run $n: $arm $TICKER $DATE"
  rm -f "$WORK/memory/$name.md"
  TRADINGAGENTS_MEMORY_LOG_PATH="$WORK/memory/$name.md" \
  TRADINGAGENTS_CHECKPOINT_ENABLED=false \
    "$VPY" "$HERE/exp_runner.py" --arm "$arm" --ticker "$TICKER" --date "$DATE" \
      --out "$RUNS/$name" --rounds 1 --ta-commit "$(git -C "$CLONE" rev-parse HEAD)"
}

n=0
for pair in $(seq 1 "$PAIRS"); do
  if [ $((pair % 2)) -eq 1 ]; then order="baseline patched"; else order="patched baseline"; fi
  for arm in $order; do
    if [ "$n" -gt 0 ]; then echo "cooldown ${COOLDOWN}s"; sleep "$COOLDOWN"; fi
    n=$((n + 1))
    run_one "$n" "$arm"
  done
done

git -C "$CLONE" checkout --quiet "$BASE_COMMIT"
"$VPY" "$CAPTURED/redact.py" --paths "$RUNS"
echo "done: $n runs in $RUNS"
