"""Self-contained HTML report: one file, no external resources, works offline.

Everything is inline — styles included, no scripts, no CDN, no fonts — so
the file can be attached to a ticket or opened air-gapped, and nothing in
it phones home. Adapts to the viewer's light/dark preference. The waste bar
is deliberately minimal: the numbers sit beside it, color never carries
meaning alone.
"""

from __future__ import annotations

import html as html_escape
from importlib.metadata import version

from tokenaudit.counterfactual import format_usd
from tokenaudit.detect.findings import Finding, SessionDiagnosis
from tokenaudit.model.records import IngestResult, Session
from tokenaudit.report.corpus_record import describe
from tokenaudit.report.health import health_score, render_score
from tokenaudit.rulebook import Rulebook

_CSS = """
:root { color-scheme: light dark;
  --bg:#ffffff; --fg:#1e2229; --muted:#5b6472; --line:#d9dee6; --card:#f6f8fa;
  --high:#b3261e; --medium:#9a6a00; --low:#3f6212; --ok:#1a7f4b; }
@media (prefers-color-scheme: dark) { :root {
  --bg:#14171c; --fg:#e6e9ee; --muted:#9aa4b2; --line:#333a45; --card:#1c2129;
  --high:#ff6b61; --medium:#e0a93e; --low:#a3c76d; --ok:#4ccb8f; } }
body { background:var(--bg); color:var(--fg); margin:0 auto; max-width:56rem;
  padding:2rem 1.25rem; font:15px/1.55 system-ui, sans-serif; }
h1 { font-size:1.4rem; margin:0 0 .25rem; } h2 { font-size:1.05rem; margin:2rem 0 .5rem; }
.muted { color:var(--muted); } code { font-size:.85em; }
.kpis { display:flex; gap:2rem; flex-wrap:wrap; margin:1rem 0; }
.kpis div b { display:block; font-size:1.3rem; }
.bar { display:flex; height:14px; border-radius:7px; overflow:hidden;
  border:1px solid var(--line); margin:.35rem 0 .2rem; }
.bar span { display:block; height:100%; }
.finding { border:1px solid var(--line); border-left:4px solid var(--muted);
  border-radius:6px; background:var(--card); padding: .8rem 1rem; margin:.8rem 0; }
.finding.high { border-left-color:var(--high); }
.finding.medium { border-left-color:var(--medium); }
.finding.low { border-left-color:var(--low); }
.sev { font-weight:600; text-transform:uppercase; font-size:.72rem; letter-spacing:.06em; }
.finding.high .sev { color:var(--high); } .finding.medium .sev { color:var(--medium); }
table { border-collapse:collapse; margin:.5rem 0; font-size:.85rem; }
td { padding:.15rem .7rem .15rem 0; vertical-align:top; }
td:first-child { color:var(--muted); white-space:nowrap; }
footer { margin-top:2.5rem; border-top:1px solid var(--line); padding-top:.8rem;
  font-size:.8rem; color:var(--muted); }
ul { margin:.3rem 0; padding-left:1.2rem; }
"""


def _e(value: object) -> str:
    return html_escape.escape(str(value))


def _finding_html(finding: Finding) -> str:
    rows = "".join(
        f"<tr><td>{_e(key)}</td><td>{_e(value)}</td></tr>"
        for key, value in finding.evidence.items()
    )
    impact = ""
    if finding.impact:
        tokens = (
            f"~{finding.impact.tokens_low:,}"
            if finding.impact.tokens_low == finding.impact.tokens_high
            else f"~{finding.impact.tokens_low:,}&ndash;{finding.impact.tokens_high:,}"
        )
        usd = format_usd(finding.impact)
        cost = f" &middot; <b>{_e(usd)}</b>" if usd else ""
        impact = (
            f"<p><b>{tokens} tokens</b> full-price equivalent{cost} "
            f"<span class='muted'>({_e(finding.impact.assumption)})</span></p>"
        )
    record = describe(finding.detector_id)
    record_html = f"<p class='muted'>{_e(record)}</p>" if record else ""
    return (
        f"<div class='finding {finding.severity.value}'>"
        f"<span class='sev'>{finding.severity.value}</span> "
        f"<b>{_e(finding.detector_id)}</b>"
        f"<p>{_e(finding.summary)}</p>{impact}"
        f"<p><b>Fix:</b> {_e(finding.fix.summary)}</p>"
        f"<details><summary class='muted'>evidence</summary><table>{rows}</table></details>"
        f"{record_html}</div>"
    )


