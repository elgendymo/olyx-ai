# Magic Spyglass

A broker-edge analytics tool for Jasper, a biofuel broker. It cuts through market noise to
surface **valid pricing dislocations** in renewable feedstocks (HVO, UCO, RME) and compliance
certificates (THG-Quoten, EREs) — and answers "is now a good time to sell?" with cited numbers.

**Why:** Jasper trades live capital off ~200 morning messages, conflicting feeds, and gut feel.
Wrong data loses deals, so this tool puts **trustworthy numbers over pretty screens** — bad, stale,
or half-computed prices never reach him, even when the feed is slow, failing, or dirty.

## What it does (top-to-bottom, as the dashboard lays it out)
- **Market sources** — scope every panel below to one or more sources (e.g. just `broker_quote`).
- **Header metrics + banners** — instruments tracked · newest packet · stale count, with a loud
  **STALE** banner and a 🛡 fat-finger **rejection** banner.
- **🎯 Trade Opportunities** — the hero: the dislocations worth acting on first, ranked, with the
  low-volume noise filtered out (real size only) and the most actionable on top.
- **📈 Live Price Board** — latest price per instrument, how old it is vs the feed, whether it's
  above/below its average (VWAP), and a ⚠ on any price that looks off.
- **📉 Forward Curve & Sell Timing** — "is now a good time to sell?" — the projected trend plus a
  clear SELL/HOLD signal per instrument.
- **📨 Inbox** — client/counterparty mail with an asset + bullish/bearish tag per message and a
  one-click **Summarize unread emails** briefing.
- **🧪 Validation mode** — inject a fake bad price and watch the old (unguarded) vs new (guarded)
  view side-by-side, with the € it would have saved and a per-source reliability scoreboard.
- **🔭 Copilot (sidebar)** — ask in plain language ("what happened to UCO this week?"); the numbers
  are computed, then explained — and the exact figures sit beside every answer so you can check it.

**Cross-source guard (under the hood):** automatically drops a fat-finger price that's wildly off
what the other sources quote at the same time (>50%), and flags the milder odd ones with ⚠ — so a
broken tick never reaches the board.

## Stack
Streamlit + pandas/numpy + plotly; copilot on a local-first LLM via Ollama (swappable to
Anthropic/OpenAI by env). Single-user, no DB.

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

LLM is swappable via env: `BROKER_LLM_PROVIDER=ollama|huggingface|anthropic|openai`,
`BROKER_LLM_MODEL=…`, `OLLAMA_HOST=…`. Cloud providers read their own key
(`ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `HF_TOKEN`). The feed endpoint is one env var:
`FEED_BASE_URL`.

**Auto-detection (local vs deployed):** leave `BROKER_LLM_PROVIDER` unset and the client picks
for you — locally it uses **Ollama** (`qwen2.5:7b`); when an **`HF_TOKEN`** is present it routes
to **Hugging Face** (`Qwen/Qwen2.5-7B-Instruct`, same Qwen family so narration matches). That's
the Streamlit Cloud story: Cloud can't run an Ollama daemon, so add `HF_TOKEN` under **App →
Settings → Secrets** (Streamlit exposes secrets as env vars) and the deployed app talks to HF
while your laptop keeps using local Ollama — no code change, no per-environment config.

> Tests are run with `python -m pytest` (the `-m` puts the repo root on the import path); plain
> `pytest` won't find the modules.

## Layout
- `feed.py` — ingest (stream `/feed/bulk` as NDJSON), retry/backoff, cache-as-last-good, pure `validate()`
- `analytics.py` — VWAP, dislocation, freshness, forward curve, cross-source `guard()` + fault
  injection (pure pandas, grouped per instrument)
- `config.py` — tunable thresholds (the trader's calibration knob)
- `llm.py` — swappable LLM client (Ollama default), retry + fail-silent
- `copilot.py` — deterministic-compute → LLM-narrate-with-citations; inbox summarizer
- `app.py` — Streamlit dark-card dashboard

See `ARCHITECTURE.md` for the data-flow / trust-gate / copilot diagrams, `PHASES.md` for the build
log, and `PITCH.md` for the pitch/cut/truth.
