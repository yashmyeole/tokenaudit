"""Capture one real TradingAgents run as OpenTelemetry spans in a JSONL file.

Zero-modification capture: TradingAgents' code is untouched. Instrumentation
wraps the provider SDK (OpenAI or Anthropic) and LangChain at process level,
the same zero-integration posture tokenaudit itself takes. Spans are written
line-by-line using the OTel Python SDK's own ``ReadableSpan.to_json()``
rendering, with no custom mapping, so the captured dialect is exactly what
the SDK produces in the wild.

This script is executed by ``capture_tradingagents.sh`` inside a throwaway
virtualenv on the machine that holds the API key. It is not part of the
installable package.

``--smoke`` exercises the provider/exporter plumbing with one hand-made span
and no third-party imports beyond the OTel SDK, so the pipeline can be tested
without an API key or TradingAgents installed.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult


class JsonlSpanExporter(SpanExporter):
    """Writes each finished span as one compact JSON line, SDK-native schema."""

    def __init__(self, path: Path) -> None:
        self._fh = path.open("a", encoding="utf-8")

    def export(self, spans: Any) -> SpanExportResult:
        for span in spans:
            line = json.dumps(json.loads(span.to_json()), separators=(",", ":"))
            self._fh.write(line + "\n")
        self._fh.flush()
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        self._fh.close()

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        self._fh.flush()
        return True


def build_provider(trace_file: Path, run_id: str, ta_commit: str) -> TracerProvider:
    resource = Resource.create(
        {
            "service.name": "tradingagents-capture",
            "tokenaudit.capture": "tradingagents",
            "tokenaudit.run_id": run_id,
            "tokenaudit.tradingagents_commit": ta_commit,
        }
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(SimpleSpanProcessor(JsonlSpanExporter(trace_file)))
    return provider


def instrument(provider: TracerProvider, llm_provider: str = "openai") -> list[str]:
    """Attach OpenInference instrumentors; return the ones that activated."""
    active: list[str] = []
    try:
        if llm_provider == "anthropic":
            from openinference.instrumentation.anthropic import AnthropicInstrumentor

            AnthropicInstrumentor().instrument(tracer_provider=provider)
            active.append("openinference-instrumentation-anthropic")
        else:
            from openinference.instrumentation.openai import OpenAIInstrumentor

            OpenAIInstrumentor().instrument(tracer_provider=provider)
            active.append("openinference-instrumentation-openai")
    except Exception as exc:
        print(f"warning: {llm_provider} SDK instrumentor not active: {exc}", file=sys.stderr)
    try:
        from openinference.instrumentation.langchain import LangChainInstrumentor

        LangChainInstrumentor().instrument(tracer_provider=provider)
        active.append("openinference-instrumentation-langchain")
    except Exception as exc:
        print(f"warning: LangChain instrumentor not active: {exc}", file=sys.stderr)
    if not active:
        raise RuntimeError("no instrumentor activated; captured trace would be empty")
    return active


def run_analysis(
    ticker: str, date: str, deep: str, quick: str, rounds: int, llm_provider: str = "openai"
) -> str:
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    config = dict(DEFAULT_CONFIG)
    config["llm_provider"] = llm_provider
    config["deep_think_llm"] = deep
    config["quick_think_llm"] = quick
    config["max_debate_rounds"] = rounds
    config["online_tools"] = True

    graph = TradingAgentsGraph(debug=False, config=config)
    _, decision = graph.propagate(ticker, date)
    return str(decision)


def _pkg_version(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:
        return "not-installed"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    parser.add_argument("--ticker", default="NVDA")
    parser.add_argument("--date", default="2026-08-14")
    parser.add_argument("--out", type=Path, required=True, help="run output directory")
    parser.add_argument("--deep", default="gpt-5.4-mini")
    parser.add_argument("--quick", default="gpt-5.4-mini")
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--provider", choices=["openai", "anthropic"], default="openai")
    parser.add_argument("--ta-commit", default="unknown")
    parser.add_argument("--smoke", action="store_true", help="plumbing test, no API calls")
    args = parser.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    trace_file = args.out / "trace.jsonl"
    run_id = args.out.name
    provider = build_provider(trace_file, run_id, args.ta_commit)

    started = time.time()
    manifest: dict[str, Any] = {
        "run_id": run_id,
        "smoke": args.smoke,
        "ticker": args.ticker,
        "trade_date": args.date,
        "llm_provider": args.provider,
        "models": {"deep_think": args.deep, "quick_think": args.quick},
        "max_debate_rounds": args.rounds,
        "tradingagents_commit": args.ta_commit,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "versions": {
            name: _pkg_version(name)
            for name in (
                "tradingagents",
                "opentelemetry-sdk",
                "openinference-instrumentation-openai",
                "openinference-instrumentation-anthropic",
                "openinference-instrumentation-langchain",
                "openai",
                "anthropic",
                "langchain-anthropic",
                "langchain-core",
            )
        },
    }

    if args.smoke:
        tracer = provider.get_tracer("tokenaudit.smoke")
        with tracer.start_as_current_span("chat smoke-model") as span:
            span.set_attribute("gen_ai.operation.name", "chat")
            span.set_attribute("gen_ai.provider.name", "smoke")
            span.set_attribute("gen_ai.request.model", "smoke-model")
            span.set_attribute("gen_ai.usage.input_tokens", 1234)
            span.set_attribute("gen_ai.usage.output_tokens", 56)
        manifest["instrumentors"] = []
        decision = "smoke"
    else:
        manifest["instrumentors"] = instrument(provider, args.provider)
        decision = run_analysis(
            args.ticker, args.date, args.deep, args.quick, args.rounds, args.provider
        )
        (args.out / "decision.txt").write_text(decision + "\n", encoding="utf-8")

    provider.force_flush()
    provider.shutdown()

    manifest["duration_s"] = round(time.time() - started, 1)
    manifest["span_count"] = sum(1 for _ in trace_file.open(encoding="utf-8"))
    (args.out / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"{run_id}: {manifest['span_count']} spans, {manifest['duration_s']}s -> {args.out}")


if __name__ == "__main__":
    main()
