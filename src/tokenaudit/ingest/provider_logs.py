"""Raw provider logs: API responses as logged, with or without their requests.

One record per JSONL line, or per element of a JSON array. A record is either
a bare response (``model`` and ``usage`` are enough) or
``{"request": ..., "response": ..., "timestamp": ..., "session_id": ...}``,
where everything but ``response`` is optional.

Usage shapes:

- OpenAI Responses: ``input_tokens`` with ``input_tokens_details``
  (``cached_tokens``, ``cache_write_tokens``), counted inside the input;
- OpenAI Chat Completions: ``prompt_tokens`` with ``prompt_tokens_details``;
- Anthropic Messages: ``input_tokens`` excludes ``cache_read_input_tokens``
  and ``cache_creation_input_tokens``. Converted to nested and flagged.

Records without a timestamp keep their order and are flagged, so detectors
that reason about time abstain instead of guessing.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from tokenaudit.ingest.containers import UnknownTraceFormatError
from tokenaudit.ingest.dialects import block_digest, tool_defs
from tokenaudit.model.records import BlockDigest, Request, TokenUsage

FLAG_EXCLUSIVE_CONVERTED = "exclusive_counts_converted"
FLAG_NO_TIMESTAMP = "no_timestamp"


def looks_like_provider_log(text: str) -> bool:
    stripped = text.lstrip()
    if stripped.startswith("["):
        try:
            records = json.loads(stripped)
        except json.JSONDecodeError:
            return False
        return bool(records) and all(_is_record(r) for r in records[:3])
    if stripped.startswith("{"):
        first = stripped.split("\n", 1)[0]
        try:
            return _is_record(json.loads(first))
        except json.JSONDecodeError:
            return False
    return False


def _is_record(value: Any) -> bool:
    if not isinstance(value, dict) or "resourceSpans" in value or "context" in value:
        return False
    response = value.get("response", value)
    return isinstance(response, dict) and isinstance(response.get("usage"), dict)


def _records(text: str, source: str) -> list[dict[str, Any]]:
    stripped = text.lstrip()
    try:
        if stripped.startswith("["):
            records: list[dict[str, Any]] = json.loads(stripped)
        else:
            records = [json.loads(line) for line in text.splitlines() if line.strip()]
    except json.JSONDecodeError as exc:
        raise UnknownTraceFormatError(f"{source}: invalid provider log ({exc})") from exc
    for n, record in enumerate(records):
        if not _is_record(record):
            raise UnknownTraceFormatError(f"{source}: record {n} has no response usage")
    return records


def _kind(usage: dict[str, Any], response: dict[str, Any], model: str, hint: Any) -> str:
    if hint in ("openai", "anthropic"):
        if hint == "anthropic":
            return "anthropic"
        return "openai-chat" if "prompt_tokens" in usage else "openai-responses"
    if "prompt_tokens" in usage:
        return "openai-chat"
    if (
        "cache_read_input_tokens" in usage
        or "cache_creation_input_tokens" in usage
        or response.get("type") == "message"
        or model.startswith("claude")
    ):
        return "anthropic"
    return "openai-responses"


def _int(value: Any) -> int | None:
    return int(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _usage(kind: str, usage: dict[str, Any]) -> tuple[TokenUsage, list[str]]:
    if kind == "anthropic":
        base = _int(usage.get("input_tokens"))
        read = _int(usage.get("cache_read_input_tokens"))
        write = _int(usage.get("cache_creation_input_tokens"))
        total = None if base is None else base + (read or 0) + (write or 0)
        return TokenUsage(total, read, write, _int(usage.get("output_tokens"))), [
            FLAG_EXCLUSIVE_CONVERTED
        ]
    if kind == "openai-chat":
        details = usage.get("prompt_tokens_details") or {}
        out_details = usage.get("completion_tokens_details") or {}
        return TokenUsage(
            _int(usage.get("prompt_tokens")),
            _int(details.get("cached_tokens")),
            _int(details.get("cache_write_tokens")),
            _int(usage.get("completion_tokens")),
            _int(out_details.get("reasoning_tokens")),
        ), []
    details = usage.get("input_tokens_details") or {}
    out_details = usage.get("output_tokens_details") or {}
    return TokenUsage(
        _int(usage.get("input_tokens")),
        _int(details.get("cached_tokens")),
        _int(details.get("cache_write_tokens")),
        _int(usage.get("output_tokens")),
        _int(out_details.get("reasoning_tokens")),
    ), []


def _text(content: Any) -> tuple[str, bool]:
    """Flatten message content to text; also report whether any part is marked."""
    if content is None:
        return "", False
    if isinstance(content, str):
        return content, False
    if isinstance(content, dict):
        content = [content]
    if not isinstance(content, list):
        return json.dumps(content, sort_keys=True), False
    texts, marked = [], False
    for part in content:
        if not isinstance(part, dict):
            texts.append(str(part))
            continue
        marked = marked or "cache_control" in part
        if isinstance(part.get("text"), str):
            texts.append(part["text"])
        elif part.get("type") == "tool_result":
            texts.append(_text(part.get("content"))[0])
        else:
            texts.append(json.dumps(part, sort_keys=True, separators=(",", ":")))
    return "".join(texts), marked


def _blocks(kind: str, request: dict[str, Any]) -> tuple[BlockDigest, ...] | None:
    blocks: list[BlockDigest] = []
    if kind == "anthropic":
        if request.get("system"):
            text, marked = _text(request["system"])
            blocks.append(block_digest("system", text, marked))
        messages = request.get("messages")
    elif kind == "openai-chat":
        messages = request.get("messages")
    else:
        if request.get("instructions"):
            blocks.append(block_digest("system", str(request["instructions"])))
        items = request.get("input")
        messages = [{"role": "user", "content": items}] if isinstance(items, str) else items
    if not isinstance(messages, list):
        return tuple(blocks) or None
    for item in messages:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "function_call":
            role, text = "assistant", f"{item.get('name')}({item.get('arguments', '')})"
            marked = False
        elif item.get("type") == "function_call_output":
            role, (text, marked) = "tool", _text(item.get("output"))
        else:
            role = str(item.get("role", ""))
            text, marked = _text(item.get("content"))
            if not text and item.get("tool_calls"):
                text = json.dumps(item["tool_calls"], sort_keys=True, separators=(",", ":"))
        blocks.append(block_digest(role, text, marked))
    return tuple(blocks) or None


def _response_calls(response: dict[str, Any]) -> tuple[str, ...] | None:
    """Names of tools the response called, or None when the output wasn't logged."""
    if isinstance(response.get("output"), list):  # OpenAI Responses
        return tuple(
            str(item.get("name"))
            for item in response["output"]
            if isinstance(item, dict) and item.get("type") == "function_call"
        )
    if isinstance(response.get("choices"), list):  # OpenAI Chat Completions
        calls = []
        for choice in response["choices"]:
            message = choice.get("message", {}) if isinstance(choice, dict) else {}
            for call in message.get("tool_calls") or []:
                calls.append(str((call.get("function") or {}).get("name")))
        return tuple(calls)
    if isinstance(response.get("content"), list):  # Anthropic Messages
        return tuple(
            str(block.get("name"))
            for block in response["content"]
            if isinstance(block, dict) and block.get("type") == "tool_use"
        )
    return None


