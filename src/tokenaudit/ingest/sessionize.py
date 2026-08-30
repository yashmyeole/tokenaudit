"""Group requests into sessions.

Preference order, documented because it will be questioned:

1. An explicit conversation id (``gen_ai.conversation.id`` / ``session.id``)
   groups exactly and is trusted when present.
2. Otherwise the **source file** is one session. Grouping by trace id is
   deliberately not attempted: in real captures every framework invocation
   starts its own trace (the TradingAgents runs hold 17 trace ids for one
   agent session), so trace-id grouping would shatter one conversation into
   fragments and hide cross-call cache behavior — the very thing being
   diagnosed.
"""

from __future__ import annotations

from pathlib import Path

from tokenaudit.model.records import Request, Session


def sessionize(requests: list[Request], source: str) -> list[Session]:
    by_conversation: dict[str, list[Request]] = {}
    unassigned: list[Request] = []
    for request in requests:
        if request.conversation_id:
            by_conversation.setdefault(request.conversation_id, []).append(request)
        else:
            unassigned.append(request)

    sessions = [
        Session(
            session_id=conversation_id,
            grouping="conversation_id",
            requests=tuple(sorted(group, key=lambda r: (r.start_unix_nano, r.span_id))),
        )
        for conversation_id, group in sorted(by_conversation.items())
    ]
    if unassigned:
        sessions.append(
            Session(
                session_id=f"file:{Path(source).name}",
                grouping="file",
                requests=tuple(sorted(unassigned, key=lambda r: (r.start_unix_nano, r.span_id))),
            )
        )
    return sessions
