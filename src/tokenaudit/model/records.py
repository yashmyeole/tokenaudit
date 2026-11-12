"""Normalized trace records: the one shape every dialect is read into.

Canonical token accounting is **nested**: ``TokenUsage.input_total`` includes
cache-read and cache-creation tokens, matching the current OTel GenAI
semantic conventions and OpenAI's usage shape. Dialects that report
exclusive counts (Anthropic raw SDK fields, legacy flat passthrough) are
converted at ingest and flagged.

``None`` and ``0`` are different facts everywhere in this module: ``None``
means the trace did not report the field; ``0`` means the trace reported
zero. Collapsing the two is how observability tools lie, so nothing here
ever defaults a missing count to zero.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Segment:
    """One line of a block: truncated hash and length (newline included)."""

    sha256: str
    chars: int


@dataclass(frozen=True)
class BlockDigest:
    """Privacy-preserving digest of one prompt block: hash + length, no text.

    ``segments`` holds per-line digests so detectors can see shared content
    inside blocks that differ as a whole. They stay in memory and are left
    out of anything the CLI prints: short lines are guessable from a hash.
    """

    role: str
    sha256: str
    chars: int
    cache_marker: bool = False
    segments: tuple[Segment, ...] = field(default=(), compare=False, repr=False)


@dataclass(frozen=True)
class ToolDef:
    """One tool definition sent with a request: its name and schema length."""

    name: str
    chars: int


@dataclass(frozen=True)
class TokenUsage:
    """Billing outcome of one request, in canonical nested accounting."""

    input_total: int | None
    cache_read: int | None
    cache_creation: int | None
    output: int | None
    reasoning_output: int | None = None

    @property
    def uncached_input(self) -> int | None:
        """Tokens billed at the full input rate, when computable."""
        if self.input_total is None:
            return None
        return self.input_total - (self.cache_read or 0) - (self.cache_creation or 0)


@dataclass(frozen=True)
class Request:
    """One LLM request, normalized from any supported trace dialect."""

    span_id: str
    trace_id: str
    parent_id: str | None
    name: str
    schema: str
    provider: str | None
    requested_model: str | None
    response_model: str | None
    start_unix_nano: int
    end_unix_nano: int
    usage: TokenUsage
    blocks: tuple[BlockDigest, ...] | None
    tool_schema_sha256: str | None
    params_sha256: str | None
    conversation_id: str | None
    source: str
    quality: tuple[str, ...] = ()
    unpriceable: bool = False
    tool_defs: tuple[ToolDef, ...] | None = None  # None: definitions not captured
    tool_calls: tuple[str, ...] | None = None  # tools the response called; None: unknown

    @property
    def model(self) -> str | None:
        """Best-known model id: the resolved one when reported, else requested."""
        return self.response_model or self.requested_model


@dataclass(frozen=True)
class Session:
    """Time-ordered requests that share a conversation."""

    session_id: str
    grouping: str  # "conversation_id" | "file"
    requests: tuple[Request, ...]


@dataclass(frozen=True)
class IngestResult:
    """Everything read from one trace source."""

    source: str
    container: str  # "otlp-json" | "otel-sdk-jsonl" | "provider-log"
    sessions: tuple[Session, ...]
    spans_total: int
    spans_skipped: int
    deduped_span_ids: tuple[str, ...]

    @property
    def requests(self) -> tuple[Request, ...]:
        return tuple(r for s in self.sessions for r in s.requests)
