"""Dialect extraction: raw span attributes -> a normalized :class:`Request`.

Four real dialects, all present in this repo's corpus (sources verified
2026-08-29; see ``corpus/README.md``):

- ``otel-genai-current``  — dotted cache names; ``input_tokens`` already
  includes cache tokens (nested), so it maps straight through.
- ``otel-genai-legacy-flat`` — older openllmetry names; ``prompt_tokens``
  passes the provider field through, which for Anthropic EXCLUDES cache
  tokens. Normalization adds the cache fields back in and flags the
  assumption, because the trace itself cannot prove which convention the
  emitter used.
- ``openinference`` — ``llm.*`` attributes; ``llm.token_count.prompt`` is
  nested; cache details may be absent entirely (absent != zero, preserved).
- Spans with no recognized token accounting are not requests; the caller
  counts them as skipped.

Prompt content is never stored: blocks become sha256(role + NUL + text)
digests plus lengths, matching the corpus generator's hashing scheme so
synthetic ground truth and ingested output can be compared hash-for-hash.
"""

from __future__ import annotations

import hashlib
import json
import re

from tokenaudit.ingest.containers import AttrValue, RawSpan
from tokenaudit.model.records import BlockDigest, Request, Segment, TokenUsage, ToolDef

FLAG_LEGACY_EXCLUSIVE_ASSUMED = "legacy_exclusive_assumed"
FLAG_PROVIDER_ATTR_DEPRECATED = "provider_attr_deprecated"

_MSG_INDEX = re.compile(r"^llm\.input_messages\.(\d+)\.message\.(role|content)$")
_LEGACY_INDEX = re.compile(r"^gen_ai\.prompt\.(\d+)\.(role|content)$")
_TOOL_INDEX = re.compile(r"^llm\.tools\.(\d+)\.tool\.json_schema$")
_OUT_TOOL_CALL = re.compile(
    r"^llm\.output_messages\.\d+\.message\.tool_calls\.\d+\.tool_call\.function\.name$"
)
_OUT_MESSAGE = re.compile(r"^llm\.output_messages\.\d+\.message\.")


def _digest(role: str, text: str) -> str:
    return hashlib.sha256(f"{role}\x00{text}".encode()).hexdigest()


def _segments(role: str, text: str) -> tuple[Segment, ...]:
    lines = text.split("\n")
    out = []
    for i, line in enumerate(lines):
        chars = len(line) + (1 if i < len(lines) - 1 else 0)
        if chars:
            out.append(Segment(_digest(role, line)[:16], chars))
    return tuple(out)


def tool_name(definition: object) -> str | None:
    """Name of a tool definition in any common shape (OpenAI, Anthropic, flat)."""
    if not isinstance(definition, dict):
        return None
    name = definition.get("name")
    function = definition.get("function")
    if name is None and isinstance(function, dict):
        name = function.get("name")
    return str(name) if name is not None else None


def tool_defs(definitions: object) -> tuple[ToolDef, ...] | None:
    if not isinstance(definitions, list):
        return None
    out = []
    for definition in definitions:
        text = json.dumps(definition, sort_keys=True, separators=(",", ":"))
        out.append(ToolDef(tool_name(definition) or "?", len(text)))
    return tuple(out)


def block_digest(role: str, text: str, cache_marker: bool = False) -> BlockDigest:
    return BlockDigest(
        role=role,
        sha256=_digest(role, text),
        chars=len(text),
        cache_marker=cache_marker,
        segments=_segments(role, text),
    )


def _int_or_none(attrs: dict[str, AttrValue], key: str) -> int | None:
    value = attrs.get(key)
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return int(value)
    return None


def _str_or_none(attrs: dict[str, AttrValue], key: str) -> str | None:
    value = attrs.get(key)
    return str(value) if value is not None else None


def _sha_or_none(text: str | None) -> str | None:
    return hashlib.sha256(text.encode()).hexdigest() if text else None


def extract(raw: RawSpan, source: str) -> Request | None:
    """Interpret one raw span; ``None`` when it carries no token accounting."""
    attrs = raw.attributes
    if "llm.token_count.prompt" in attrs:
        return _extract_openinference(raw, source)
    if "gen_ai.usage.input_tokens" in attrs:
        return _extract_current(raw, source)
    if "gen_ai.usage.prompt_tokens" in attrs:
        return _extract_legacy(raw, source)
    return None


# --------------------------------------------------------------------------- #
# Current OTel GenAI semconv
# --------------------------------------------------------------------------- #


