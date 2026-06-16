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

**Status:** ✅ install + boot verified. Committed `c985c56`. Local commit per phase, no remote.

## Phase 2 — `feed.py` (ingestion resilience) + `config.py`

**Done**
- `config.py`: `CONFIG` dataclass — the tuning knob (timeouts, retries/backoff, cache TTL,
  dislocation/zscore/volume/lookback/horizons/stale_after). No hardcoded product list (products are
  discovered from data — see failure detectors).
- `feed.py`: `_get` (exp backoff + jitter, retries 429/5xx, fail-silent → None + log), `health`,
  `latest`, `bulk` (streamed + parquet cache as last-good), pure `validate` (single tz-UTC
  normalization + dirty-data defense + dedupe-on-id), `_records` (envelope unwrap), `_parse_stream`.
- `tests/test_feed.py`: 15 tests — validate edge cases, backoff attempt-count + fail-silent, envelope
  unwrap, NDJSON/array/single-object stream shapes, last-good-cache degradation.

**Decisions**
- Reused decisions 2A/3A/8A/10A/14A and corrections C1/C2/C4 from the plan.
- `_records()` extracts records from any of {prices,data,records,items,results} envelope or a bare
  list — DRY across `latest` + `bulk` (flagged repetition, factored once).
- `bulk()` returns `(df, fetched_at)` where `fetched_at` is a cache token, NOT freshness (C2).

**Failure detectors → corrections (the live-smoke caught two real bugs)**
1. **Envelope miss.** `latest` returned 0 rows live; raw payload was `{"prices":[...]}`, not
   `{data|records}`. Detector: live smoke row-count = 0 despite health 200. Fix: `_records()` +
   `prices` key. Regression test added (`test_latest_unwraps_prices_envelope`).
2. **Bulk shape ≠ NDJSON (reviewer C1 guess was wrong).** `/feed/bulk` is ONE chunked
   `{"prices":[...]}` object, no newlines → my line-parser appended the whole envelope as a single
   "record" → validate dropped it → 0 rows. Fix: `_parse_stream` unwraps via `_records` regardless
   of framing. Test `test_parse_stream_single_envelope_object`.
3. **Slow cold load.** Live bulk took ~89s (cold Render + 48,888 rows). Risk: read timeout at the
   10s default. Fix: `CONFIG.bulk_timeout=120s` for bulk only; parquet cache (TTL 300s) absorbs
   repeats. ponytail note: in-memory assemble (~10MB) — ijson only if OOM.

**Data reality discovered (feeds Phase 3):** 20 products across **4 units** (MT, MWh, tCO2, unit) and
mixed sources; live source-disagreement already visible (THG quota argus 260.33 vs broker 294.61).
→ C4 per-`(product_name, unit, currency)` grouping is mandatory, not optional.

**Test:** `pytest tests/test_feed.py` → 15 passed. Live: health True, latest 5 rows (UTC), bulk
48,888 rows.

**Status:** ✅ committed `2074681`.

## Phase 2.5 — Data-integrity hardening (the core of the business)

Probed the **raw** 50k feed (pre-validation) for real dirt, then hardened `validate()` + added a
test per case. Loop: live-probe → find anomaly → fix → unit-test → assert post-conditions on live.

**Anomalies found in raw 50k:** 528 null prices · 214 ≤0 prices · 222 null products · 143 dup ids ·
241 zero-volume · **all 20 products quoted in >1 currency** (cross-currency VWAP would be garbage) ·
0 whitespace/offset/non-finite currently (defended anyway).

**Hardening (each with a unit test, 25 total now):**
- Dedupe now **sort-by-timestamp then keep-last** → keeps the LATEST per id (was input-order = a bug
  with 143 real dups).
