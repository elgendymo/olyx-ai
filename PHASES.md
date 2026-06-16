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

**Status:** ✅ committed `66945d5`.

## Phase 3.6 — Checklist reflection (pre-LLM)

Audited Phases 1–3.5 against the Senior Product Engineer checklist. Sections 1 & 2 complete;
Section 3 is Phase 4/5 (chat session_state already done); Section 4 is Phase 6 (PITCH).

**Two gaps settled:**
- **Forward-curve sentinel (fixed).** `forward_curve` now ALWAYS returns a dict with `status`
  ("ok" | "no_data" | "insufficient_data") and a human `reason`, instead of bare `None` — so the
  UI/copilot can explain *why* there's no curve (checklist: "strict descriptive sentinel + clear
  warning reasoning"). Tests updated.
- **Zero/negative-volume — CONSCIOUS DEVIATION (kept).** Checklist says *drop* zero/negative-volume
  rows; we **keep the price and zero the weight**. Rationale: a 0/indicative quote still carries a
  valid price level for Pulse/freshness/dislocation, and VWAP already excludes zero weight — dropping
  would discard ~241 live price points. → **PITCH "Cut/Truth" entry.**

**Still scheduled (on track, not gaps):** compute≠narrate + inbox sentiment (Phase 4); analytics
cache-by-token — mechanism ready (`bulk()` returns `fetched_at`), consumed in Phase 5; `base_url` is
already a one-line env failover; PITCH "The Cut" (Phase 6).

**Test:** `pytest tests/` → **48 passed**.

**Status:** ✅ committed `654b53e`.

## Phase 4a — `llm.py` (swappable LLM client, local-first, no API key)

**Done**
- Briefy-style provider abstraction: `chat(system, user)` dispatches by `OLYX_LLM_PROVIDER`
  (ollama default | anthropic | openai | offline) via raw HTTP. Demo default = **Ollama
  `llama3.1:8b`** (Briefy's benchmarked gold-standard local model; already pulled on this box).
- **Dropped the `anthropic` SDK dep** — all providers are raw `requests` calls, like Briefy.
- Fail-silent contract: `chat()` returns **None on every failure** (timeout, no key, model down,
  bad provider, empty content) → copilot degrades to raw facts. `health()` badge for the UI.
- temperature 0 + fixed seed for max reproducibility (NOT a guarantee — see limitations).
- `tests/test_llm.py`: 8 mocked tests (request shape, all fail-silent paths, no-key, health) +
  1 opt-in live test (`OLYX_LIVE_LLM=1`).

**Why local-first:** user has no Anthropic key. Ollama runs offline, no egress, no cost. Swapping to
a cloud key later = one env var (`OLYX_LLM_PROVIDER=anthropic` + `ANTHROPIC_API_KEY`).

**Limitations (deliberately accepted; documented for PITCH):**
1. **Not deterministic.** Even temp 0 + seed, local generation can vary (kv-cache/threads). So the
   determinism *guarantee* stays on the compute layer; narration is allowed to vary → we test
   plumbing + fail-safe, NOT prose (golden/property tests are wrong here).
2. **Can mis-narrate.** An 8B model may phrase poorly or cite a number not in the facts. Mitigation:
   prompt says "use ONLY these numbers" + copilot shows the raw facts beside the prose (Phase 4b).
   The LLM is a convenience layer, never the source of truth.
3. **Latency ~2.5–6s** (cold loads ~5GB) → needs a spinner (Phase 5); not rapid-fire.
4. **Resource/portability:** needs Ollama + ~5GB RAM → live test skipped in CI; mocked tests are the
   real coverage.
5. **No streaming** in the slice (stream:false).

**Test:** `pytest tests/test_llm.py` → 8 passed, 1 skipped. Live: health ok, llama3.1:8b cited
"1165.26 EUR/MT, 2.1%" correctly in 6.1s.

**Status:** ✅ committed `aff7dd7`.

## Phase 4b — `copilot.py` (compute≠narrate + inbox sentiment)

**Done**
- `answer(query, df)`: explicit keyword routing (7A) → deterministic facts dict (via analytics) →
  `llm.chat` narrates with a "use ONLY these numbers" system prompt. Returns
  `{answer, facts, intent, used_llm}` — UI shows answer AND facts (verifiable receipt).
- LLM-offline fallback: `_facts_to_text` renders the same facts deterministically, so the copilot
  always answers and still cites numbers. Cached on `(query, facts)` → repeats skip the LLM.
- `summarize_inbox`: LLM summary + deterministic `_naive_sentiment` keyword tilt (authoritative
  label + offline fallback).
- `tests/test_copilot.py`: 14 tests — routing, product detection, narrate-vs-fallback, caching,
  empty short-circuit, facts determinism, inbox bullish/bearish/empty/llm.

**Test:** `pytest tests/` → **70 passed, 1 skipped**. Live (real data + llama3.1:8b): "arb
opportunities?" → cited RME 19% / SAF HEFA 8.57% spreads; inbox sentiment narrated.

**Live findings (loop) — 2 real issues to address next:**
1. 🔴 **Forward curve / VWAP have NO per-instrument outlier rejection.** Live HVO curve included a
   274,714 EUR/MT spike (instrument trades ~1,800) — a deliberate feed outlier that passes validate
   (<price_max) then skews the daily-mean curve; the LLM narrated the garbage. SRS §5.3 wants noise
   filtering. → propose robust aggregation (median/MAD winsorize) before curve & VWAP. **DECISION PENDING.**
2. 🟡 **Small-model inbox sentiment is unreliable** — llama3.1:8b hallucinated "Bearish on Crude Oil"
   (not in the text) vs naive "Bullish". Expected (Phase 4a limitation); naive label stays
   authoritative; PITCH "Truth" entry. Optional: prompt tweak "name only assets in the messages".

**Status:** ✅ committing Phase 4b. Next: decide outlier handling, then Phase 5 (UI wiring).