def _session_html(session: Session, diagnosis: SessionDiagnosis, rulebook: Rulebook) -> str:
    total_input = sum(r.usage.input_total or 0 for r in session.requests)
    total_read = sum(r.usage.cache_read or 0 for r in session.requests)
    hit = f"{total_read / total_input:.1%}" if total_input else "n/a"
    score = health_score(session, diagnosis, rulebook)
    total_written = sum(r.usage.cache_creation or 0 for r in session.requests)
    full_price = max(total_input - total_read - total_written, 0)
    read_pct = (total_read / total_input * 100) if total_input else 0
    write_pct = (total_written / total_input * 100) if total_input else 0
    written = f"{total_written:,} written to cache &middot; " if total_written else ""

    findings_html = "".join(_finding_html(f) for f in diagnosis.findings) or (
        "<p class='muted'>No findings.</p>"
    )
    fired = {f.detector_id for f in diagnosis.findings}
    clean = [d for d in diagnosis.checked if d not in fired]
    footer_bits = []
    if clean:
        footer_bits.append(f"checked, clean: {', '.join(clean)}")
    footer_bits += [f"not checked — {d}: {r}" for d, r in diagnosis.abstained.items()]
    checked_html = "".join(f"<li>{_e(bit)}</li>" for bit in footer_bits)
    notes_html = "".join(f"<li>{_e(note)}</li>" for note in score.notes)

    return (
        f"<h2>Session <code>{_e(diagnosis.session_id)}</code></h2>"
        f"<div class='kpis'>"
        f"<div><b>{diagnosis.requests_examined}</b><span class='muted'>requests</span></div>"
        f"<div><b>{total_input:,}</b><span class='muted'>input tokens</span></div>"
        f"<div><b>{hit}</b><span class='muted'>cache read rate</span></div>"
        f"<div><b>{_e(render_score(score))}</b>"
        f"<span class='muted'>cache health (floor)</span></div>"
        f"</div>"
        f"<div class='bar' role='img' aria-label='input read from cache, written to it, "
        f"and billed in full'>"
        f"<span style='width:{read_pct:.1f}%;background:var(--ok)'></span>"
        f"<span style='width:{write_pct:.1f}%;background:var(--medium)'></span>"
        f"<span style='width:{max(100 - read_pct - write_pct, 0):.1f}%;"
        f"background:var(--line)'></span></div>"
        f"<p class='muted'>{total_read:,} tokens read from cache &middot; {written}"
        f"{full_price:,} billed at full input price</p>"
        f"{findings_html}<ul class='muted'>{checked_html}{notes_html}</ul>"
    )


def _across_html(diagnosis: SessionDiagnosis) -> str:
    findings_html = "".join(_finding_html(f) for f in diagnosis.findings) or (
        "<p class='muted'>No findings.</p>"
    )
    fired = {f.detector_id for f in diagnosis.findings}
    bits = [f"checked, clean: {d}" for d in diagnosis.checked if d not in fired]
    bits += [f"not checked — {d}: {r}" for d, r in diagnosis.abstained.items()]
    items = "".join(f"<li>{_e(bit)}</li>" for bit in bits)
    return f"<h2>Across sessions</h2>{findings_html}<ul class='muted'>{items}</ul>"


def render_html(
    results: list[tuple[IngestResult, list[SessionDiagnosis]]],
    rulebook: Rulebook,
    across: SessionDiagnosis | None = None,
) -> str:
    sections = []
    for result, diagnoses in results:
        sessions = {s.session_id: s for s in result.sessions}
        body = "".join(
            _session_html(sessions[d.session_id], d, rulebook)
            for d in diagnoses
            if d.session_id in sessions
        )
        sections.append(f"<h2 class='muted'><code>{_e(result.source)}</code></h2>{body}")
    if across is not None:
        sections.append(_across_html(across))
    try:
        tool_version = version("tokenaudit")
    except Exception:  # pragma: no cover - not installed
        tool_version = "dev"
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>tokenaudit report</title><style>{_CSS}</style></head><body>"
        "<h1>tokenaudit report</h1>"
        "<p class='muted'>Root-cause diagnosis of wasted LLM tokens. Impact figures are "
        "bounded ranges with stated assumptions; findings are not additive (one fix at "
        "a time); requests failing the data-quality gate are excluded, not priced.</p>"
        + "".join(sections)
        + f"<footer>tokenaudit {_e(tool_version)} &middot; rulebook {_e(rulebook.version)} "
        "&middot; self-contained report: no scripts, no external resources</footer>"
        "</body></html>"
    )
