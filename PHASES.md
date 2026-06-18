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
Section 3 is Phase 4/5 (chat session_state already done); Section 4 is Phase 6.

**Two gaps settled:**
- **Forward-curve sentinel (fixed).** `forward_curve` now ALWAYS returns a dict with `status`
  ("ok" | "no_data" | "insufficient_data") and a human `reason`, instead of bare `None` — so the
  UI/copilot can explain *why* there's no curve (checklist: "strict descriptive sentinel + clear
  warning reasoning"). Tests updated.
- **Zero/negative-volume — CONSCIOUS DEVIATION (kept).** Checklist says *drop* zero/negative-volume
  rows; we **keep the price and zero the weight**. Rationale: a 0/indicative quote still carries a
  valid price level for Pulse/freshness/dislocation, and VWAP already excludes zero weight — dropping
  would discard ~241 live price points. → **deliberate cut, documented.**

**Still scheduled (on track, not gaps):** compute≠narrate + inbox sentiment (Phase 4); analytics
cache-by-token — mechanism ready (`bulk()` returns `fetched_at`), consumed in Phase 5; `base_url` is
already a one-line env failover (Phase 6).

**Test:** `pytest tests/` → **48 passed**.

**Status:** ✅ committed `654b53e`.

## Phase 4a — `llm.py` (swappable LLM client, local-first, no API key)

**Done**
- Briefy-style provider abstraction: `chat(system, user)` dispatches by `BROKER_LLM_PROVIDER`
  (ollama default | anthropic | openai | offline) via raw HTTP. Demo default = **Ollama
  `llama3.1:8b`** (Briefy's benchmarked gold-standard local model; already pulled on this box).
- **Dropped the `anthropic` SDK dep** — all providers are raw `requests` calls, like Briefy.
- Fail-silent contract: `chat()` returns **None on every failure** (timeout, no key, model down,
  bad provider, empty content) → copilot degrades to raw facts. `health()` badge for the UI.
- temperature 0 + fixed seed for max reproducibility (NOT a guarantee — see limitations).
- `tests/test_llm.py`: 8 mocked tests (request shape, all fail-silent paths, no-key, health) +
  1 opt-in live test (`BROKER_LIVE_LLM=1`).

**Why local-first:** user has no Anthropic key. Ollama runs offline, no egress, no cost. Swapping to
a cloud key later = one env var (`BROKER_LLM_PROVIDER=anthropic` + `ANTHROPIC_API_KEY`).

**Limitations (deliberately accepted; documented):**
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
   authoritative. Optional: prompt tweak "name only assets in the messages".

**Status:** ✅ committed `cce7678`.

## Phase 4c — Robust outlier rejection (SRS §5.3)

**Done**
- `analytics._inliers(prices)`: median ± `CONFIG.mad_k`·(1.4826·MAD) mask; all-equal/tiny → keep all
  (median is always an inlier, so a group never empties). Applied **before VWAP and the forward
  curve**, NOT to the dislocation detector (flagging outliers is its job).
- `CONFIG.mad_k = 5.0` knob.
- Tests: VWAP ignores a 999999 spike; curve robust to a 5M spike (projection stays <1000); inliers
  keep uniform series. 73 total pass.

**Result (the bug from Phase 4b, fixed):** live HVO Class II curve history max **274,714 → 2,168.78**;
slope −1.46, 90d projection 1747 — sane. Copilot now narrates realistic numbers.

**Test:** `pytest tests/` → **73 passed, 1 skipped**.

**Status:** ✅ committed `2c28295`.

## Phase 4d — LLM output quality (4 levers)

**Done**
- **#2 tight facts** — per intent, hand the model 1-3 labeled, unit-tagged numbers (curve trimmed to
  current_price/slope/recommendation/projections/low/high; no 90-row history) instead of raw rows.
- **#3 deterministic verdict** — `forward_curve` now returns `current_price` + `recommendation`
  (downtrend/uptrend/flat from projected change); the LLM phrases the verdict, doesn't decide it.
