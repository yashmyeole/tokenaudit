"""Report rendering: CLI text, JSON, and HTML output."""

from tokenaudit.report.corpus_record import describe, load_corpus_record
from tokenaudit.report.health import HealthScore, health_score, render_score
from tokenaudit.report.html import render_html

__all__ = [
    "HealthScore",
    "describe",
    "health_score",
    "load_corpus_record",
    "render_html",
    "render_score",
]
