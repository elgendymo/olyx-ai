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
streamlit run app.py     # opens http://localhost:8501
python -m pytest         # math + resilience + property + guard + copilot tests
```
First launch fetches ~1yr of history from the mock feed (~1–2 min, cold Render); after that it
reads the parquet cache (sub-second) and refreshes in the background.

### Copilot LLM (required for the AI summaries & narration)
The AI features — the inbox **"Summarize unread emails"** digest and the copilot's plain-language
answers — need an LLM. It defaults to a **local** model via [Ollama](https://ollama.com) (no API
key, no egress) and is **not auto-installed**; set it up once:
```bash
# 1. install Ollama (macOS): brew install ollama   (or download from ollama.com)
# 2. start it (local server on :11434):             ollama serve &
# 3. pull the default model:                         ollama pull qwen2.5:7b
```
**Graceful degradation (by design, not a substitute):** if no LLM is reachable, nothing crashes —
the copilot returns the deterministic **facts receipt** and the digest returns the structured
per-email asset+sentiment lines. The numeric dashboard (prices, dislocations, freshness, curve)
is fully usable without an LLM, but the *AI summarisation/narration* is only there with one running.

LLM is swappable via env: `OLYX_LLM_PROVIDER=ollama|anthropic|openai` (default `ollama`,
model `qwen2.5:7b`), `OLYX_LLM_MODEL=…`, `OLLAMA_HOST=…`. Cloud providers read
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
