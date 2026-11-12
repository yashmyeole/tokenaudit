"""Deterministic trace-fixture generator for the tokenaudit corpus.

Generates OTLP/JSON trace files that simulate LLM agent sessions with known,
labeled token-waste failure modes. Every fixture ships with a ``.label.json``
naming the failure modes it contains and the modes a correct tool must NOT
report for it.

Design rules:

- **Deterministic.** No wall clock, no randomness, no floats in payloads.
  The same script produces byte-identical output on any machine, so a test
  can regenerate the corpus and diff it against the committed files.
- **Honest.** Every trace carries ``tokenaudit.synthetic = true`` in its
  resource attributes. These fixtures are unit-test ground truth, never
  evidence: published claims come only from captured runs of real projects
  (see ``corpus/README.md``).
- **Standards-faithful.** Emits three real schema generations, verified
  against primary sources on 2026-08-29:

  1. Current OTel GenAI semconv (all still "Development" status):
     ``gen_ai.usage.cache_read.input_tokens`` /
     ``gen_ai.usage.cache_creation.input_tokens``, with
     ``gen_ai.usage.input_tokens`` INCLUDING cached tokens.
     https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-spans.md
  2. Legacy flat names as emitted by older openllmetry:
     ``gen_ai.usage.cache_read_input_tokens`` /
     ``gen_ai.usage.cache_creation_input_tokens`` alongside deprecated
     ``gen_ai.usage.prompt_tokens``; provider fields passed through, so for
     Anthropic the prompt count EXCLUDES cached tokens.
     https://github.com/traceloop/openllmetry/issues/3647
  3. OpenInference: ``llm.token_count.prompt_details.cache_read`` /
     ``.cache_write`` with flattened message attributes.
     https://github.com/Arize-ai/openinference/blob/main/spec/semantic_conventions.md

The cache simulation follows the provider rules verified 2026-08-29 from the
Anthropic and OpenAI prompt-caching docs (minimum cacheable sizes, 4-breakpoint
cap, 20-position lookback, TTL measured from request start vs sliding). Where
this simulator simplifies, the simplification is commented at the site.

Run: ``python corpus/synthetic/generate.py`` (regenerates in place)

Compatible with Python 3.10+ so it can run on more machines than the package
itself (which requires 3.11+); stdlib only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- #
# Provider constants come from the packaged rulebook (the single source of
# truth; every entry is sourced and dated there). The byte-exact regeneration
# test in tests/test_corpus.py guards this coupling: a rulebook edit either
# leaves these fixtures unchanged or forces a deliberate corpus update.
# --------------------------------------------------------------------------- #

_RULEBOOK_DIR = Path(__file__).resolve().parents[2] / "src" / "tokenaudit" / "rulebook"


def _provider_rules(filename: str) -> dict[str, Any]:
    with open(_RULEBOOK_DIR / filename, encoding="utf-8") as fh:
        loaded: dict[str, Any] = json.load(fh)
    return loaded


_ANTHROPIC = _provider_rules("anthropic.json")
_OPENAI = _provider_rules("openai.json")

ANTHROPIC_MIN_CACHEABLE: dict[str, int] = {
    model: entry["min_cacheable_tokens"] for model, entry in _ANTHROPIC["models"].items()
}
ANTHROPIC_TTL_S: int = _ANTHROPIC["ttl"][0]["seconds"]  # 5m, from the START of the request
ANTHROPIC_MAX_BREAKPOINTS: int = _ANTHROPIC["max_explicit_breakpoints"]
ANTHROPIC_LOOKBACK_POSITIONS: int = _ANTHROPIC["lookback_positions"]

OPENAI_MIN_CACHEABLE: dict[str, int] = {
    model: entry["min_cacheable_tokens"]
    for model, entry in _OPENAI["models"].items()
    if entry["min_cacheable_tokens"] is not None
}
OPENAI_TTL_S: int = _OPENAI["models"]["gpt-5.6"]["ttl"][0]["seconds"]  # sliding from last use

FAILURE_MODES = (
    "prefix_drift",
    "sub_minimum",
    "model_switch",
    "ttl_expiry",
    "breakpoint_limit",
    "lookback_overflow",
    "tool_schema_churn",
    "context_bloat",
    "retry_storm",
    "misordered_shared_context",
    "cross_session_drift",
)

# Fixed simulated clock: 2026-01-15T09:00:00Z. Never use the wall clock.
BASE_EPOCH_S = 1_768_467_600

SCOPE = {"name": "tokenaudit-corpus-simulator", "version": "0.1.0"}


def tokens(text: str) -> int:
    """Deterministic stand-in tokenizer: ~4 chars per token, minimum 1."""
    return max(1, len(text) // 4)


def filler(topic: str, n_tokens: int) -> str:
    """Deterministic prose of exactly ``n_tokens`` simulated tokens."""
    out: list[str] = []
    i = 0
    while sum(len(s) for s in out) < n_tokens * 4:
        i += 1
        out.append(f"Policy section {i}: standard operating guidance for {topic}, item {i}. ")
    return "".join(out)[: n_tokens * 4]


def notes(topic: str, n_tokens: int) -> str:
    """Deterministic multi-line text of about ``n_tokens``, one note per line."""
    lines: list[str] = []
    i = 0
    while sum(len(line) + 1 for line in lines) < n_tokens * 4:
        i += 1
        lines.append(f"- {topic} note {i}: figures and commentary for line item {i}.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Session model
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Block:
    """One prompt content block, in request order."""

    role: str
    text: str
    cache_marker: bool = False  # Anthropic cache_control on this block

    @property
    def digest(self) -> str:
        return hashlib.sha256(f"{self.role}\x00{self.text}".encode()).hexdigest()


@dataclass(frozen=True)
class Call:
    """One LLM request in a session."""

    blocks: tuple[Block, ...]
    model: str
    t_offset_s: int  # seconds after BASE_EPOCH_S at which the request starts
    duration_ms: int
    output_tokens: int
    conversation: str | None = None  # defaults to one conversation per fixture


@dataclass(frozen=True)
class Usage:
    """Simulated billing outcome of one call.

    ``uncached_input`` is the Anthropic-SDK-style exclusive count: tokens
    neither read from nor written to cache. Total input is always
    ``uncached_input + cache_read + cache_creation``.
    """

    uncached_input: int
    cache_read: int
    cache_creation: int
    output: int

    @property
    def total_input(self) -> int:
        return self.uncached_input + self.cache_read + self.cache_creation


@dataclass
class _CacheEntry:
    prefix_len: int  # number of blocks covered
    expires_at_s: int


class AnthropicCacheSim:
    """Simulates Anthropic prompt-cache behavior for one conversation.

    Modeled rules (sources in module docstring):
    - byte-identical prefix matching on block boundaries, per model
    - at most 4 explicit breakpoints are honored; extra markers are treated
      as silently ignored here (real-API behavior for >4 markers should be
      confirmed against the live API before any detector claims it)
    - a breakpoint checks at most 20 positions, counting itself as the first
    - entries live TTL seconds from the START of the request that wrote or
      last read them
    - a prefix below the model minimum is silently not cached
    """

    def __init__(self) -> None:
        # key: (model, sha256 chain of block digests up to a breakpoint)
        self._entries: dict[tuple[str, str], _CacheEntry] = {}

    @staticmethod
    def _chain(blocks: tuple[Block, ...], upto: int) -> str:
        h = hashlib.sha256()
        for b in blocks[: upto + 1]:
            h.update(bytes.fromhex(b.digest))
        return h.hexdigest()

    def simulate(self, call: Call) -> Usage:
        now = BASE_EPOCH_S + call.t_offset_s
        marked = [i for i, b in enumerate(call.blocks) if b.cache_marker]
        honored = marked[:ANTHROPIC_MAX_BREAKPOINTS]
        block_tok = [tokens(b.text) for b in call.blocks]
        min_cacheable = ANTHROPIC_MIN_CACHEABLE[call.model]

        # Longest live cached prefix among honored breakpoints, best first.
        read_upto = -1
        for bp in sorted(honored, reverse=True):
            key = (call.model, self._chain(call.blocks, bp))
            entry = self._entries.get(key)
            if entry is None or entry.expires_at_s <= now:
                continue
            # Lookback: the breakpoint checks at most 20 positions, counting
            # itself as the first, so a match more than 19 positions before
            # the breakpoint is not found.
            if bp - (entry.prefix_len - 1) >= ANTHROPIC_LOOKBACK_POSITIONS:
                continue
            entry.expires_at_s = now + ANTHROPIC_TTL_S  # read refreshes TTL
            read_upto = bp
            break

        cache_read = sum(block_tok[: read_upto + 1])

        # Write: cache from the end of the read through the last honored
        # breakpoint, provided the total cached prefix clears the minimum.
        write_upto = honored[-1] if honored else -1
        cache_creation = 0
        if write_upto > read_upto:
            prefix_tokens = sum(block_tok[: write_upto + 1])
            if prefix_tokens >= min_cacheable:
                cache_creation = sum(block_tok[read_upto + 1 : write_upto + 1])
                for bp in honored:
                    if bp > read_upto:
                        key = (call.model, self._chain(call.blocks, bp))
                        self._entries[key] = _CacheEntry(
                            prefix_len=bp + 1, expires_at_s=now + ANTHROPIC_TTL_S
                        )

        uncached = sum(block_tok) - cache_read - cache_creation
        return Usage(uncached, cache_read, cache_creation, call.output_tokens)


class OpenAICacheSim:
    """Simulates OpenAI automatic prefix caching (no explicit markers).

    Modeled rules: the longest block-aligned prefix shared with a previously
    seen request is served from cache when it clears the model minimum;
    entries stay usable for TTL seconds after their most recent write or
    reuse (sliding window, unlike Anthropic's from-request-start clock).
    Cache growth increments and multi-machine routing are not modeled.
    """

    def __init__(self) -> None:
        self._seen: dict[tuple[str, str], _CacheEntry] = {}

    def simulate(self, call: Call) -> Usage:
        now = BASE_EPOCH_S + call.t_offset_s
        block_tok = [tokens(b.text) for b in call.blocks]
        min_cacheable = OPENAI_MIN_CACHEABLE[call.model]

        cached_tokens = 0
        for upto in range(len(call.blocks) - 1, -1, -1):
            key = (call.model, AnthropicCacheSim._chain(call.blocks, upto))
            entry = self._seen.get(key)
            if entry is not None and entry.expires_at_s > now:
                cached_tokens = sum(block_tok[: upto + 1])
                entry.expires_at_s = now + OPENAI_TTL_S
                break

        # Every block-aligned prefix that clears the minimum becomes reusable.
        running = 0
        for upto, tok in enumerate(block_tok):
            running += tok
            if running >= min_cacheable:
                key = (call.model, AnthropicCacheSim._chain(call.blocks, upto))
                self._seen[key] = _CacheEntry(prefix_len=upto + 1, expires_at_s=now + OPENAI_TTL_S)

        # OpenAI usage is nested: cached_tokens is a detail of the input
        # total, and there is no separate billed cache-write count. We model
        # cache_creation as 0 and keep everything else in uncached_input.
        uncached = sum(block_tok) - cached_tokens
        return Usage(uncached, cached_tokens, 0, call.output_tokens)


# --------------------------------------------------------------------------- #
# OTLP/JSON emission
# --------------------------------------------------------------------------- #


def _attr(key: str, value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        # proto3 JSON mapping: int64 is encoded as a decimal string
        return {"key": key, "value": {"intValue": str(value)}}
    return {"key": key, "value": {"stringValue": str(value)}}


def _ids(fixture: str, i: int) -> tuple[str, str]:
    trace_id = hashlib.sha256(f"tokenaudit:{fixture}".encode()).hexdigest()[:32]
    span_id = hashlib.sha256(f"tokenaudit:{fixture}:{i}".encode()).hexdigest()[:16]
    return trace_id, span_id


def _messages_json(blocks: tuple[Block, ...]) -> str:
    msgs: list[dict[str, Any]] = []
    for b in blocks:
        part: dict[str, Any] = {"type": "text", "content": b.text}
        if b.cache_marker:
            part["cache_control"] = {"type": "ephemeral"}
        msgs.append({"role": b.role, "parts": [part]})
    return json.dumps(msgs, separators=(",", ":"), sort_keys=True)


def _span_shell(fixture: str, i: int, call: Call, name: str) -> dict[str, Any]:
    trace_id, span_id = _ids(fixture, i)
    start_ns = (BASE_EPOCH_S + call.t_offset_s) * 1_000_000_000
    end_ns = start_ns + call.duration_ms * 1_000_000
    return {
        "traceId": trace_id,
        "spanId": span_id,
        "name": name,
        "kind": 3,  # SPAN_KIND_CLIENT
        "startTimeUnixNano": str(start_ns),
        "endTimeUnixNano": str(end_ns),
        "attributes": [],
        "status": {"code": 1},  # STATUS_CODE_OK
    }


def emit_current(
    fixture: str, provider: str, calls: list[Call], usages: list[Usage], content: bool
) -> dict[str, Any]:
    """Current OTel GenAI semconv: input_tokens INCLUDES cached tokens."""
    spans: list[dict[str, Any]] = []
    session_uses_cache = any(u.cache_read or u.cache_creation for u in usages) or any(
        b.cache_marker for c in calls for b in c.blocks
    )
    for i, (call, usage) in enumerate(zip(calls, usages, strict=True)):
        span = _span_shell(fixture, i, call, f"chat {call.model}")
        attrs: list[dict[str, Any]] = [
            _attr("gen_ai.operation.name", "chat"),
            _attr("gen_ai.provider.name", provider),
            _attr("gen_ai.request.model", call.model),
            _attr("gen_ai.response.model", call.model),
            _attr("gen_ai.conversation.id", call.conversation or f"conv-{fixture}"),
            _attr("gen_ai.usage.input_tokens", usage.total_input),
            _attr("gen_ai.usage.output_tokens", usage.output),
        ]
        if session_uses_cache:
            attrs.append(_attr("gen_ai.usage.cache_read.input_tokens", usage.cache_read))
            attrs.append(_attr("gen_ai.usage.cache_creation.input_tokens", usage.cache_creation))
        if content:
            attrs.append(_attr("gen_ai.input.messages", _messages_json(call.blocks)))
            out = json.dumps(
                [{"role": "assistant", "parts": [{"type": "text", "content": f"reply {i}"}]}],
                separators=(",", ":"),
                sort_keys=True,
            )
            attrs.append(_attr("gen_ai.output.messages", out))
        span["attributes"] = attrs
        spans.append(span)
    return _wrap(fixture, spans)


def emit_legacy(
    fixture: str, provider: str, calls: list[Call], usages: list[Usage]
) -> dict[str, Any]:
    """Legacy flat naming (older openllmetry): provider fields passed through.

    For Anthropic that means ``gen_ai.usage.prompt_tokens`` EXCLUDES cache
    tokens — the accounting trap a normalizer has to fix.
    """
    spans: list[dict[str, Any]] = []
    for i, (call, usage) in enumerate(zip(calls, usages, strict=True)):
        span = _span_shell(fixture, i, call, "anthropic.chat")
        span["attributes"] = [
            _attr("gen_ai.system", provider),
            _attr("gen_ai.request.model", call.model),
            _attr("gen_ai.usage.prompt_tokens", usage.uncached_input),
            _attr("gen_ai.usage.completion_tokens", usage.output),
            _attr("gen_ai.usage.cache_read_input_tokens", usage.cache_read),
            _attr("gen_ai.usage.cache_creation_input_tokens", usage.cache_creation),
            _attr("gen_ai.prompt.0.role", call.blocks[0].role),
            _attr("gen_ai.prompt.0.content", call.blocks[0].text),
        ]
        spans.append(span)
    return _wrap(fixture, spans)


def emit_openinference(
    fixture: str, provider: str, calls: list[Call], usages: list[Usage]
) -> dict[str, Any]:
    """OpenInference: llm.* attributes, flattened messages, nested counts."""
    spans: list[dict[str, Any]] = []
    for i, (call, usage) in enumerate(zip(calls, usages, strict=True)):
        span = _span_shell(fixture, i, call, "ChatCompletion")
        attrs: list[dict[str, Any]] = [
            _attr("openinference.span.kind", "LLM"),
            _attr("llm.system", provider),
            _attr("llm.provider", provider),
            _attr("llm.model_name", call.model),
            _attr("session.id", f"conv-{fixture}"),
            _attr("llm.token_count.prompt", usage.total_input),
            _attr("llm.token_count.completion", usage.output),
            _attr("llm.token_count.total", usage.total_input + usage.output),
            _attr("llm.token_count.prompt_details.cache_read", usage.cache_read),
            _attr("llm.token_count.prompt_details.cache_write", usage.cache_creation),
        ]
        for j, b in enumerate(call.blocks):
            attrs.append(_attr(f"llm.input_messages.{j}.message.role", b.role))
            attrs.append(_attr(f"llm.input_messages.{j}.message.content", b.text))
        attrs.append(_attr("llm.output_messages.0.message.role", "assistant"))
        attrs.append(_attr("llm.output_messages.0.message.content", f"reply {i}"))
        span["attributes"] = attrs
        spans.append(span)
    return _wrap(fixture, spans)


def _wrap(fixture: str, spans: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        _attr("service.name", "tokenaudit-fixture-agent"),
                        _attr("tokenaudit.fixture", fixture),
                        _attr("tokenaudit.synthetic", True),
                    ]
                },
                "scopeSpans": [{"scope": SCOPE, "spans": spans}],
            }
        ]
    }


# --------------------------------------------------------------------------- #
# Fixture definitions
# --------------------------------------------------------------------------- #


@dataclass
class Fixture:
    name: str
    schema: str  # otel-genai-current | otel-genai-legacy-flat | openinference
    provider_shape: str  # anthropic | openai
    contains: list[str]
    evidence: dict[str, Any]
    notes: str
    calls: list[Call]
    content_captured: bool = True
    extra_labels: dict[str, Any] = field(default_factory=dict)


def _stable_system(n_tokens: int = 1400) -> Block:
    return Block("system", "You are the fixture agent. " + filler("fixture ops", n_tokens), True)


def _convo(i: int, extra: str = "") -> Block:
    return Block("user", f"Task {i}: summarize account activity for window {i}. {extra}")


def fx_prefix_drift() -> Fixture:
    calls = []
    for i in range(6):
        stamped = Block(
            "system",
            f"You are the fixture agent. [generated: 2026-01-15T09:{i:02d}:00Z] "
            + filler("fixture ops", 1400),
            True,
        )
        calls.append(Call((stamped, _convo(i)), "claude-sonnet-4-5", i * 20, 2400, 180))
    return Fixture(
        name="prefix_drift",
        schema="otel-genai-current",
        provider_shape="anthropic",
        contains=["prefix_drift"],
        evidence={"drift_block_index": 0, "calls": 6, "drift_kind": "timestamp"},
        notes=(
            "The system prompt embeds a per-call timestamp, so the marked prefix never "
            "matches and every call pays a cache write instead of a read."
        ),
        calls=calls,
    )


def fx_sub_minimum() -> Fixture:
    small = Block("system", "You are terse. " + filler("brief ops", 280), True)
    calls = [Call((small, _convo(i)), "claude-sonnet-4-5", i * 20, 1800, 120) for i in range(5)]
    return Fixture(
        name="sub_minimum",
        schema="otel-genai-current",
        provider_shape="anthropic",
        contains=["sub_minimum"],
        evidence={"marked_block_tokens": tokens(small.text), "model_minimum": 1024},
        notes=(
            "A ~300-token block is marked for caching on a model with a 1024-token "
            "minimum; the provider silently ignores the marker, so nothing is ever "
            "written or read. The content is stable: a correct tool must not call "
            "this prefix drift."
        ),
        calls=calls,
    )


def fx_model_switch() -> Fixture:
    sys_b = _stable_system()
    calls = []
    for i in range(6):
        model = "claude-sonnet-4-5" if i < 3 else "claude-opus-4-8"
        calls.append(Call((sys_b, _convo(i)), model, i * 20, 2600, 200))
    return Fixture(
        name="model_switch",
        schema="otel-genai-current",
        provider_shape="anthropic",
        contains=["model_switch"],
        evidence={"switch_at_call": 3, "from": "claude-sonnet-4-5", "to": "claude-opus-4-8"},
        notes=(
            "The session switches models mid-way with an identical prompt; caches are "
            "per-model, so the switch throws away the warm cache and pays a re-write."
        ),
        calls=calls,
    )


def fx_ttl_expiry() -> Fixture:
    sys_b = _stable_system()
    offsets = [0, 20, 40, 40 + 420, 40 + 440]  # 7-minute gap after call 2
    calls = [
        Call((sys_b, _convo(i)), "claude-sonnet-4-5", off, 2200, 160)
        for i, off in enumerate(offsets)
    ]
    return Fixture(
        name="ttl_expiry",
        schema="otel-genai-current",
        provider_shape="anthropic",
        contains=["ttl_expiry"],
        evidence={"gap_after_call": 2, "gap_seconds": 420, "ttl_seconds": 300},
        notes=(
            "A 7-minute idle gap exceeds the 5-minute cache lifetime (measured from "
            "the start of the request that wrote or last read the entry), so the next "
            "call pays a full re-write despite an identical prompt."
        ),
        calls=calls,
    )


def fx_breakpoint_limit() -> Fixture:
    docs = [Block("user", filler(f"reference doc {j}", 700), True) for j in range(5)]
    blocks = (_stable_system(), *docs)  # 6 marked blocks; only 4 honored
    calls = [Call((*blocks, _convo(i)), "claude-sonnet-4-5", i * 20, 3000, 220) for i in range(4)]
    return Fixture(
        name="breakpoint_limit",
        schema="otel-genai-current",
        provider_shape="anthropic",
        contains=["breakpoint_limit"],
        evidence={"marked_blocks": 6, "honored": ANTHROPIC_MAX_BREAKPOINTS},
        notes=(
            "Six blocks carry cache markers but the provider honors at most four, so "
            "the tail blocks are re-billed on every call. Simulated as silent "
            "truncation; confirm live-API behavior for >4 markers before a detector "
            "claims this in production."
        ),
        calls=calls,
    )


def fx_lookback_overflow() -> Fixture:
    sys_b = _stable_system()
    calls = []
    for i in range(3):
        # History grows by 24 sizable blocks per call and the moving marker
        # rides the NEWEST history block, so each new breakpoint sits 24
        # positions past the previously cached prefix - beyond the
        # 20-position lookback - and the identical, still-live prefix is
        # never found again. Only the sys-block breakpoint keeps reading.
        n = (i + 1) * 24
        history = [
            Block("user" if j % 2 == 0 else "assistant", filler(f"turn {j}", 120)) for j in range(n)
        ]
        last = history[-1]
        history[-1] = Block(last.role, last.text, True)
        calls.append(Call((sys_b, *history, _convo(i)), "claude-sonnet-4-5", i * 20, 2800, 240))
    return Fixture(
        name="lookback_overflow",
        schema="otel-genai-current",
        provider_shape="anthropic",
        contains=["lookback_overflow"],
        evidence={"history_growth_blocks": 24, "lookback_positions": 20},
        notes=(
            "The cache marker rides on the newest message while history grows by ~24 "
            "blocks per call, so the previously cached prefix lies outside the "
            "20-position lookback window and is never reused."
        ),
        calls=calls,
    )


def fx_tool_schema_churn() -> Fixture:
    tool_a = "Tool get_report: fetch a report by id. Parameters: id (string), format (string). "
    tool_b = "Tool send_alert: send an alert. Parameters: channel (string), severity (string). "
    sys_b = _stable_system(900)
    calls = []
    for i in range(6):
        order = tool_a + tool_b if i != 3 else tool_b + tool_a  # reorder on call 3
        tools = Block("system", "Available tools: " + order + filler("tool details", 2000), True)
        calls.append(Call((tools, sys_b, _convo(i)), "claude-sonnet-4-5", i * 20, 2600, 200))
    return Fixture(
        name="tool_schema_churn",
        schema="otel-genai-current",
        provider_shape="anthropic",
        contains=["tool_schema_churn"],
        evidence={"churn_at_call": 3, "churn_kind": "reorder"},
        notes=(
            "Two tool definitions swap order on one call. Tool definitions sit at the "
            "top of the invalidation cascade, so the reorder rebuilds the entire "
            "cache. Ground truth attributes this to the tools block, not generic "
            "prefix drift; a correct tool reports tool_schema_churn here."
        ),
        calls=calls,
    )


def fx_context_bloat() -> Fixture:
    sys_b = Block("system", "You are the fixture agent. " + filler("fixture ops", 1400))
    calls = []
    for i in range(8):
        history = tuple(
            Block("user" if j % 2 == 0 else "assistant", filler(f"turn {j}", 260))
            for j in range(i * 2)
        )
        calls.append(Call((sys_b, *history, _convo(i)), "claude-sonnet-4-5", i * 20, 3200, 260))
    return Fixture(
        name="context_bloat",
        schema="otel-genai-current",
        provider_shape="anthropic",
        contains=["context_bloat"],
        evidence={"growth_blocks_per_call": 2, "calls": 8, "cache_markers": 0},
        notes=(
            "The full conversation is re-sent every call with no caching at all, so "
            "input grows linearly and every repeated token is billed at full price."
        ),
        calls=calls,
    )


def fx_retry_storm() -> Fixture:
    sys_b = Block("system", "You are the fixture agent. " + filler("fixture ops", 1200))
    ask = Block("user", "Task 0: summarize account activity for window 0.")
    calls = [Call((sys_b, ask), "claude-sonnet-4-5", off, 9000, 210) for off in (0, 12, 25, 39)]
    calls.append(Call((sys_b, _convo(1)), "claude-sonnet-4-5", 80, 2400, 190))
    return Fixture(
        name="retry_storm",
        schema="otel-genai-current",
        provider_shape="anthropic",
        contains=["retry_storm"],
        evidence={"duplicate_calls": 4, "window_seconds": 39},
        notes=(
            "Four byte-identical requests complete within 40 seconds — the client "
            "retried on timeouts while the server billed each attempt. No caching is "
            "configured, so each duplicate pays the full prompt again."
        ),
        calls=calls,
    )


def fx_healthy_cached() -> Fixture:
    sys_b = _stable_system()
    calls = [Call((sys_b, _convo(i)), "claude-sonnet-4-5", i * 20, 2200, 170) for i in range(6)]
    return Fixture(
        name="healthy_cached",
        schema="otel-genai-current",
        provider_shape="anthropic",
        contains=[],
        evidence={"calls": 6},
        notes=(
            "A stable marked prefix reused every 20 seconds: one write, then reads. "
            "Negative fixture — a correct tool reports nothing here."
        ),
        calls=calls,
    )


def fx_healthy_incremental() -> Fixture:
    sys_b = _stable_system()
    calls = []
    for i in range(6):
        history = tuple(
            Block(
                "user" if j % 2 == 0 else "assistant",
                filler(f"turn {j}", 300),
                cache_marker=(j == i * 2 - 1),  # marker rides the newest turn
            )
            for j in range(i * 2)
        )
        calls.append(Call((sys_b, *history, _convo(i)), "claude-sonnet-4-5", i * 20, 2600, 210))
    return Fixture(
        name="healthy_incremental",
        schema="otel-genai-current",
        provider_shape="anthropic",
        contains=[],
        evidence={"growth_blocks_per_call": 2, "calls": 6},
        notes=(
            "History grows, but a moving breakpoint keeps the whole prefix cached "
            "incrementally within the lookback window. Negative fixture guarding the "
            "context_bloat and lookback_overflow detectors against false positives."
        ),
        calls=calls,
    )


def fx_healthy_small() -> Fixture:
    sys_b = Block("system", "You are terse. Answer in one sentence.")
    calls = [Call((sys_b, _convo(i)), "claude-sonnet-4-5", i * 30, 900, 40) for i in range(3)]
    return Fixture(
        name="healthy_small",
        schema="otel-genai-current",
        provider_shape="anthropic",
        contains=[],
        evidence={"calls": 3},
        notes=(
            "Three tiny uncached calls with nothing marked for caching and too little "
            "repeated content to matter. Negative fixture — in particular, nothing is "
            "marked, so sub_minimum must not fire."
        ),
        calls=calls,
    )


def fx_openai_healthy() -> Fixture:
    sys_b = Block("system", "You are the fixture agent. " + filler("fixture ops", 1500))
    calls = [Call((sys_b, _convo(i)), "gpt-5.6", i * 20, 2100, 150) for i in range(5)]
    return Fixture(
        name="openai_healthy_cached",
        schema="otel-genai-current",
        provider_shape="openai",
        contains=[],
        evidence={"calls": 5},
        notes=(
            "OpenAI-shaped session with automatic prefix caching working as intended. "
            "Negative fixture; also exercises the nested accounting shape, where the "
            "cached count is a detail inside the input total and there is no billed "
            "cache-write figure."
        ),
        calls=calls,
    )


_REPORTS = "Shared reports\n" + notes("market", 1400) + "\n" + notes("news", 1200)


def _role(agent: str) -> str:
    return f"You are the {agent}.\n" + notes(f"{agent} instructions", 150)


def fx_misordered_shared_context() -> Fixture:
    calls = [
        Call(
            (Block("user", _role(agent) + "\n\n" + _REPORTS + "\n\nGive your view."),),
            "gpt-5.6",
            i * 40,
            3200,
            220,
        )
        for i, agent in enumerate(["bull analyst", "bear analyst", "research judge"])
    ]
    return Fixture(
        name="misordered_shared_context",
        schema="otel-genai-current",
        provider_shape="openai",
        contains=["misordered_shared_context"],
        evidence={"agents": 3, "shared_tokens": tokens(_REPORTS), "affected_calls": 2},
        notes=(
            "Three agents get the same ~2,600 tokens of reports in one message, each "
            "behind its own role text. The prefix differs from the first line, so none "
            "of the shared reports is ever read from cache."
        ),
        calls=calls,
    )


def fx_shared_context_first() -> Fixture:
    calls = [
        Call(
            (Block("user", _REPORTS), Block("user", _role(agent) + "\n\nGive your view.")),
            "gpt-5.6",
            i * 40,
            3200,
            220,
        )
        for i, agent in enumerate(["bull analyst", "bear analyst", "research judge"])
    ]
    return Fixture(
        name="shared_context_first",
        schema="otel-genai-current",
        provider_shape="openai",
        contains=[],
        evidence={"agents": 3, "shared_tokens": tokens(_REPORTS)},
        notes=(
            "The same three agents with the reports first and the role text after them. "
            "The reports are a common prefix and are read from cache. Negative fixture "
            "for misordered_shared_context."
        ),
        calls=calls,
    )


def fx_shared_context_small() -> Fixture:
    footer = notes("disclaimer", 300)
    calls = [
        Call(
            (Block("user", _role(agent) + "\n\n" + footer),),
            "gpt-5.6",
            i * 40,
            1400,
            150,
        )
        for i, agent in enumerate(["bull analyst", "bear analyst", "research judge"])
    ]
    return Fixture(
        name="shared_context_small",
        schema="otel-genai-current",
        provider_shape="openai",
        contains=[],
        evidence={"agents": 3, "shared_tokens": tokens(footer)},
        notes=(
            "Agents share a ~300-token footer behind their role text: below any cacheable "
            "minimum, so reordering would save nothing. Near-miss negative for "
            "misordered_shared_context."
        ),
        calls=calls,
    )


_POLICY = notes("support policy", 1500)


def _support_sessions(system_for: dict[str, str], user_prefix: dict[str, str]) -> list[Call]:
    calls = []
    for s, account in enumerate(system_for):
        system = Block("system", system_for[account])
        for k in range(3):
            turn = Block("user", f"{user_prefix[account]}Question {k} about the last invoice.")
            calls.append(
                Call((system, turn), "gpt-5.6", s * 60 + k * 20, 1900, 160, f"session-{account}")
            )
    return calls


def fx_cross_session_drift() -> Fixture:
    accounts = ["acme", "globex", "initech", "umbrella"]
    system_for = {
        a: f"You are the support agent for {a}. Today is 2026-01-{15 + i:02d}.\n" + _POLICY
        for i, a in enumerate(accounts)
    }
    return Fixture(
        name="cross_session_drift",
        schema="otel-genai-current",
        provider_shape="openai",
        contains=["cross_session_drift"],
        evidence={"sessions": 4, "volatile_line": 0, "shared_tail_tokens": tokens(_POLICY)},
        notes=(
            "Four sessions, a minute apart, use one system prompt whose first line holds "
            "the account name and date. Each session caches its own prompt, but the "
            "~1,500 identical tokens after that line are never shared across sessions."
        ),
        calls=_support_sessions(system_for, {a: "" for a in accounts}),
    )


def fx_cross_session_stable() -> Fixture:
    accounts = ["acme", "globex", "initech", "umbrella"]
    system = "You are the support agent.\n" + _POLICY
    return Fixture(
        name="cross_session_stable",
        schema="otel-genai-current",
        provider_shape="openai",
        contains=[],
        evidence={"sessions": 4},
        notes=(
            "The same four sessions with the account and date moved into the user "
            "message. The system prompt is identical everywhere and is read from cache "
            "across sessions. Negative fixture for cross_session_drift."
        ),
        calls=_support_sessions(
            {a: system for a in accounts},
            {a: f"Account {a}, 2026-01-{15 + i:02d}. " for i, a in enumerate(accounts)},
        ),
    )


def fx_cross_session_distinct() -> Fixture:
    agents = ["billing", "shipping", "returns"]
    system_for = {
        a: f"You are the {a} agent.\n" + notes(f"{a} policy", 1500) + "\nAnswer concisely."
        for a in agents
    }
    return Fixture(
        name="cross_session_distinct",
        schema="otel-genai-current",
        provider_shape="openai",
        contains=[],
        evidence={"sessions": 3},
        notes=(
            "Three sessions run three different agents whose long system prompts share "
            "only their last line. Different prompts, not one prompt with a changing "
            "value: near-miss negative for cross_session_drift."
        ),
        calls=_support_sessions(system_for, {a: "" for a in agents}),
    )


def build_fixtures() -> list[Fixture]:
    fixtures = [
        fx_prefix_drift(),
        fx_sub_minimum(),
        fx_model_switch(),
        fx_ttl_expiry(),
        fx_breakpoint_limit(),
        fx_lookback_overflow(),
        fx_tool_schema_churn(),
        fx_context_bloat(),
        fx_retry_storm(),
        fx_healthy_cached(),
        fx_healthy_incremental(),
        fx_healthy_small(),
        fx_openai_healthy(),
        fx_misordered_shared_context(),
        fx_shared_context_first(),
        fx_shared_context_small(),
        fx_cross_session_drift(),
        fx_cross_session_stable(),
        fx_cross_session_distinct(),
    ]

    # Schema variants of prefix_drift: same session, older/other trace dialects,
    # so the normalizer can be tested to produce identical findings for all three.
    legacy = fx_prefix_drift()
    legacy.name = "prefix_drift_legacy_flat"
    legacy.schema = "otel-genai-legacy-flat"
    legacy.notes = (
        "Same session as prefix_drift, emitted with the legacy flat attribute names "
        "(gen_ai.usage.cache_read_input_tokens) and deprecated prompt_tokens, which "
        "here EXCLUDES cache tokens - the passthrough accounting trap."
    )
    fixtures.append(legacy)

    oi = fx_prefix_drift()
    oi.name = "prefix_drift_openinference"
    oi.schema = "openinference"
    oi.notes = (
        "Same session as prefix_drift, emitted as OpenInference llm.* attributes "
        "with flattened messages and nested prompt_details cache counts."
    )
    fixtures.append(oi)

    nc = fx_prefix_drift()
    nc.name = "prefix_drift_no_content"
    nc.content_captured = False
    nc.notes = (
        "Same session as prefix_drift with message content capture disabled (the "
        "OTel default). Usage numbers show the cache never being reused; without "
        "content, a tool can prove waste but not the drifting block, and must say "
        "so rather than guess."
    )
    fixtures.append(nc)

    return fixtures


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #


def render(fx: Fixture) -> tuple[dict[str, Any], dict[str, Any]]:
    sim: AnthropicCacheSim | OpenAICacheSim
    sim = OpenAICacheSim() if fx.provider_shape == "openai" else AnthropicCacheSim()
    usages = [sim.simulate(c) for c in fx.calls]

    if fx.schema == "otel-genai-current":
        trace = emit_current(fx.name, fx.provider_shape, fx.calls, usages, fx.content_captured)
    elif fx.schema == "otel-genai-legacy-flat":
        trace = emit_legacy(fx.name, fx.provider_shape, fx.calls, usages)
    elif fx.schema == "openinference":
        trace = emit_openinference(fx.name, fx.provider_shape, fx.calls, usages)
    else:  # pragma: no cover - guarded by tests
        raise ValueError(f"unknown schema {fx.schema}")

    label = {
        "fixture": fx.name,
        "schema": fx.schema,
        "provider_shape": fx.provider_shape,
        "models": sorted({c.model for c in fx.calls}),
        "content_captured": fx.content_captured,
        "contains": sorted(fx.contains),
        "must_not_contain": sorted(m for m in FAILURE_MODES if m not in fx.contains),
        "evidence": fx.evidence,
        "notes": fx.notes,
        "totals": {
            "calls": len(fx.calls),
            "input_tokens": sum(u.total_input for u in usages),
            "cache_read_tokens": sum(u.cache_read for u in usages),
            "cache_creation_tokens": sum(u.cache_creation for u in usages),
            "output_tokens": sum(u.output for u in usages),
        },
        **fx.extra_labels,
    }
    return trace, label


def write_corpus(out_dir: Path) -> list[str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    names: list[str] = []
    manifest: list[dict[str, Any]] = []
    for fx in build_fixtures():
        trace, label = render(fx)
        (out_dir / f"{fx.name}.trace.json").write_text(
            json.dumps(trace, indent=2) + "\n", encoding="utf-8"
        )
        (out_dir / f"{fx.name}.label.json").write_text(
            json.dumps(label, indent=2) + "\n", encoding="utf-8"
        )
        names.append(fx.name)
        manifest.append(
            {
                "fixture": fx.name,
                "schema": fx.schema,
                "provider_shape": fx.provider_shape,
                "contains": sorted(fx.contains),
                "calls": len(fx.calls),
            }
        )
    (out_dir / "manifest.json").write_text(
        json.dumps({"generator": SCOPE, "fixtures": manifest}, indent=2) + "\n",
        encoding="utf-8",
    )
    return names


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).parent / "fixtures",
        help="output directory (default: corpus/synthetic/fixtures)",
    )
    args = parser.parse_args()
    names = write_corpus(args.out)
    print(f"wrote {len(names)} fixtures to {args.out}")


if __name__ == "__main__":
    main()
