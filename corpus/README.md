# The corpus

Ground truth for the diagnosis engine, in two tracks:

- `synthetic/` — generated fixtures with **known answers**. A deterministic
  simulator (`synthetic/generate.py`) replays agent sessions under specific
  broken configurations and emits the traces a correctly instrumented app
  would have produced, in three real schema dialects.
- `captured/` — recorded runs of **real public projects** (arrives with the
  capture track). These contain genuine waste nobody scripted.

## The boundary between the tracks

Synthetic fixtures are **unit tests, never evidence**. "The tool catches
nine bugs I planted myself" is a strawman, and this project doesn't ship
strawmen. Any number that appears in the top-level README — savings, hit
rates, case-study results — must come from `captured/` runs of code I did
not write. The synthetic track exists so that every detector has a measured
precision and recall against cases with published answers, including
negative cases where a correct tool must stay silent.

One caveat worth stating plainly: the simulator encodes the same provider
rules the detectors will check, so agreement between them is partly
circular. That is what makes the captured track non-optional — real traces
are the check against reality, and the simulator's rules cite the provider
docs they came from so they can be audited independently.

## Label format

Every `<fixture>.trace.json` ships with `<fixture>.label.json`:

| Field | Meaning |
|---|---|
| `contains` | failure modes genuinely present — the tool should report these |
| `must_not_contain` | every other mode — reporting one of these is a false positive |
| `content_captured` | whether message content is in the trace; without it, a tool can prove waste but must not guess the cause |
| `evidence` | machine-readable pointers (which call, which block, which gap) |
| `totals` | session-level token accounting, used to cross-check dialects |
| `notes` | the human explanation |

`contains` and `must_not_contain` always partition the full failure-mode
set, so an evaluation can score both misses and false alarms.

**Attribution precedence:** when tool definitions change or reorder, ground
truth is `tool_schema_churn`, not `prefix_drift`, even though the prefix
did technically change — the fix is different, so the finding must be too.
Fixtures encode this: the tool-churn fixture lists `prefix_drift` under
`must_not_contain`.

## Determinism

The generator uses a fixed simulated clock (2026-01-15T09:00:00Z), no
randomness, integer-only payloads, and stable JSON rendering. The same
command produces byte-identical output on any machine and Python ≥ 3.10:

    python corpus/synthetic/generate.py

`tests/test_corpus.py` regenerates the corpus on every test run and fails
if the committed files differ from what the generator produces, so the
generator and fixtures cannot drift apart. Every trace carries
`tokenaudit.synthetic = true` in its resource attributes so a synthetic
file can never be mistaken for a captured one.

## Schema dialects covered

1. **Current OTel GenAI semconv** — `gen_ai.usage.cache_read.input_tokens`,
   `gen_ai.usage.cache_creation.input_tokens`; `gen_ai.usage.input_tokens`
   includes cached tokens. Message content (opt-in in real
   instrumentations) rides `gen_ai.input.messages` / `gen_ai.output.messages`.
2. **Legacy flat names** (older openllmetry) —
   `gen_ai.usage.cache_read_input_tokens` etc., alongside deprecated
   `gen_ai.usage.prompt_tokens`, which passes the provider's field through —
   for Anthropic that count EXCLUDES cache tokens. Same session, different
   arithmetic: the normalizer has to make them agree.
3. **OpenInference** — `llm.token_count.prompt_details.cache_read` /
   `.cache_write`, flattened `llm.input_messages.<i>.message.*` attributes.

Sources (verified 2026-08-29):
[OTel GenAI spans](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-spans.md) ·
[openllmetry #3647](https://github.com/traceloop/openllmetry/issues/3647) ·
[OpenInference spec](https://github.com/Arize-ai/openinference/blob/main/spec/semantic_conventions.md) ·
[Anthropic prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching) ·
[OpenAI prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching)