- **#4 prompt hardening** — "use ONLY FACTS numbers, no invented assets/dates, base timing on
  recommendation, never compute".
- **#1 number-grounding verifier** — `_is_grounded`: extract every number from the narration (dates
  stripped first), require each to match a facts number within tolerance (2% / 0.5), compared on
  magnitude (sign often in words). Ungrounded narration → rejected → deterministic fallback.
  `answer()` returns `grounded`; `used_llm = grounded`.
- **#5 model bake-off** → switched default `llama3.1:8b` → **`qwen2.5:7b`**.

**Bake-off (our prompts, real facts):**
| model | speed | quality |
|---|---|---|
| **qwen2.5:7b** | ~7s | **best** — correctly read "2 opportunities: TTF 6.87%, HVO IV 4.2%", articulate |
| llama3.1:8b | ~6s | misread the dislocation count ("no opportunities" when 2) — grounded but WRONG |
| qwen2.5:3b | ~1-2s | fast, terse, fine on simple Qs; fumbles nuance |
| gemma4 | 12-19s | empty once; cut |

**Bugs caught while building (loop):**
- Verifier regex dropped the leading minus → false-rejected llama's real `-0.8712` slope. Fixed:
  capture `-?`, compare on magnitude.
- Month-name date regex `dec[a-z]*` ate **"dec-lining"** inside "declining" → corrupted a number →
  false reject. Fixed: whole-word month names (`\b…\b`).
- Renamed `age_min`→`age_minutes` (model misread minutes as days live).

**The honest residual risk:** grounding stops fabricated *numbers*, NOT
*misinterpretation* — llama cited the real "2" and still concluded "no opportunities". No prompt or
8B fixes that; the facts receipt stays on screen so Jasper verifies meaning, not just digits.

**Test:** `pytest tests/` → **78 passed, 1 skipped**. Live (qwen2.5:7b): dislocations read
correctly + grounded; bad curve narration auto-rejected → clean deterministic fallback.

**Status:** ✅ committed `6937c91`.

## Phase 4e — Multi-asset cross-wire fix + inbox asset-lock

**A reviewer caught a live hole in 4d** (verified empirically): number-grounding checks number- and
asset-membership *independently*, so with ≥2 assets in the payload "POME trading at 1165.26" (UCO's
price) PASSED. Independent set-membership = false safety on multi-asset facts.

**Fix — single-asset context isolation (copilot):**
- The LLM narrates numbers ONLY when facts are scoped to ONE instrument (`_single_asset`). Then a
  cross-wire is impossible — no other asset's numbers are in context. Verifier also rejects prose
  naming a *foreign* known product (`_mentions_foreign_asset`).
- Multi-asset questions ("any opportunities?") skip the LLM and return the deterministic render,
  which binds each number to its asset by construction. No N-call latency (rejected the reviewer's
  per-asset loop — Ollama serializes; 5 calls = 10-20s).

**Fix — inbox asset-lock (per user):** gazetteer (`_find_product`) locks the asset name from the raw
text; the LLM writes asset-free sentiment prose only; Python assembles `"{asset} — {summary}"`. No
known instrument -> skip the LLM, return "Unrecognized instrument." The model is structurally
incapable of inventing "Crude Oil" because it never authors the name.

**Tests:** +6 (cross-wire blocked, multi-asset deterministic, foreign-asset reject, inbox lock/skip).
**82 passed.** Live: single-asset narrates (verified); multi-asset = "UCO 22.9%; TTF 16.88%;
Glycerine 10.77%" (correctly bound, no LLM); inbox "UCO — …" locked; no-asset -> Unrecognized.

**🟡 RESIDUAL (reasoning, unfixable by grounding) — needs a Phase 5 decision.** The live "good time
to sell HVO Class II?" answer was self-CONTRADICTORY: "Now is not a good time to sell … the
recommendation suggests selling sooner rather than waiting." Grounding passed (no bad numbers) but
the model garbled the verdict. The deterministic `recommendation` field is unambiguous
("downtrend — selling sooner likely beats waiting"); the LLM muddied it. Mitigation for Phase 5:
show the deterministic recommendation verbatim as the headline; LLM prose is color only, never the
verdict.