def _timestamp_ns(record: dict[str, Any], response: dict[str, Any]) -> int | None:
    value = record.get("timestamp", response.get("created_at", response.get("created")))
    if isinstance(value, int | float) and not isinstance(value, bool):
        return round(value * 1_000_000_000)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return round(parsed.timestamp() * 1_000_000_000)
    return None


def load_provider_log(text: str, source: str) -> list[Request]:
    trace_id = hashlib.sha256(source.encode()).hexdigest()[:32]
    requests = []
    for n, record in enumerate(_records(text, source)):
        response = record.get("response", record)
        body = record.get("request") if isinstance(record.get("request"), dict) else None
        model = str(response.get("model") or (body or {}).get("model") or "")
        kind = _kind(response["usage"], response, model, record.get("provider"))
        usage, quality = _usage(kind, response["usage"])

        start = _timestamp_ns(record, response)
        if start is None:
            start = n
            quality.append(FLAG_NO_TIMESTAMP)
        duration = _int(record.get("duration_ms"))
        tools = body.get("tools") if body else None
        session = record.get("session_id", record.get("conversation_id"))

        requests.append(
            Request(
                span_id=str(response.get("id") or f"{n:016x}"),
                trace_id=trace_id,
                parent_id=None,
                name=f"{kind} {model}",
                schema=f"provider-{kind}",
                provider="anthropic" if kind == "anthropic" else "openai",
                requested_model=str(body["model"]) if body and body.get("model") else None,
                response_model=model or None,
                start_unix_nano=start,
                end_unix_nano=start + (duration or 0) * 1_000_000,
                usage=usage,
                blocks=_blocks(kind, body) if body else None,
                tool_schema_sha256=(
                    hashlib.sha256(
                        json.dumps(tools, sort_keys=True, separators=(",", ":")).encode()
                    ).hexdigest()
                    if tools
                    else None
                ),
                params_sha256=None,
                conversation_id=str(session) if session is not None else None,
                source=source,
                quality=tuple(quality),
                tool_defs=tool_defs(tools) if tools else None,
                tool_calls=_response_calls(response),
            )
        )
    return requests
