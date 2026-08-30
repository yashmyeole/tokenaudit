"""Rulebook tests: validation, alias resolution, staleness, and docs drift."""

from datetime import date, timedelta
from pathlib import Path

from tokenaudit.rulebook import load_rulebook

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs" / "rulebook.md"

MAX_AGE_DAYS = 90


def test_rulebook_loads_and_validates() -> None:
    rulebook = load_rulebook()
    assert len(rulebook.models) >= 20
    for rules in rulebook.models.values():
        assert rules.source_url.startswith("https://")
        assert rules.min_cacheable_tokens > 0
        assert rules.accounting_shape in ("nested", "exclusive")


def test_every_entry_is_recently_verified() -> None:
    """The rulebook must know when it is rotting: fail CI past 90 days."""
    stale = [
        f"{rules.provider}/{rules.model} (verified {rules.verified_on})"
        for rules in load_rulebook().models.values()
        if date.today() - rules.verified_on > timedelta(days=MAX_AGE_DAYS)
    ]
    assert not stale, f"re-verify against provider docs and bump verified_on: {stale}"


def test_lookup_resolves_dated_model_ids() -> None:
    rulebook = load_rulebook()
    # The resolved model id from the captured TradingAgents runs:
    rules = rulebook.lookup("gpt-5.4-mini-2026-03-17", provider="openai")
    assert rules is not None and rules.min_cacheable_tokens == 2048
    # Anthropic-style date suffix, no provider hint:
    rules = rulebook.lookup("claude-sonnet-4-5-20250929")
    assert rules is not None and rules.min_cacheable_tokens == 1024
    # The tier table's sharp edges:
    assert (opus5 := rulebook.lookup("claude-opus-5")) and opus5.min_cacheable_tokens == 512
    assert (haiku := rulebook.lookup("claude-haiku-4-5")) and haiku.min_cacheable_tokens == 4096
    # Unknown models must return None so detectors abstain:
    assert rulebook.lookup("gpt-99-ultra") is None


def test_unknowns_are_recorded_not_guessed() -> None:
    gemini = load_rulebook().lookup("gemini-2.5-pro", provider="google")
    assert gemini is not None
    assert gemini.cache_read_multiplier is None
    assert gemini.ttl == ()
    assert not gemini.supports_explicit_cache


def test_accounting_shapes_match_provider_sdks() -> None:
    rulebook = load_rulebook()
    anthropic = rulebook.lookup("claude-sonnet-4-5")
    openai = rulebook.lookup("gpt-5.6")
    assert anthropic is not None and anthropic.accounting_shape == "exclusive"
    assert openai is not None and openai.accounting_shape == "nested"


def test_docs_table_is_current() -> None:
    """docs/rulebook.md is generated; fail if it drifted from the data."""
    rendered = load_rulebook().render_markdown() + "\n"
    assert DOCS.read_text(encoding="utf-8") == rendered, (
        "regenerate with: python -m tokenaudit rulebook > docs/rulebook.md"
    )
