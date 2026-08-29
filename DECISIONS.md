# Decision log

Short, dated notes on the non-obvious calls, so the "why" survives longer than my memory of it.

## 001 — Verdicts come from deterministic rules, not an LLM (2026-08-29)

**Decision:** the diagnosis engine is a library of explicit, testable rules. No model call anywhere in the verdict path. If an LLM is ever used, it drafts prose or patch text from an already-decided finding, in a module the detectors cannot import.

**Why:** same trace in, same findings out; every detector can be unit-tested against labeled fixtures and given a measured precision; running the tool is free, which matters for a tool whose whole point is reducing cost; and every finding carries an evidence pointer instead of a vibe.

**Cost accepted:** rules only catch what I have enumerated. An enumerated catalog with measured precision beats open-ended guesses I can't test.

## 002 — Name: tokenaudit, not cachelens (2026-08-29)

The working name for this project was "cachelens". The day-one collision check found [cachelens on PyPI](https://pypi.org/project/cachelens/) — released June 2026, an SDK-wrapper cache meter, i.e. a neighbor in this space, not just a name squatter. Shipping a lookalike name would be unfair to them and confusing for everyone.

Picked **tokenaudit** over the other candidates (wastelens, cachedoctor) for two reasons: it leaves the cache-/-lens naming family entirely, and several planned detectors (retry storms, context bloat, tool-schema bloat) are token-waste problems that aren't cache problems at all, so "cache" in the name undersold the scope. Verified free on PyPI and GitHub 2026-08-29.

## 003 — Apache-2.0, not MIT (2026-08-29)

Explicit patent grant, and it matches the ecosystem this project sits in (OpenTelemetry is Apache-2.0).

## 004 — src layout and strict mypy from the first commit (2026-08-29)

src/ layout so tests import the installed package rather than the working directory. `mypy --strict` costs nothing to adopt on an empty package and a lot to adopt later; the detector engine will be pure functions, which is exactly the code strict typing is cheap for.
