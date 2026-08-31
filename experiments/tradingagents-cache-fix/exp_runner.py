"""One experiment run: TradingAgents, traced or usage-tee'd, into an out dir.

Thin wrapper over ``corpus/captured/runner.py`` (imported, not duplicated):
the traced mode is exactly the Phase-2 capture; ``--control`` swaps the
OpenTelemetry instrumentation for a passive tee that only records the
``usage`` object from each OpenAI response, wrapping whichever HTTP module
the installed SDK actually uses (see ``_http_modules``). The control exists to answer
one auditor question honestly: does observing the runs change the cache
behavior being measured? The tee touches no prompt bytes and no request
ordering, so its hit rates are what an *unobserved* run achieves.

Executed by run_experiment.sh inside the capture venv. Not part of the
installable package.
"""

from __future__ import annotations

import argparse
import importlib
import json
import platform
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "corpus" / "captured"))
import runner  # noqa: E402  (build_provider, instrument, run_analysis, _pkg_version)


def _http_modules() -> list[ModuleType]:
    """The HTTP client module(s) the OpenAI SDK actually uses.

    Not a guess: the installed ``openai`` package is introspected for the
    httpx-like module it imported, because the ecosystem now ships two
    generations side by side (``httpx`` 0.x and ``httpx2``) and the SDK may
    be on either. Patching the wrong one is how the first control runs
    recorded nothing while appearing to work — the tee wrapped a module no
    request ever passed through.
    """
    found: dict[str, ModuleType] = {}
    try:
        import openai._base_client as base_client

        for value in vars(base_client).values():
            if (
                isinstance(value, ModuleType)
                and hasattr(value, "Client")
                and hasattr(value, "AsyncClient")
            ):
                found[value.__name__] = value
    except Exception:  # SDK layout changed, or openai not importable here
        pass
    for name in ("httpx", "httpx2"):  # belt and braces
        try:
            module = importlib.import_module(name)
        except ImportError:
            continue
        if hasattr(module, "Client") and hasattr(module, "AsyncClient"):
            found.setdefault(module.__name__, module)
    if not found:
        raise RuntimeError(
            "no httpx-like HTTP module found to observe; the control run would measure nothing"
        )
    return list(found.values())


def install_usage_tee(out_path: Path) -> list[str]:
    """Record the usage block of every OpenAI JSON response; change nothing.

    Both the sync and async clients of every candidate module are wrapped
    (a response's content is cached after the first read, so reading it here
    does not disturb the SDK's own read). Returns the module names patched,
    which go into the manifest — a run whose recorded module list does not
    include the one the SDK used is a run to distrust.
    """
    fh = out_path.open("a", encoding="utf-8")

    def record(content: bytes) -> None:
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

    patched: list[str] = []
    for module in _http_modules():
        sync_send = module.Client.send

        def send(self: Any, request: Any, _send: Any = sync_send, **kwargs: Any) -> Any:
            response = _send(self, request, **kwargs)
            try:
                if wanted(request, response):
                    record(response.read())
            except Exception:
                pass
            return response

        module.Client.send = send

        async_send = module.AsyncClient.send

        async def asend(self: Any, request: Any, _send: Any = async_send, **kwargs: Any) -> Any:
            response = await _send(self, request, **kwargs)
            try:
                if wanted(request, response):
                    record(await response.aread())
            except Exception:
                pass
            return response

        module.AsyncClient.send = asend
        patched.append(module.__name__)
    return patched


def selftest() -> int:
    """Prove the tee observes the SDK's real HTTP path — before spending money.

    Checks that (a) the module the installed openai SDK uses is among those
    the tee patches, and (b) a mocked OpenAI response is actually recorded.
    Costs nothing, needs no API key, and would have caught the httpx/httpx2
    mismatch that silently voided the first control runs.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "usage.jsonl"
        patched = install_usage_tee(out)
        print(f"tee patched: {', '.join(patched)}")

        sdk_module = None
        try:
            import openai._base_client as base_client

            for value in vars(base_client).values():
                if (
                    isinstance(value, ModuleType)
                    and hasattr(value, "Client")
                    and hasattr(value, "AsyncClient")
                ):
                    sdk_module = value.__name__
                    break
        except Exception as exc:
            print(f"WARNING: could not introspect the openai SDK ({exc})", file=sys.stderr)
        if sdk_module is not None:
            print(f"openai SDK HTTP module: {sdk_module}")
            if sdk_module not in patched:
                print(
                    f"ERROR: the tee does not patch {sdk_module}; a control run "
                    "would record nothing",
                    file=sys.stderr,
                )
                return 3

        module = importlib.import_module(patched[0] if sdk_module is None else sdk_module)
        payload = json.dumps({"model": "selftest", "usage": {"prompt_tokens": 7}}).encode()
        transport = module.MockTransport(
            lambda request: module.Response(
                200, headers={"content-type": "application/json"}, content=payload
            )
        )
        with module.Client(transport=transport) as client:
            client.send(client.build_request("POST", "https://api.openai.com/v1/responses"))
        if not out.read_text().strip():
            print("ERROR: mocked OpenAI response was not recorded by the tee", file=sys.stderr)
            return 3
    print("selftest OK: the tee records usage on the SDK's own HTTP path")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--selftest", action="store_true", help="verify the tee, then exit")
    parser.add_argument("--ticker")
    parser.add_argument("--date")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--arm", choices=["baseline", "patched"])
    parser.add_argument("--deep", default="gpt-5.4-mini")
    parser.add_argument("--quick", default="gpt-5.4-mini")
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--ta-commit", default="unknown")
    parser.add_argument(
        "--control", action="store_true", help="no tracing; record usage via httpx tee"
    )
    args = parser.parse_args()

    if args.selftest:
        raise SystemExit(selftest())
    missing = [n for n in ("ticker", "date", "out", "arm") if getattr(args, n) is None]
    if missing:
        parser.error("missing required arguments: " + ", ".join("--" + n for n in missing))

    args.out.mkdir(parents=True, exist_ok=True)
    started = time.time()

    if args.control:
        patched = install_usage_tee(args.out / "usage.jsonl")
        observation = f"usage-tee({'+'.join(patched)})"
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
    print(f"{args.out.name} [{args.arm}/{observation}]: {manifest['duration_s']}s")
    if args.control and (args.out / "usage.jsonl").stat().st_size == 0:
        print(
            f"ERROR: {args.out.name}: the usage tee recorded nothing (watched "
            f"{observation}) — this control run measured NOTHING and must be re-run",
            file=sys.stderr,
        )
        raise SystemExit(3)


if __name__ == "__main__":
    main()