**Status:** ✅ committed `d6a8b90`.

## Phase 4f — Verdict/translation split (kills the contradictory sell answer)

The 4e residual (LLM garbled the sell verdict) was a visible trust-killer. Fix: **separate the
verdict from the translation** for the curve intent.
- **Verdict (deterministic):** `[SELL SIGNAL|HOLD SIGNAL|NEUTRAL] <trend>, <±pct>% projected Nd` —
  computed from the curve, framed as a SIGNAL not a command (it's a linear fit, not an oracle).
- **Translation (LLM):** states ONLY the grounded numbers (price/range/VWAP), with directional words
  (buy/sell/hold/should/momentum/upside…) **forbidden AND verified** (`_DIRECTIONAL` regex rejects
  them — prompt + enforce, since tone evades number-grounding). Added the instrument's VWAP to curve
  facts so the translation has a concrete grounded delta to state.
- Rejected translation / offline → deterministic state sentence. Verdict always shown.

**Critically rejected** the reviewer's claim that banning words makes contradiction "physically
impossible" — word-bans cut explicit verdicts, not tonal implication; only number-grounding is a
hard guarantee. And rejected a hard `[SELL]` imperative (oversells a linear fit) → "SELL SIGNAL".

**Tests:** +2 (verdict deterministic + directional translation rejected; clean translation accepted).
**84 passed.** Live: "sell HVO Class II?" → "[SELL SIGNAL] downtrend, -14.8% projected 90d. …price
2195.69, range 1510.87–2195.69, VWAP 1972.55." — coherent, no contradiction.

**Status:** ✅ committed `3f2dca4`.

## Phase 5 — Streamlit UI wiring

**Done** — `app.py` is now the full dashboard over the tested engines (it computes nothing):
- **Cache-by-token (14A):** `load_bulk()` fetches the 50k frame once per TTL; `view_*` analytics are
  `@st.cache_data` keyed on the cheap `fetched_at` token (not the hashed df) → tab switches don't
  recompute and don't refetch.
- **Header:** instruments / newest-packet / stale metrics, LLM provider+model health badge, Refresh.
- **Pulse:** instrument selectbox → plotly dark chart (daily price + VWAP line + dashed projection),
  deterministic `[SIGNAL]` verdict line, latest-quote table with feed-relative age (C2).
- **Opportunities:** volume-gated dislocation table, tradeable-first, 🔴/⚪ priority + signal column.
- **Inbox:** textarea → asset-locked summary + sentiment badge.
- **Copilot:** `st.chat_input` + `session_state` history; each answer shows a verified/deterministic
  tag and an expandable **facts receipt** (the source of truth beside the prose).

**Verified headless** with Streamlit `AppTest` (catches API errors a boot check can't): no render
exception on initial load OR after a chat turn; 4 tabs/metrics render; copilot path appends turns
and renders the deterministic answer. Fixed deprecated `use_container_width` → `width="stretch"`.
README run steps updated (`ollama pull qwen2.5:7b`, env swaps).

**Status:** ✅ committed `8fa24ff`.

## Phase 5b — Single-glance redesign (Briefy-style, no tabs)

UX critique: tabs contradicted the "dislocations **surfaced**" thesis (the opportunity queue was
hidden behind a click) and the copilot wasn't a persistent presence. Restructured:
- **Persistent copilot in the sidebar** (always visible beside the data; closest Streamlit gets to
  Briefy's docked assistant) — chat history + facts receipt + refresh live there.
- **No tabs.** Main column is one glance, top-down by urgency: loud **STALE banner** → metrics →
  **🎯 NEEDS ATTENTION NOW** hero (tradeable dislocations = the opportunity queue) → **PULSE** board
  (price · age · ▲/▼ vs VWAP) beside the **FORWARD CURVE** chart + verdict → **INBOX** card.
- `st.container(border=True)` cards with mono eyebrow headers (Briefy calm-card feel within
  Streamlit's limits — no pixel-match without leaving Streamlit, which we won't for a 6h slice).

**Verified headless** (AppTest): no exception on load or after a sidebar-chat turn; sidebar
chat_input works; all four section cards render. 84 tests pass.

**Status:** ✅ committed `fb59817`.

## Phase 5c — Calm-card styling (rejected st_tailwind)

Evaluated `st_tailwind` at source level (8KB): it injects an iframe per styled call that reaches
`parent.document.addTokens(...)` to bolt Tailwind classes onto Streamlit elements **by undocumented
`data-testid`**. Rejected — version-coupled (hardcoded testids vs our bleeding-edge 1.58.0; silent
no-op on drift), fights React (classes flicker/vanish on rerun), an iframe per call, and can't reach
the elements that matter (plotly charts, chat, the React dataframe grid). Net-negative for a slice
graded on data integrity, not polish.

Instead: a small hand-written CSS block via `st.markdown(unsafe_allow_html=True)` — accent card
borders + gradient, mono uppercase eyebrow headers (cyan), boxed metrics, darker sidebar, tighter
chat/dataframe. No dependency, no iframe/React fight; degrades gracefully if a testid drifts.
AppTest: no exception. (Deliberate cut: evaluated st_tailwind, chose stdlib CSS.)

**Status:** ✅ committed `7dfed35`.

## Phase 5d — Perf (instant load + bg refresh), layout & SOTA polish (Chrome-verified)

**Perf (the big UX fix):** load was blocking ~90s on every load AND refresh because the cache was
TTL-gated. Now: **serve the parquet cache instantly** (sub-second), fetch synchronously only on the
very first run when no cache exists; **manual refresh runs in a daemon thread** and an
`st.fragment(run_every=2)` watcher swaps in the new data **silently** when it lands.

**Layout:** Pulse and Forward Curve are now **full-width stacked** (both big/readable, chart 420px).
Inbox is a **Gmail-style mock** (unread dots, sender/subject/snippet/time, per-email asset + sentiment
chips, net read, AI digest) — Briefy-style. Wider sidebar (380px) + bigger chat input.

**Polish (driven by Chrome DevTools MCP screenshots):** sans body + mono numbers (config `font`),
gradient title, semantic palette (cyan accent · emerald up/fresh · rose down/sell/stale · amber
projection), compact colored hero rows, card hover/shadow, styled scrollbars, colored verdict, and
the Pulse board sorted **freshest-first** with green ▲ / rose ▼ via a pandas Styler (2-dp prices).

**Fixes found visually:** Pulse was sorted stalest-first (→ freshest-first); `last_price` showed 6
decimals under the Styler (→ `{:,.2f}`); naive sentiment matched substrings ("prices"→rise,
"sellers"→sell) → **word-boundary, directional-only lexicon** (Klaas now correctly Bullish).

**Verified** via Chrome MCP across multiple screenshots; 84 tests pass.

**Status:** ✅ committing Phase 5d. Next: Phase 6.

## Phase 6a — Multi-source scope (gain max market info, no new feeds)

**Why:** brokers want to read each market source on its own (broker_quote vs exchange vs …). No
free external feed matches the products (RME/biodiesel, EUR/MT) — the space is paywalled PRAs
(Platts/Argus/ICIS) — so adding sources would mean *different* products, which Jasper doesn't want.
Instead we surface the sources **already in the feed** (the `source` field).

**Built:** a "Market sources" multiselect that scopes every panel (Pulse, opportunity queue,
forward curve, metrics, stale banner). It's a subset of the already-validated frame → **no new I/O,
no integrity re-check**; rate-limit backoff (`feed._get`) and `validate()` still own those concerns.
`sources` is part of the cache key, so views stay deterministic per selection.

## Phase 6b — Cross-source validation (defense-in-depth circuit breaker)

**Why:** the Pulse board's "latest price" was the **one number Jasper trades on** and it was
unguarded — a raw last tick. A single fat-finger print (RME at 15,247 not 1,524) became the
displayed price and fed the z-score detector. The MAD filter only protected VWAP/curve, pooled
across sources, and couldn't attribute a bad print to a source.

**Built (`analytics.guard()` + `latest_with_freshness` + `circuit_breaker_pct` knob):**
- **Circuit breaker** — drops *recent* ticks >20% off the **contemporaneous peer consensus** (median
  of each source's latest). History untouched, so trending products aren't nuked. Needs ≥2 live
  sources; a lone source passes through.
- **MAD suspect flag** — survives the breaker but is a cross-source MAD outlier → shown with a ⚠
  badge (flag, don't drop — a 2–5% dislocation is *money*, not bad data).
- **Source attribution** — every drop logged with its source for vendor-quality tracking, surfaced
  in a dashboard banner + table.

**Deliberate deviation from spec:** the breaker lives in `analytics.guard()`, not `feed.validate()`.
`validate()` sees the whole 1-yr frame with no time context — a 20% rule there would drop legitimate
trends. "Last known consensus" = contemporaneous peers, which is what guard does.

## Phase 6c — Validation mode (proof for stakeholders) + fullscreen fix

**Why:** stakeholders want to *see* the guard work, not read code. The prod playbook (shadow mode,
staging mirror, Grafana) assumes a live trading stream that doesn't exist here — **cut as theatre**.
What proves value on a mock feed in a demo:
- **Fault injection (chaos)** — `analytics.inject_fault()` appends one synthetic tick `pct` off the
  latest; ±25% trips the breaker, ±4% is kept as a real dislocation.
- **RAW vs GUARDED A/B** — left spikes, right holds at consensus (+ ⚠).
- **Saved-capital** — `volume × |price − consensus|`, the € a bad position would have cost.
- **Source reputation leaderboard** — rejections + € blocked, grouped by source.

**Fullscreen fix:** the Pulse board's hardcoded `height=380` truncated the table; height now scales
to row count (capped 1200px) so fullscreen shows the whole board.

**Status:** ✅ 89 tests pass (5 new in `tests/test_guard.py`: breaker drop+attribution, dislocation
kept, lone-source passthrough, suspect flag, fault-injection spike-caught/drift-kept).

## Phase 6d — Broker-facing labels, header fix, e2e calibration

**Labels (per Jasper):** PULSE → **LIVE PRICE BOARD**, 🔮 FORWARD CURVE → **📉 FORWARD CURVE & SELL
TIMING**, NEEDS ATTENTION NOW → **🎯 TRADE OPPORTUNITIES**. "AI digest" → **Summarize unread emails**.
Greeting-orb glow was clipped at the page top → header gets `padding-top` + `overflow:visible`.
Pulse table height removed so inline stays compact and native fullscreen expands fully.

**Stale-banner bug (the big one).** Banner read "56 instruments STALE — oldest 6433h behind." Root
cause: `feed_now = timestamp.max()` and the dirty feed carries ~489 future-dated junk ticks (newest
2026-07-16, ~a month ahead). One far-future tick defined "now", so every real instrument looked
hundreds of hours stale — AND it pushed the source-disagreement window into the empty future, killing
that detector. Fix: **robust `feed_now`** = latest tick at/under the 99th percentile (ignores the
future tail); freshness clipped at 0; `stale_after` 1h → **48h** (instruments quote daily/weekly).
Result on live cache: now ≈ today, stale 56 → 37 (genuinely old lines), disagreement detector alive
(20 opportunities). Not a fetch failure — the bulk load and bg refresh were always working.

**Circuit-breaker recalibration (found by e2e).** A headless `AppTest` run showed the 20% breaker
dropping 23 instruments' ticks — including legitimate 20–30% cross-source dislocations (Carbon EUA,
HVO, UCO), the tool's core signal — and, where the median consensus was junk-contaminated, the GOOD
ticks. Real spreads top ~30%; true fat-fingers were 800%+. Moved `circuit_breaker_pct` 0.20 → **0.50**
so AUTO-DROP catches only catastrophic junk (3 ticks: 825% / 92,426% / 101,722%); 20–30% spreads now
survive → surfaced as opportunities + ⚠-flagged (flag-don't-drop intent preserved).

**Verified:** 89 tests pass; `AppTest` runs end-to-end with no render exception; all renamed sections
present; banner honest (37 stale).

### How both bugs were detected (method)

Neither bug was found by the unit suite — both needed **real dirty data**. Worth recording because
"handle the feed's reality" is the graded pillar.

1. **The tool warned on itself.** The stale banner read "oldest **6433h** behind" (~268 days). The
   number was *implausible on its face* — a freshness warning showing a quarter of a year is
   self-evidently broken (crying wolf on 56/60 lines). The dashboard surfaced its own bug.
2. **Confirmed empirically, not by assumption.** Rather than guess-and-patch the threshold, I queried
   the actual cache (`feed._read_cache()`) and printed the timestamp distribution — min, max, 99th
   percentile, rows beyond the 99th, per-instrument freshness. The evidence was unambiguous:
   `timestamp.max = 2026-07-16` (~a month ahead) vs 99th pct `2026-06-15` (≈ today), 489 future-dated
   rows. A single future outlier was defining "now".
3. **The breaker bug needed an e2e run.** All 89 unit tests passed — they use small *clean synthetic*
   frames where `max()` ≈ the 99th pct and there are no fat-fingers, so the bug was invisible to them.
   It only appeared when the whole app ran headless (`streamlit.testing.v1.AppTest`) against the full
   live cache: `guard()`'s drop logs showed the 20% breaker eating real 20–30% Carbon/HVO/UCO
   dislocations. Reading the side-effect logs of a real run, not asserting on a fixture, caught it.

**Takeaway:** clean-room tests prove the math; only real-feed inspection + an end-to-end run prove the
behaviour. Both bugs hid behind a green test suite.

## Phase 6e — Full copilot intent coverage ("become Jasper")

Goal: any dashboard term Jasper asks about routes to the right data. Two new intents + broadened
keywords, then a live question battery against the real cache to find what breaks.

- **`history`** (backward-looking, the assignment's "what happened to UCO this week?"): new pure
  `analytics.price_change(df, product, days)` — start→end change %, window high/low, direction, over a
  parsed timeframe (today/week/month/quarter/year). No product → ranks the market's biggest movers.
- **`data_quality`** (the guard, now queryable): "any fat-fingers?", "which broker is unreliable?",
  "is X suspect?", "can I trust this?" → rejected count, suspect count, capital blocked, source
  leaderboard. Product-named queries scope every number to that instrument.
- **Keywords** broadened across all intents (signal/indicator, momentum/buy-sell-timing, typical-price,
  tell-me-about) with the catch-all `freshness` kept LAST so generic phrasings never hijack a sharper
  intent. "how many stale?" now hits the aggregate `feed_age` count, not a price list.

**Bugs found by the live "become Jasper" battery (not by tests):**
1. `price_change` window high/low was polluted by fat-fingers (UCO range showed `1267370`) → apply
   `_inliers` MAD filter inside the window (robust to spikes at any age, unlike recent-only `guard`).
2. **"any outliers?" always returned nothing** — the z-score sub-filter checked `type=="zscore_spike"`
   but `dislocations()` emits `"zscore"`. Long-standing typo; outlier queries were silently dead.
3. Product-specific data-quality ("is Tallow suspect?") reported whole-market numbers → scope dropped
   ticks + suspect counts to the named product.
4. `bad_capital_blocked` hit ~855M from a 2.6M phantom tick (dishonest) → cap per-tick error at the
   consensus notional (you can't lose more than the position).

**Verified:** 105 tests pass (+8 routing); live battery of 12 Jasper questions all route correctly and
return honest numbers.

## Phase 6f — Autonomous "become Jasper" loop (10 rounds vs live data)

Fired ~120 broker questions at the real cache over 10 rounds, fixing every misroute/wrong-number until
a full round found nothing. Bugs caught (all by live behaviour, none by the unit suite):

- **`"cross"` matched "a*cross*"** → "highest price across products" routed to dislocations. Switched
  to hyphenated/spaced `cross-market`/`cross-source`.
- **Product code matched inside words** — `"rme"` in "perfo**rme**r" → "worst performer" returned RME;
  `"uco"` would match "UCOME". `_find_product` Pass 1 is now **word-bounded** (`\bcode\b`).
- **Acronym suffixes unresolved** — "eua?"/"saf?"/"thg?"/"ttf?" (3-char, <4-char Pass-2 floor) found
  nothing; added a small alias whitelist (can't blanket-acronym-match — "CAN" would hijack "**can** I…").
- **Stale list showed FRESH** — "show me everything stale" needed exact phrases; now any
  stale/outdated/lagging mention filters to stale; added an oldest/stalest age sort.
- **"any outliers?" was silently dead** — z-score sub-filter checked `zscore_spike`; analytics emits
  `zscore`.
- **Data-quality trust hazard** — LLM narrated a *rejected* fat-finger as the live price ("Carbon EUA
  is 87,250, suspect"); data_quality is now always deterministic and product-scoped.
- **No direction in movers** — "biggest gainer" showed losers; added gainer/loser/winner sorting.

New capabilities added to hit ~all dashboard vocabulary: **overview** ("market summary", "what's the
market doing?", "anything I should worry about?"), **history** (timeframe change + movers/volatility),
**data_quality** (rejected/suspect/reputation/trust), **vwap_compare** (above/below VWAP = the ▲/▼
signal), and count phrasings → aggregate feed_age.

**Verified:** 117 tests pass (+12); rounds 8–10 of the live battery returned zero misroutes.

## Phase 6g — Ingestion security hardening (untrusted feed, zero-trust)

Threat model: the third-party feed is hostile/compromisable. Ranked real risks for this app and the
defense-in-depth response — stdlib-first, no new deps.

1. **HTML/script injection (highest, concrete).** `product_name`/`source` flow from the feed into
   `st.markdown(..., unsafe_allow_html=True)` — a feed sending `product_name:"<img src=x onerror=…>"`
   = stored-XSS in Jasper's browser. Fix: **`html.escape()` every feed-derived string at the render
   boundary** (hero opportunity rows + inbox). Chose stdlib `html.escape` over `bleach` — we never
   want feed data to *be* HTML, so escape-all beats sanitize-some.
2. **Resource exhaustion / DoS.** `_parse_stream` accumulated all records with no bounds — a huge
   stream or gzip/decompression bomb → OOM. Fix: hard caps in `_parse_stream` — `max_records`
   (200k), `max_stream_bytes` (200MB decoded), `max_line_bytes` (2MB/line, oversized skipped),
   bounded fallback buffer. Truncation is **logged, never silent** (a silent cap reads as "ingested
   everything").
3. **String abuse.** Fix: `validate()` — the single sanitization chokepoint — strips C0/C1 control
   chars + null bytes and truncates to `max_str_len` (256) on every string field, before anything
   stores or renders them (vectorized: 48k rows in 0.05s).

Layers compose: even if one is bypassed, the next holds (bounds → sanitize → escape). TLS verification
is on (requests default; never `verify=False`). Numeric attacks were already covered (price/volume
caps, NaN/Inf drop, robust feed_now).

**Considered, not adopted:** `pydantic` per-record schema validation — the vectorized `validate()` +
bounds is leaner for 50k rows and already the enforced chokepoint; `bleach` — escape-all is stricter
than HTML sanitization for fields that should never contain markup.

**Verified:** 123 tests pass (+5 security: control-char/null strip, length cap, injection-escaped-at-
render, record-count cap, oversized-line skip); e2e clean; validate 48k rows in 0.05s.

## Phase 7 — Data foundation hardened end-to-end (review response)

Review verdict: breadth landed before the data foundation was trustworthy from zero. Four concrete
failures, each fixed and tested — the foundation now holds run-from-zero.

1. **Clean checkout loaded no data.** The only fallback was a binary parquet (version/ignore-fragile).
   Fix: ship `seed_data.json` — a bundled, human-readable slice of REAL history (8 instruments, 5
   sources, ~90d) carrying the feed's own raw junk on purpose. `feed.seed()` validates it through the
   same chokepoint. App load order is now **cache → live → seed**, so a clean checkout (or a dead feed)
   always renders real data — never an empty screen, never fabricated, and clearly labeled "📦 sample,
   not live" so it can't be mistaken for the market.
2. **A refresh could overwrite its own cached fallback.** `bulk()` wrote the cache unconditionally, so a
   flaky/truncated fetch replaced last-good with worse data. Fix: `_safe_to_replace_cache()` — a refresh
   replaces the cache only if non-empty AND ≥ `cache_replace_min_ratio` (50%) of prior rows; otherwise
   the old frame is kept and the regression is logged.
3. **Invalid records dropped silently.** Fix: `validate(df, with_report=True)` returns an audit trail —
   ingested/kept/rejected, a mutually-exclusive reason breakdown (bad price / bad timestamp / missing
   id / missing product / duplicate), and attributed sample rows. Surfaced in a new **DATA QUALITY ·
   INGESTION AUDIT** card. Drops are no longer an act of faith.
4. **Silent good-data loss (found while hardening).** `pd.to_datetime` inferred ONE format from row 0,
   then coerced every valid-but-differently-formatted timestamp (e.g. no fractional seconds) to NaT —
   silently dropping good ticks. Fix: `format="ISO8601"` parses each value by its own ISO variant
   (also removes the dateutil warnings).

Plus responsiveness: media queries make the sidebar, hero header, metric/column rows and tables usable
on mobile (the desk is checked on a phone), without disturbing the desktop layout.

**Verified:** 129 tests pass (+6: rejection-report counts/reasons, seed renders + surfaces rejections,
last-good kept on degraded refresh, cache replaced on healthy refresh, mixed-timestamp survival,
default `validate(df)->df` unchanged); e2e headless clean; seed flows through every panel.

## Future work — deliberately out of scope now, likely important later

Captured so the decisions are explicit and reviewable, not forgotten. None are built; each has a clear
trigger for when it becomes worth doing.

1. **Streaming JSON ingestion (`ijson`).** Today `_parse_stream` loads all records into a list
   (~10 MB at 50k) before handing them to pandas, now hard-capped at 200k records. `ijson` would parse
   the byte stream incrementally — peak memory ≈ one record, not all — and natively handle a giant
   non-newline-delimited `{"prices":[...]}` object (which we currently special-case with a buffer).
   **Out of scope:** unnecessary at 10 MB; adds a dependency and complexity (YAGNI).
   **Trigger:** `max_records` raised into the millions, or the feed grows to hundreds of MB.

2. **Web search as an LLM tool.** Give the copilot LLM a web-search tool (function/tool-calling) so it
   can fetch external context on demand — news, regulatory changes, freight/feedstock signals,
   counterparty info — and fuse it with the in-feed numbers ("UCO tight in ARA this week, and our feed
   shows a 4% dislocation"). The model decides when to search; results come back as grounded snippets.
   **Out of scope:** the take-home is scoped to the provided feed, and the local-first default
   (Ollama) has no built-in search; bolting on search adds a trust/grounding surface (must cite +
   verify sources, handle rate limits and cost) that needs its own design and a provider with tool use
   (e.g. Anthropic/OpenAI tool-calling + a search API).
   **Trigger:** once the desk wants market *context*, not just price math, in one place — and we're on
   a provider whose LLM supports tool calls.

3. **Unified data — connect the dashboard to other company sources.** So Jasper stops dealing with
   fragmented data and constant context-switching: wire in the desk's other systems (CRM/counterparty
   book, deal/position history, internal chat, email, ERP/limits) behind the same copilot and board,
   one pane of glass.
   **Out of scope:** no access to those systems in the take-home; each is a separate integration with
   its own auth, schema, and freshness model.
   **Trigger:** production deployment on the desk, where those sources exist and the integration cost
   pays back in saved context-switching.
