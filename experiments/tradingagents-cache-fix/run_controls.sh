#!/usr/bin/env bash
# Re-run ONLY the two control runs (03 and 08) — the untraced arm that
# answers "does tracing change the cache behavior we measure?".
#
# Why they need re-running: the first attempt's usage tee patched `httpx`,
# but the OpenAI SDK in the capture venv uses `httpx2`, so the tee wrapped a
# module no request passed through and recorded nothing. exp_runner.py now
# derives the module from the installed SDK, patches every candidate, and
# exits non-zero if a control run ends with an empty tee. `--selftest`
# proves all of that against a mocked response before any money is spent.
#
# The six traced runs are untouched and remain valid.
#
# Usage:
#   export OPENAI_API_KEY=...
#   bash experiments/tradingagents-cache-fix/run_controls.sh
#
# Env: COOLDOWN (between runs, default 900), INITIAL_COOLDOWN (before the
# first run, default = COOLDOWN; set to 0 if the API key has been idle for
# 15+ minutes already, e.g. after a failed attempt).

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
CAPTURED="$REPO/corpus/captured"
WORK="$CAPTURED/.work"
CLONE="$WORK/TradingAgents"
RUNS="$HERE/runs"
BASE_COMMIT="a33fd4c0f134485a43553a2c23a63cb14adbd88f"
COOLDOWN="${COOLDOWN:-900}"
INITIAL_COOLDOWN="${INITIAL_COOLDOWN:-$COOLDOWN}"

: "${OPENAI_API_KEY:?Set OPENAI_API_KEY in your environment first}"
VPY="$WORK/venv/bin/python"
[ -x "$VPY" ] || { echo "capture venv missing; run run_experiment.sh first"; exit 1; }

# --- preflight: fail fast, before cooldowns and before spending -------------
# The previous attempt died mid-run on DNS failure ([Errno 8] nodename nor
# servname provided) after the network dropped. Check reachability first.
"$VPY" - <<'PY' || { echo "network/DNS check failed — is the machine online?"; exit 1; }
import socket, sys
try:
    socket.getaddrinfo("api.openai.com", 443)
except OSError as exc:
    print(f"cannot resolve api.openai.com: {exc}", file=sys.stderr)
    raise SystemExit(1)
print("network OK: api.openai.com resolves")
PY

# The tee must demonstrably observe the SDK's own HTTP path.
"$VPY" "$HERE/exp_runner.py" --selftest

git -C "$CLONE" checkout --quiet "$BASE_COMMIT"

run_control() {  # n
  local out="$RUNS/$(printf '%02d' "$1")-baseline-control-NVDA-2026-08-14"
  rm -rf "$out"
  echo "--- control run $1 -> $out"
  "$VPY" "$HERE/exp_runner.py" --arm baseline --ticker NVDA --date 2026-08-14 \
    --out "$out" --rounds 1 --ta-commit "$BASE_COMMIT" --control
}

if [ "$INITIAL_COOLDOWN" -gt 0 ]; then
  echo "cooldown ${INITIAL_COOLDOWN}s first (isolate from recent API activity)..."
  sleep "$INITIAL_COOLDOWN"
fi
run_control 3
echo "cooldown ${COOLDOWN}s (cache isolation)..."
sleep "$COOLDOWN"
run_control 8

"$VPY" "$CAPTURED/redact.py" --paths "$RUNS"

echo
echo "done. regenerate results with:"
echo "  python experiments/tradingagents-cache-fix/analyze.py"
