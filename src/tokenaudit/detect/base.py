"""Detector contract and shared helpers.

A detector is a pure function of ``(Session, Rulebook)``. It must do one of
three things, and the distinction is part of the product:

- **abstain** with a reason when it cannot examine the session (no content
  captured, model unknown to the rulebook, too few requests) — reported as
  "not checked", never as "clean";
- **run and find nothing** — reported as checked-and-clean;
- **run and emit findings**, each carrying evidence a stranger could verify
  against the raw trace.

Token estimates from block lengths use the same ~4-chars-per-token stand-in
as the corpus simulator, and every impact that relies on it says so in its
assumption string.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from itertools import accumulate, pairwise
from typing import ClassVar

from tokenaudit.detect.findings import Finding
from tokenaudit.model.records import BlockDigest, Request, Segment, Session
from tokenaudit.rulebook import Rulebook

CHARS_PER_TOKEN = 4
TOKEN_ESTIMATE_NOTE = "block token counts estimated from lengths at ~4 chars/token"


class Abstain(Exception):
    """Raised by a detector that cannot examine this session; carries the reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Detector(ABC):
    detector_id: ClassVar[str]

    @abstractmethod
    def check(self, session: Session, rulebook: Rulebook) -> list[Finding]:
        """Return findings for this session, or raise :class:`Abstain`."""


class CrossSessionDetector(ABC):
    """Same contract as :class:`Detector`, over all sessions at once."""

    detector_id: ClassVar[str]

    @abstractmethod
    def check_sessions(self, sessions: Sequence[Session], rulebook: Rulebook) -> list[Finding]:
        """Return findings across these sessions, or raise :class:`Abstain`."""


def est_tokens(chars: int) -> int:
    return max(1, chars // CHARS_PER_TOKEN)


def blocks_tokens(blocks: tuple[BlockDigest, ...]) -> int:
    return sum(est_tokens(b.chars) for b in blocks)


def block_segments(block: BlockDigest) -> tuple[Segment, ...]:
    """A block's line digests; one whole-block segment when none were kept."""
    if block.segments:
        return block.segments
    return (Segment(block.sha256[:16], block.chars),) if block.chars else ()


def request_segments(request: Request) -> tuple[Segment, ...]:
    return tuple(s for b in request.blocks or () for s in block_segments(b))


def offsets(segments: Sequence[Segment]) -> list[int]:
    """Char offset at which each segment starts."""
    return [0, *accumulate(s.chars for s in segments)][:-1] if segments else []


def common_prefix_chars(a: Sequence[Segment], b: Sequence[Segment]) -> int:
    total = 0
    for x, y in zip(a, b, strict=False):
        if x.sha256 != y.sha256:
            break
        total += x.chars
    return total


@dataclass(frozen=True)
class SharedRun:
    """The longest stretch of consecutive lines two prompts have in common."""

    start_a: int  # char offset in the first prompt
    start_b: int  # char offset in the second prompt
    chars: int
    index_a: int  # segment index in the first prompt
    length: int  # segments


def longest_shared_run(a: Sequence[Segment], b: Sequence[Segment]) -> SharedRun | None:
    matcher = SequenceMatcher(None, [s.sha256 for s in a], [s.sha256 for s in b], autojunk=False)
    off_a, off_b = offsets(a), offsets(b)
    best: SharedRun | None = None
    for match in matcher.get_matching_blocks():
        if match.size == 0:
            continue
        chars = sum(s.chars for s in a[match.a : match.a + match.size])
        if best is None or chars > best.chars:
            best = SharedRun(off_a[match.a], off_b[match.b], chars, match.a, match.size)
    return best


def last_marked_index(blocks: tuple[BlockDigest, ...]) -> int | None:
    marked = [i for i, b in enumerate(blocks) if b.cache_marker]
    return marked[-1] if marked else None


@dataclass(frozen=True)
class Pair:
    """Two consecutive same-model requests that both captured content."""

    first: Request
    second: Request

    @property
    def first_divergence(self) -> int | None:
        a = self.first.blocks or ()
        b = self.second.blocks or ()
        for i in range(min(len(a), len(b))):
            if a[i].sha256 != b[i].sha256:
                return i
        return None


def consecutive_pairs(session: Session) -> list[Pair]:
    """Adjacent same-model request pairs with content on both sides."""
    pairs: list[Pair] = []
    requests = session.requests
    for first, second in pairwise(requests):
        if first.blocks and second.blocks and first.model == second.model:
            pairs.append(Pair(first, second))
    return pairs


def require_content(session: Session, minimum_requests: int = 1) -> list[Request]:
    with_content = [r for r in session.requests if r.blocks]
    if len(with_content) < minimum_requests:
        raise Abstain(
            f"needs at least {minimum_requests} request(s) with captured message content; "
            "usage numbers alone can prove waste but not its cause"
        )
    return with_content
