# Magic Spyglass

A broker-edge analytics tool for Jasper (OLYX biofuel desk). It cuts through market noise to
surface **valid pricing dislocations** in renewable feedstocks (HVO, UCO, RME) and compliance
certificates (THG-Quoten, EREs) — and answers "is now a good time to sell?" with cited numbers.

**Why:** Jasper trades live capital off ~200 morning messages, conflicting feeds, and gut feel.
Wrong data loses deals, so this tool optimizes for **data integrity and ingestion resilience over
visual polish** — corrupted/half-computed numbers never reach the screen.

## Stack
Streamlit + pandas/numpy + plotly, Anthropic SDK for the copilot. Single-user, no DB.

## Run
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
ollama pull qwen2.5:7b   # local copilot model — no API key needed (default provider)
streamlit run app.py
python -m pytest         # math + resilience + property + copilot tests
```
LLM is swappable via env (no key needed for the local default):
`OLYX_LLM_PROVIDER=ollama|anthropic|openai`, `OLYX_LLM_MODEL=…`. Cloud providers read
`ANTHROPIC_API_KEY` / `OPENAI_API_KEY`. The feed endpoint is one env var: `FEED_BASE_URL`.

## Layout
- `feed.py` — ingest (stream `/feed/bulk` as NDJSON), retry/backoff, cache-as-last-good, pure `validate()`
- `analytics.py` — VWAP, dislocation, freshness, forward curve (pure pandas, grouped per instrument)
- `config.py` — tunable thresholds (the trader's calibration knob)
- `llm.py` — single Anthropic client with retry + fail-safe
- `copilot.py` — deterministic-compute → LLM-narrate-with-citations; inbox summarizer
- `app.py` — Streamlit dark-card dashboard

See `PHASES.md` for the build log and `PITCH.md` for the pitch/cut/truth.
