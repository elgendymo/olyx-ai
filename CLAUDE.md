# CLAUDE.md — Magic Spyglass (OLYX broker-edge tool)

Agent context for this repo. Read before editing. Keep it current when invariants change.

## What this is
A Streamlit dashboard + LLM copilot that gives a biofuel broker (Jasper) an edge over a flaky
third-party renewable-price feed. It surfaces tradeable dislocations, answers plain-language
questions, and **never lets corrupted/half-computed numbers reach the screen** — data integrity and
ingestion resilience are the graded pillars, valued over visual polish.

## Run & test
```bash
source .venv/bin/activate          # `python`/`streamlit` resolve here after activate
pip install -r requirements.txt
ollama pull qwen2.5:7b             # local copilot model, no API key (default provider)
streamlit run app.py              # http://localhost:8501
python -m pytest                  # the `-m` puts repo root on sys.path; bare `pytest` fails to import
```
- LLM is swappable by env: `OLYX_LLM_PROVIDER=ollama|anthropic|openai`, `OLYX_LLM_MODEL=…`
  (cloud reads `ANTHROPIC_API_KEY`/`OPENAI_API_KEY`). Feed endpoint: `FEED_BASE_URL`.
- E2E smoke (headless, catches render errors unit tests can't):
  `python -c "from streamlit.testing.v1 import AppTest; print(bool(AppTest.from_file('app.py').run().exception))"`

## Layout
- `feed.py` — ingestion: retry/backoff (fail-silent), bounded NDJSON stream, parquet cache as
  last-good, and the **pure `validate()` chokepoint** (the only place dirty data is cleaned).
- `analytics.py` — pure pandas, grouped per instrument: VWAP, dislocations, freshness, forward curve,
  `guard()` (cross-source circuit breaker), `price_change`, `inject_fault`.
- `config.py` — every threshold (the trader's calibration knob). Change tuning here, not in logic.
- `llm.py` — swappable LLM client, fail-silent (returns None on any failure).
- `copilot.py` — route query → tight facts → narrate-with-citations; deterministic fallback; inbox digest.
- `app.py` — thin Streamlit presentation over the tested engines (it computes nothing).

## Invariants — do not break (each has a reason and tests)
- **`validate()` is the single data-cleaning + sanitization chokepoint.** Everything downstream trusts
  its output: typed, deduped on `id` (latest wins), UTC, price/volume bounded, NaN/Inf dropped,
  control chars stripped + strings length-capped. Don't clean data anywhere else.
- **"Now" = `analytics.feed_now(df)`**, a robust 99th-percentile of `timestamp.max` — NOT the wall
  clock (the feed carries future-dated junk; raw `max()` made everything look stale). Freshness clips
  at 0.
- **Analytics group by `(product_name, unit, currency)`** — never do cross-currency/unit math.
- **`guard()` circuit breaker auto-drops only catastrophic ticks (>50% off contemporaneous peer
  consensus).** 20–30% spreads are real opportunities, not bad data — flag, don't drop.
- **Copilot grounds every narrated number against the facts dict.** Multi-asset / data_quality /
  inventory answers are deterministic (no LLM cross-wire). Curve verdict is deterministic; LLM only
  translates and is forbidden directional words.
- **The feed is untrusted.** `unsafe_allow_html` render points MUST `html.escape()` feed-derived
  strings. Ingestion is bounded (`max_records`/`max_stream_bytes`/`max_line_bytes`). TLS verify on.

## Conventions
- Match surrounding style: terse, comment the *why* (often tagged like `C2`, `§5.3`, review refs).
- Tests: `python -m pytest` (or `rtk proxy python -m pytest` locally — the rtk hook swallows pytest's
  summary). Non-trivial logic ships with a test. Add routing cases for new copilot intents.
- Don't auto-commit unless asked. Group commits logically (engine / app / tests / docs).
- `PHASES.md` is the reviewable build log (no references to any separate pitch doc).