def _blocks_from_structured(messages_json: str) -> tuple[BlockDigest, ...]:
    blocks: list[BlockDigest] = []
    for message in json.loads(messages_json):
        role = str(message.get("role", ""))
        parts = message.get("parts", [])
        text = "".join(str(p.get("content", "")) for p in parts if isinstance(p, dict))
        marker = any(isinstance(p, dict) and "cache_control" in p for p in parts)
        blocks.append(block_digest(role, text, marker))
    return tuple(blocks)


def _extract_current(raw: RawSpan, source: str) -> Request:
    attrs = raw.attributes
    quality: list[str] = []
    provider = _str_or_none(attrs, "gen_ai.provider.name")
    if provider is None:
        provider = _str_or_none(attrs, "gen_ai.system")
        if provider is not None:
            quality.append(FLAG_PROVIDER_ATTR_DEPRECATED)

    blocks: tuple[BlockDigest, ...] | None = None
    messages = _str_or_none(attrs, "gen_ai.input.messages")
    if messages:
        blocks = _blocks_from_structured(messages)

    usage = TokenUsage(
        input_total=_int_or_none(attrs, "gen_ai.usage.input_tokens"),
        cache_read=_int_or_none(attrs, "gen_ai.usage.cache_read.input_tokens"),
        cache_creation=_int_or_none(attrs, "gen_ai.usage.cache_creation.input_tokens"),
        output=_int_or_none(attrs, "gen_ai.usage.output_tokens"),
    )
    return Request(
        span_id=raw.span_id,
        trace_id=raw.trace_id,
        parent_id=raw.parent_id,
        name=raw.name,
        schema="otel-genai-current",
        provider=provider,
        requested_model=_str_or_none(attrs, "gen_ai.request.model"),
        response_model=_str_or_none(attrs, "gen_ai.response.model"),
        start_unix_nano=raw.start_unix_nano,
        end_unix_nano=raw.end_unix_nano,
        usage=usage,
        blocks=blocks,
        tool_schema_sha256=None,
        params_sha256=None,
        conversation_id=_str_or_none(attrs, "gen_ai.conversation.id"),
        source=source,
        quality=tuple(quality),
        tool_defs=_json_tool_defs(_str_or_none(attrs, "gen_ai.tool.definitions")),
        tool_calls=_structured_calls(_str_or_none(attrs, "gen_ai.output.messages")),
    )


def _json_tool_defs(value: str | None) -> tuple[ToolDef, ...] | None:
    if not value:
        return None
    try:
        return tool_defs(json.loads(value))
    except json.JSONDecodeError:
        return None


def _structured_calls(value: str | None) -> tuple[str, ...] | None:
    if not value:
        return None
    try:
        messages = json.loads(value)
    except json.JSONDecodeError:
        return None
    calls = []
    for message in messages if isinstance(messages, list) else []:
        for part in message.get("parts", []) if isinstance(message, dict) else []:
            if isinstance(part, dict) and part.get("type") == "tool_call" and part.get("name"):
                calls.append(str(part["name"]))
    return tuple(calls)


# --------------------------------------------------------------------------- #
# Legacy flat names (older openllmetry)
# --------------------------------------------------------------------------- #


def _blocks_from_indexed(
    attrs: dict[str, AttrValue], pattern: re.Pattern[str]
) -> tuple[BlockDigest, ...] | None:
    parts: dict[int, dict[str, str]] = {}
    for key, value in attrs.items():
        match = pattern.match(key)
        if match:
            parts.setdefault(int(match.group(1)), {})[match.group(2)] = str(value)
    if not parts:
        return None
    blocks = []
    for index in sorted(parts):
        role = parts[index].get("role", "")
        text = parts[index].get("content", "")
        blocks.append(block_digest(role, text))
    return tuple(blocks)


