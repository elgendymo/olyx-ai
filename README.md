# Magic Spyglass

A broker-edge analytics tool for Jasper (OLYX biofuel desk). It cuts through market noise to
surface **valid pricing dislocations** in renewable feedstocks (HVO, UCO, RME) and compliance
certificates (THG-Quoten, EREs) — and answers "is now a good time to sell?" with cited numbers.

**Why:** Jasper trades live capital off ~200 morning messages, conflicting feeds, and gut feel.
Wrong data loses deals, so this tool optimizes for **data integrity and ingestion resilience over
visual polish** — corrupted/half-computed numbers never reach the screen.

## What it does
- **Market pulse** — latest price per instrument, freshness vs the feed clock, ▲/▼ vs VWAP.
- **Opportunity queue** — volume-gated, ranked dislocations (source disagreement + z-score).
- **Forward curve** — "is now a good time to sell?" with a projected trend per instrument.
- **Broker copilot** — plain-language Q&A, deterministic-compute → LLM-narrate with citations.
- **Source scope** — filter every panel to one or more market sources (e.g. just `broker_quote`).
- **Cross-source guard** — a circuit breaker drops fat-finger ticks (>20% off contemporaneous
  peer consensus) and flags ⚠ statistical outliers, so a broken tick never reaches the board.
- **Validation mode** — fault-injection + RAW-vs-GUARDED A/B, saved-capital, source leaderboard.

## Stack
Streamlit + pandas/numpy + plotly, Anthropic SDK for the copilot. Single-user, no DB.

## Run
```bash
python3 -m venv .venv && source .venv/bin/activate   # `python`/`streamlit` resolve here after activate
pip install -r requirements.txt
ollama pull qwen2.5:7b   # local copilot model — no API key needed (default provider)
streamlit run app.py     # opens http://localhost:8501
python -m pytest         # math + resilience + property + guard + copilot tests
```
First launch fetches ~1yr of history from the mock feed (~1–2 min, cold Render); after that it
reads the parquet cache (sub-second) and refreshes in the background.

LLM is swappable via env (no key needed for the local default):
`OLYX_LLM_PROVIDER=ollama|anthropic|openai`, `OLYX_LLM_MODEL=…`. Cloud providers read
`ANTHROPIC_API_KEY` / `OPENAI_API_KEY`. The feed endpoint is one env var: `FEED_BASE_URL`.

> Tests are run with `python -m pytest` (the `-m` puts the repo root on the import path); plain
> `pytest` won't find the modules.

## Layout
- `feed.py` — ingest (stream `/feed/bulk` as NDJSON), retry/backoff, cache-as-last-good, pure `validate()`
- `analytics.py` — VWAP, dislocation, freshness, forward curve, cross-source `guard()` + fault
  injection (pure pandas, grouped per instrument)
- `config.py` — tunable thresholds (the trader's calibration knob)
- `llm.py` — single Anthropic client with retry + fail-safe
- `copilot.py` — deterministic-compute → LLM-narrate-with-citations; inbox summarizer
- `app.py` — Streamlit dark-card dashboard

See `PHASES.md` for the build log and `PITCH.md` for the pitch/cut/truth.
