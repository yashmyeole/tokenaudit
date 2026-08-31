#!/usr/bin/env bash
# Before/after experiment: does the cache-friendly prompt layout patch
# actually raise TradingAgents' cache hit rate? (See README.md for design.)
#
# 8 runs total, in this order (>= COOLDOWN seconds apart so one run's
# provider-side cache cannot leak into the next — gpt-5.4-mini's implicit
# cache lives ~5-10 idle minutes):
#
#   1 baseline NVDA:2026-08-14      5 patched  AAPL:2026-08-14
#   2 patched  NVDA:2026-08-14      6 baseline NVDA:2026-08-21
#   3 control  NVDA:2026-08-14      7 patched  NVDA:2026-08-21
#   4 baseline AAPL:2026-08-14      8 control  NVDA:2026-08-14
#
#   baseline = TradingAgents @ the pinned commit, traced (as in Phase 2)
#   patched  = same commit + cache_friendly_prompts.patch, traced
#   control  = baseline, NO tracing, usage recorded by a passive httpx tee
#              (answers: does tracing itself change cache behavior?)
#
# Arm order alternates within pairs to cancel time-of-day effects. Cost:
# ~8 runs x ~100k tokens on gpt-5.4-mini — around a dollar. Wall time is
# dominated by the 7 cooldowns: ~2h15m. Start it and walk away.
#
# Usage:
#   export OPENAI_API_KEY=...     # never committed, never leaves this shell
#   bash experiments/tradingagents-cache-fix/run_experiment.sh
#
# Optional env: COOLDOWN (seconds between runs, default 900)

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

# --- workspace: reuse the Phase-2 capture venv + clone -----------------------
if [ ! -x "$WORK/venv/bin/python" ] || [ ! -d "$CLONE/.git" ]; then
  echo "capture workspace missing; building it via the Phase-2 harness (smoke only)..."
  mkdir -p "$WORK"
  [ -d "$CLONE/.git" ] || git clone https://github.com/TauricResearch/TradingAgents.git "$CLONE"
  "$(command -v python3.12 || command -v python3)" -m venv "$WORK/venv" 2>/dev/null || true
fi
VPY="$WORK/venv/bin/python"

# Make sure the pinned commit is reachable (the capture clone is shallow).
if ! git -C "$CLONE" cat-file -e "$BASE_COMMIT^{commit}" 2>/dev/null; then
  git -C "$CLONE" fetch --quiet origin "$BASE_COMMIT" \
    || git -C "$CLONE" fetch --quiet --unshallow origin
fi
git -C "$CLONE" checkout --quiet "$BASE_COMMIT"

# (Re)create the patched branch from the pinned commit, apply the patch.
git -C "$CLONE" branch -f cache-fix "$BASE_COMMIT"
git -C "$CLONE" checkout --quiet cache-fix
git -C "$CLONE" apply --check "$HERE/cache_friendly_prompts.patch"
git -C "$CLONE" apply "$HERE/cache_friendly_prompts.patch"
git -C "$CLONE" -c user.name=experiment -c user.email=experiment@local \
  commit --quiet -am "experiment: cache-friendly prompt layout"
git -C "$CLONE" checkout --quiet "$BASE_COMMIT"

"$VPY" -m pip install --quiet --upgrade pip
"$VPY" -m pip install --quiet -e "$CLONE"
"$VPY" -m pip install --quiet opentelemetry-sdk \
  openinference-instrumentation-openai openinference-instrumentation-langchain

# Plumbing checks before spending money (no API calls, no key needed).
"$VPY" "$CAPTURED/runner.py" --smoke --out "$WORK/smoke-exp" >/dev/null
echo "exporter plumbing OK"
"$VPY" "$HERE/exp_runner.py" --selftest   # the control tee must see the SDK's HTTP path
"$VPY" - <<'PY' || { echo "network/DNS check failed — is the machine online?"; exit 1; }
import socket, sys
try:
    socket.getaddrinfo("api.openai.com", 443)
except OSError as exc:
    print(f"cannot resolve api.openai.com: {exc}", file=sys.stderr)
    raise SystemExit(1)
print("network OK: api.openai.com resolves")
PY

mkdir -p "$RUNS"

checkout_arm() {  # baseline|patched
  if [ "$1" = "patched" ]; then git -C "$CLONE" checkout --quiet cache-fix
  else git -C "$CLONE" checkout --quiet "$BASE_COMMIT"; fi
}

run_one() {  # n arm ticker date [--control]
  local n="$1" arm="$2" ticker="$3" date="$4" extra="${5:-}"
  local out="$RUNS/$(printf '%02d' "$n")-$arm${extra:+-control}-$ticker-$date"
  checkout_arm "$arm"
  echo "--- run $n/8: $arm${extra:+ (control)} $ticker $date -> $out"
  "$VPY" "$HERE/exp_runner.py" --arm "$arm" --ticker "$ticker" --date "$date" \
    --out "$out" --rounds 1 --ta-commit "$(git -C "$CLONE" rev-parse HEAD)" $extra
}

cool() { echo "cooldown ${COOLDOWN}s (cache isolation)..."; sleep "$COOLDOWN"; }

run_one 1 baseline NVDA 2026-08-14           ; cool
run_one 2 patched  NVDA 2026-08-14           ; cool
run_one 3 baseline NVDA 2026-08-14 --control ; cool
run_one 4 baseline AAPL 2026-08-14           ; cool
run_one 5 patched  AAPL 2026-08-14           ; cool
run_one 6 baseline NVDA 2026-08-21           ; cool
run_one 7 patched  NVDA 2026-08-21           ; cool
run_one 8 baseline NVDA 2026-08-14 --control

git -C "$CLONE" checkout --quiet "$BASE_COMMIT"

# Redact traced runs before anything can be committed (usage.jsonl holds
# only token counts, but redact.py verifying that costs nothing).
"$VPY" "$CAPTURED/redact.py" --paths "$RUNS"

echo
echo "done. analyze with:"
echo "  python experiments/tradingagents-cache-fix/analyze.py"
