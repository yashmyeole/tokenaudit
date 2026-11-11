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
        if rules.min_cacheable_tokens is None:
            assert rules.min_cacheable_notes
        else:
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
    assert rules is not None and rules.cache_read_multiplier == 0.1
    # Anthropic-style date suffix, no provider hint:
    rules = rulebook.lookup("claude-sonnet-4-5-20250929")
    assert rules is not None and rules.min_cacheable_tokens == 1024
    # The tier table's sharp edges:
    assert (opus5 := rulebook.lookup("claude-opus-5")) and opus5.min_cacheable_tokens == 512
    assert (haiku := rulebook.lookup("claude-haiku-4-5")) and haiku.min_cacheable_tokens == 4096
    sonnet55 = rulebook.lookup("claude-sonnet-5-5")
    assert sonnet55 is not None and sonnet55.min_cacheable_tokens == 512
    terra = rulebook.lookup("gpt-5.6-terra")
    assert terra is not None and terra.min_cacheable_tokens == 1024
    # Unknown models must return None so detectors abstain:
    assert rulebook.lookup("gpt-99-ultra") is None


def test_per_model_read_multipliers() -> None:
    rulebook = load_rulebook()
    expected = {
        "claude-sonnet-4-5": 0.1,
        "claude-opus-5-5": 0.05,
        "claude-sonnet-5-5": 0.05,
        "claude-fable-5-2": 0.025,
        "claude-mythos-5-1": 0.025,
        "gpt-5.6": 0.1,
        "gpt-6.1-sol": 0.05,
        "gpt-6.1-terra": 0.05,
    }
    actual = {}
    for model in expected:
        rules = rulebook.lookup(model)
        actual[model] = rules.cache_read_multiplier if rules else None
    assert actual == expected


def test_gpt56_ttl_options() -> None:
    rules = load_rulebook().lookup("gpt-6.1-terra")
    assert rules is not None
    assert [(t.name, t.seconds, t.write_multiplier) for t in rules.ttl] == [
        ("30m", 1800, 1.25),
        ("12h", 43200, 2.5),
    ]


def test_unknowns_are_recorded_not_guessed() -> None:
    gemini = load_rulebook().lookup("gemini-2.5-pro", provider="google")
    assert gemini is not None
    assert gemini.cache_read_multiplier is None
    assert gemini.ttl == ()
    assert not gemini.supports_explicit_cache

    mini = load_rulebook().lookup("gpt-5.4-mini")
    assert mini is not None and mini.min_cacheable_tokens is None
    assert mini.min_cacheable_notes and "varies" in mini.min_cacheable_notes

    haiku = load_rulebook().lookup("claude-haiku-5-5")
    assert haiku is not None and haiku.base_input_price_per_mtok is None
    assert haiku.price_notes


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