def _extract_legacy(raw: RawSpan, source: str) -> Request:
    attrs = raw.attributes
    prompt = _int_or_none(attrs, "gen_ai.usage.prompt_tokens")
    cache_read = _int_or_none(attrs, "gen_ai.usage.cache_read_input_tokens")
    cache_creation = _int_or_none(attrs, "gen_ai.usage.cache_creation_input_tokens")
    # Passthrough assumption: the deprecated prompt_tokens field carried the
    # provider's own count, which for Anthropic excludes cache tokens. The
    # trace cannot prove this, so the conversion is flagged, not silent.
    input_total = None if prompt is None else prompt + (cache_read or 0) + (cache_creation or 0)
    usage = TokenUsage(
        input_total=input_total,
        cache_read=cache_read,
        cache_creation=cache_creation,
        output=_int_or_none(attrs, "gen_ai.usage.completion_tokens"),
    )
    return Request(
        span_id=raw.span_id,
        trace_id=raw.trace_id,
        parent_id=raw.parent_id,
        name=raw.name,
        schema="otel-genai-legacy-flat",
        provider=_str_or_none(attrs, "gen_ai.system"),
        requested_model=_str_or_none(attrs, "gen_ai.request.model"),
        response_model=_str_or_none(attrs, "gen_ai.response.model"),
        start_unix_nano=raw.start_unix_nano,
        end_unix_nano=raw.end_unix_nano,
        usage=usage,
        blocks=_blocks_from_indexed(attrs, _LEGACY_INDEX),
        tool_schema_sha256=None,
        params_sha256=None,
        conversation_id=None,
        source=source,
        quality=(FLAG_LEGACY_EXCLUSIVE_ASSUMED,),
    )


# --------------------------------------------------------------------------- #
# OpenInference
# --------------------------------------------------------------------------- #


def _tool_schema_sha(attrs: dict[str, AttrValue]) -> str | None:
    tools: dict[int, str] = {}
    for key, value in attrs.items():
        match = _TOOL_INDEX.match(key)
        if match:
            tools[int(match.group(1))] = str(value)
    if not tools:
        return None
    # Order matters: reordering tool definitions invalidates provider caches,
    # so the digest is over the ordered concatenation, not a sorted set.
    joined = "\x00".join(tools[i] for i in sorted(tools))
    return hashlib.sha256(joined.encode()).hexdigest()


def _extract_openinference(raw: RawSpan, source: str) -> Request:
    attrs = raw.attributes
    usage = TokenUsage(
        input_total=_int_or_none(attrs, "llm.token_count.prompt"),
        cache_read=_int_or_none(attrs, "llm.token_count.prompt_details.cache_read"),
        cache_creation=_int_or_none(attrs, "llm.token_count.prompt_details.cache_write"),
        output=_int_or_none(attrs, "llm.token_count.completion"),
        reasoning_output=_int_or_none(attrs, "llm.token_count.completion_details.reasoning"),
    )
    return Request(
        span_id=raw.span_id,
        trace_id=raw.trace_id,
        parent_id=raw.parent_id,
        name=raw.name,
        schema="openinference",
        provider=_str_or_none(attrs, "llm.provider") or _str_or_none(attrs, "llm.system"),
        requested_model=_str_or_none(attrs, "llm.model_name"),
        response_model=None,
        start_unix_nano=raw.start_unix_nano,
        end_unix_nano=raw.end_unix_nano,
        usage=usage,
        blocks=_blocks_from_indexed(attrs, _MSG_INDEX),
        tool_schema_sha256=_tool_schema_sha(attrs),
        params_sha256=_sha_or_none(_str_or_none(attrs, "llm.invocation_parameters")),
        conversation_id=_str_or_none(attrs, "session.id"),
        source=source,
        quality=(),
        tool_defs=_openinference_tools(attrs),
        tool_calls=_openinference_calls(attrs),
    )


def _openinference_tools(attrs: dict[str, AttrValue]) -> tuple[ToolDef, ...] | None:
    schemas: dict[int, str] = {}
    for key, value in attrs.items():
        match = _TOOL_INDEX.match(key)
        if match:
            schemas[int(match.group(1))] = str(value)
    if not schemas:
        return None
    out = []
    for index in sorted(schemas):
        try:
            name = tool_name(json.loads(schemas[index]))
        except json.JSONDecodeError:
            name = None
        out.append(ToolDef(name or "?", len(schemas[index])))
    return tuple(out)


def _openinference_calls(attrs: dict[str, AttrValue]) -> tuple[str, ...] | None:
    if not any(_OUT_MESSAGE.match(key) for key in attrs):
        return None
    return tuple(str(v) for k, v in sorted(attrs.items()) if _OUT_TOOL_CALL.match(k))


def richness(request: Request) -> tuple[int, int]:
    """How much a request knows, for choosing between duplicate layers."""
    cache_known = int(request.usage.cache_read is not None) + int(
        request.usage.cache_creation is not None
    )
    fields = sum(
        1
        for value in (
            request.usage.reasoning_output,
            request.blocks,
            request.tool_schema_sha256,
            request.params_sha256,
            request.provider,
            request.requested_model,
        )
        if value is not None
    )
    return (cache_known, fields)