- Drop **non-finite** prices (`np.isfinite`) — inf/-inf can't reach the UI.
- **Negative/null volume → 0** (clamp, don't drop): volume is a VWAP *weight*, a bad weight must not
  discard a good price.
- **Blank unit/currency/source → "UNKNOWN"**: a NaN grouping key would be *silently dropped* by
  pandas `groupby` (dropna default) → invisible data loss. Now it surfaces. (Biggest integrity win.)
- Numeric-string prices kept; **European-decimal "1.524,74" dropped, not mis-parsed to 1.52**
  (explicit: corrupt > silently-wrong).
- Offsets normalized to UTC; whitespace stripped; future-dated rows kept (forward data is legit;
  freshness is feed-relative, C2).

**Test:** `pytest` → **25 passed**. Live post-conditions on 50k: id-unique, all prices finite/>0,
ts UTC+sorted, volume≥0, **no NaN grouping key** → all hold. 50,000 → 48,860 clean; 60 instrument
groups (product×unit×currency).

**Status:** ✅ committed `c431d0d`.

## Phase 3 — `analytics.py` (pure, grouped, guarded, deterministic)

**Done**
- `latest_with_freshness` (REQ-MP-03, freshness vs feed max), `vwap` (REQ-MP-02), `dislocations`
  (REQ-OS: source-disagreement + z-score, volume-gated), `forward_curve` (5.2, linear polyfit).
- Every aggregation `groupby(product_name, unit, currency)` (C4); "now" = `timestamp.max()` (C2);
  float64 internally, rounded at the boundary (determinism, per the nautilus *idea*).
- `tests/test_analytics.py`: 16 tests — empty guards, freshness, VWAP weighting + ÷0 guard +
  currency grouping, both dislocation detectors, volume gate, curve slope/insufficient-points/
  most-traded-group, determinism. Built through `feed.validate()` (DRY + integration).

**Decisions** — 6A guards as first-class; window-slice to lookback (13A); most-traded group picked
deterministically when unit/currency omitted.

**Failure detectors → corrections (live 50k loop caught 2 accuracy bugs synthetic tests missed)**
1. **False 28% "disagreements".** source_disagreement compared each source's last price over the
   full 90d window → fresh-vs-months-stale = drift, not disagreement (31 flags, all bogus). Fix:
   `CONFIG.disagreement_window_hours=48` — compare only contemporaneous quotes. → 31 → **2 genuine**
   flags (HBE-O 17.9%, HVO Class II 16.1%, high-volume, tradeable). Test `test_dislocation_ignores_stale_source`.
2. **Negative forward price.** GO Wind NL projected −5.92 EUR at 90d (linear extrapolation below
   zero). Fix: clamp price/lo/hi ≥ 0. Test `test_forward_curve_clamps_negative_projection_to_zero`.
   (Weak synthetic z-score data also surfaced — fixed test, not code: a small spike inflates its own
   σ; need a longer baseline to clear 3σ.)

**Test:** `pytest tests/` → **40 passed**. Live: 60 instruments, vwap 47 groups (0 fake ÷0),
dislocations 2 tradeable, 0 negative projections across all products.

**Status:** ✅ committed `4547c3e`.

## Phase 3.5 — Property-based testing (Hypothesis) on the pure layer

Added `hypothesis`; `tests/test_properties.py` asserts the data contract holds for ANY generated
input (junk prices incl inf/nan/bool/strings, weird/offset/None timestamps, negative/huge volumes,
blank labels). Scope = pure layer only (validate + analytics); I/O & LLM excluded.

**Properties:** validate output invariants (id-unique, finite price in [price_min, price_max], UTC+
sorted, no NaN grouping key, volume in [0, volume_max]); **validate idempotent**; VWAP ∈ group price
range; analytics never emit NaN/inf; freshness ≥ 0; forward curve finite & ≥0; determinism.

**Bugs Hypothesis caught that example tests missed (3 real integrity holes):**
1. **Price overflow** — `1.8e306` is finite & >0 so it passed, then overflowed to `inf` downstream.
   Fix: `CONFIG.price_max=1e9` reject (the borrowed nautilus PRICE_MAX idea, now justified).
2. **Sub-cent price → 0.00** — `1.19e-7` rounds to 0.00 at 2dp = a misleading zero price. Fix:
   `CONFIG.price_min=0.01` floor.
3. **Empty-frame dtype inconsistency** — the `len==0` early return gave object-dtype columns while
   the filtered-to-empty path gave typed columns → `validate(empty) != empty` (not idempotent).
   Fix: route empty through the same pipeline → always typed, consistent schema.
   Also `volume_max=1e7` neutralizes absurd volumes (weight overflow).

Regression example-tests added for #1/#2 in `test_feed.py`.

**Test:** `pytest tests/` → **48 passed** (~6s). Live: 50,000 → 48,820 clean; all post-conditions +
idempotency hold on real data.

**Status:** ✅ committing Phase 3.5. Next: Phase 4 — `llm.py` + `copilot.py` (await go-ahead).
