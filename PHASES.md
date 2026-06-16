# Build log

Phased delivery: each phase = implement → document (decisions · failure detectors · corrections) →
test → commit → push → stop for review.

## Phase 1 — Secured repo + tech-stack setup + skeleton

**Done**
- `git init`; `.gitignore` (the secret boundary — `.env`, `cache/`, `data/`, `.venv/`, secrets.toml).
- `.env.example` (ANTHROPIC_API_KEY, FEED_BASE_URL). Real `.env` never committed.
- `requirements.txt`: streamlit, pandas, numpy, plotly, requests, anthropic, pyarrow, pytest.
- `.streamlit/config.toml`: dark theme (calm Briefy-ish palette).
- `app.py` skeleton: 4 tabs (Pulse · Opportunities · Inbox · Copilot); **`st.session_state["chat"]`
  initialized now (C3)** so copilot history survives reruns/tab switches.
- README with what/why/run.

**Decisions** (from the §1–§4 review + reviewer §5 corrections — see plan file)
- Streamlit + pandas/plotly; not a Briefy fork. 6 source files, no premature modules.
- Cache seam `@st.cache_data` keyed on a cheap token, not the 50k df (14A).
- C1 stream bulk as NDJSON · C2 freshness vs `timestamp.max()` not wall-clock · C4 group every
  aggregation by `(product_name, unit, currency)`.

**Failure detectors / risks**
- ⚠️ Python 3.14.2 wheel risk — **resolved**: install clean on 3.14 (streamlit 1.58.0, pandas 3.0.3,
  numpy/plotly/pyarrow/anthropic all present). No pin needed. Detector for future: `pip install`
  build failure → fall back to a 3.12 venv.
- Note: pandas **3.0.3** (major). Watch for 3.0 API changes in later phases (e.g. copy-on-write
  defaults, `resample` signature) — will verify against tests in Phase 3.

**Test:** `streamlit run app.py --server.headless` → `/_stcore/health` returns 200; 4 tabs render.
No unit tests yet (no logic in Phase 1).

**Status:** ✅ install + boot verified. Committing. No git remote yet → push pending (will set up
remote before Phase 2 push, or commit-only as agreed).
