"""One experiment run: TradingAgents, traced or usage-tee'd, into an out dir.

Thin wrapper over ``corpus/captured/runner.py`` (imported, not duplicated):
the traced mode is exactly the Phase-2 capture; ``--control`` swaps the
OpenTelemetry instrumentation for a 20-line httpx tee that only records the
``usage`` object from each OpenAI response. The control exists to answer
one auditor question honestly: does observing the runs change the cache
behavior being measured? The tee touches no prompt bytes and no request
ordering, so its hit rates are what an *unobserved* run achieves.

Executed by run_experiment.sh inside the capture venv. Not part of the
installable package.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "corpus" / "captured"))
import runner  # noqa: E402  (build_provider, instrument, run_analysis, _pkg_version)


def install_usage_tee(out_path: Path) -> None:
    """Record the usage block of every OpenAI JSON response; change nothing.

    Both httpx clients are patched: langchain-openai drives the *async*
    client even under a synchronous ``.invoke()``, which is exactly how the
    first version of this tee recorded zero requests — the sync-only patch
    never saw the traffic. (httpx caches a response's content after the
    first read, so reading it here does not disturb the SDK's own read.)
    """
    import httpx

    fh = out_path.open("a", encoding="utf-8")

    def record(request: Any, response: Any, content: bytes) -> None:
        try:
            data = json.loads(content)
            if isinstance(data, dict) and data.get("usage"):
                fh.write(
                    json.dumps(
                        {"model": data.get("model"), "usage": data["usage"]},
                        separators=(",", ":"),
                    )
                    + "\n"
                )
                fh.flush()
        except Exception:  # never let observation break the run
            pass

    def wanted(request: Any, response: Any) -> bool:
        content_type = response.headers.get("content-type", "")
        return "api.openai.com" in str(request.url) and content_type.startswith("application/json")

    sync_send = httpx.Client.send

    def send(self: Any, request: Any, **kwargs: Any) -> Any:
        response = sync_send(self, request, **kwargs)
        try:
            if wanted(request, response):
                record(request, response, response.read())
        except Exception:
            pass
        return response

    httpx.Client.send = send  # type: ignore[method-assign]

    async_send = httpx.AsyncClient.send

    async def asend(self: Any, request: Any, **kwargs: Any) -> Any:
        response = await async_send(self, request, **kwargs)
        try:
            if wanted(request, response):
                record(request, response, await response.aread())
        except Exception:
            pass
        return response

    httpx.AsyncClient.send = asend  # type: ignore[method-assign]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--arm", required=True, choices=["baseline", "patched"])
    parser.add_argument("--deep", default="gpt-5.4-mini")
    parser.add_argument("--quick", default="gpt-5.4-mini")
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--ta-commit", default="unknown")
    parser.add_argument(
        "--control", action="store_true", help="no tracing; record usage via httpx tee"
    )
    args = parser.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    started = time.time()

    if args.control:
        install_usage_tee(args.out / "usage.jsonl")
        observation = "usage-tee(httpx)"
        provider = None
    else:
        provider = runner.build_provider(args.out / "trace.jsonl", args.out.name, args.ta_commit)
        observation = ",".join(runner.instrument(provider))

    decision = runner.run_analysis(args.ticker, args.date, args.deep, args.quick, args.rounds)
    (args.out / "decision.txt").write_text(decision + "\n", encoding="utf-8")
    if provider is not None:
        provider.force_flush()
        provider.shutdown()

    manifest: dict[str, Any] = {
        "experiment": "tradingagents-cache-fix",
        "arm": args.arm,
        "observation": observation,
        "run_id": args.out.name,
        "ticker": args.ticker,
        "trade_date": args.date,
        "models": {"deep_think": args.deep, "quick_think": args.quick},
        "max_debate_rounds": args.rounds,
        "tradingagents_commit": args.ta_commit,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
        "duration_s": round(time.time() - started, 1),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "versions": {
            name: runner._pkg_version(name)
            for name in ("tradingagents", "openai", "langchain-core", "opentelemetry-sdk")
        },
    }
    (args.out / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    if args.control and (args.out / "usage.jsonl").stat().st_size == 0:
        print(
            f"WARNING: {args.out.name}: the usage tee recorded nothing — "
            "the control run has no measurement and must be re-run",
            file=sys.stderr,
        )
    print(f"{args.out.name} [{args.arm}/{observation}]: {manifest['duration_s']}s")


if __name__ == "__main__":
    main()
