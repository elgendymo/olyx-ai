"""Magic Spyglass — broker-edge dashboard for Jasper, a biofuel broker.

Single-glance layout (Briefy-style): persistent copilot in the sidebar, and a main column that
surfaces what needs attention NOW — no tabs, because a trading desk monitors, it doesn't browse.
Thin presentation over the tested engines (feed/analytics/copilot); the UI computes nothing.
Analytics are cached on a cheap token (bulk fetch time), not by hashing the 50k frame (14A).
"""
import threading
from html import escape as esc          # escape feed-derived strings before any unsafe_allow_html

import plotly.graph_objects as go
import streamlit as st

import analytics
import copilot
import feed
import llm
from config import CONFIG

st.set_page_config(page_title="Magic Spyglass", page_icon="🔭", layout="wide")
if "chat" not in st.session_state:                       # C3: survives reruns
    st.session_state["chat"] = []

# Calm dark "Briefy" feel via a small hand-written CSS block — no dependency, no React fight.
# Targets a few Streamlit testids; if any drift across versions it just no-ops (graceful).
st.markdown("""<style>
:root { --mono: ui-monospace, SFMono-Regular, "JetBrains Mono", Menlo, monospace;
        --accent: #22d3ee; --line: rgba(148,163,184,0.14); }
/* hard guard: a too-wide sidebar must never create a horizontal scroll / off-screen content */
html, body, [data-testid="stAppViewContainer"] { overflow-x: hidden !important; max-width: 100vw; }
.block-container { padding-top: 2rem; padding-bottom: 3rem; max-width: 1380px; }
/* title */
h1 { font-weight: 800 !important; letter-spacing: -0.02em; font-size: 2rem !important; }
/* cards */
[data-testid="stVerticalBlockBorderWrapper"] {
  background: rgba(255,255,255,0.025);
  border: 1px solid var(--line) !important; border-radius: 16px;
}
[data-testid="stVerticalBlockBorderWrapper"] > div { padding: 0.2rem 0.2rem; }
/* eyebrow headers (#####) */
[data-testid="stMarkdownContainer"] h5 {
  font-family: var(--mono); letter-spacing: .12em; font-size: .72rem; font-weight: 600;
  color: #67e8f9; text-transform: uppercase; margin: .1rem 0 .35rem; opacity: .9;
}
/* metrics — boxed, mono tabular value */
[data-testid="stMetric"] { background: rgba(148,163,184,0.05); border: 1px solid var(--line);
  border-radius: 12px; padding: .55rem .9rem; }
[data-testid="stMetricValue"] { font-family: var(--mono); font-variant-numeric: tabular-nums;
  font-size: 1.5rem !important; }
[data-testid="stMetricLabel"] { opacity: .65; }
/* sidebar */
section[data-testid="stSidebar"] { background: #0d0e11; border-right: 1px solid var(--line); }
section[data-testid="stSidebar"] h3 { letter-spacing: -.01em; }
/* chat + tables + numbers in mono */
[data-testid="stChatMessage"] { background: rgba(148,163,184,0.05); border-radius: 12px; }
[data-testid="stDataFrame"] { font-family: var(--mono); font-size: .82rem;
  font-variant-numeric: tabular-nums; border-radius: 12px; }
/* alert (stale banner) */
[data-testid="stAlert"] { border-radius: 12px; }
/* buttons */
.stButton > button { border: 1px solid rgba(34,211,238,0.45); border-radius: 10px; font-weight: 600; }
.stButton > button:hover { border-color: var(--accent); color: var(--accent); }
/* selectbox + textarea rounding */
[data-baseweb="select"] > div, .stTextArea textarea { border-radius: 10px; }
/* roomier copilot column on DESKTOP only; phones use Streamlit's native sidebar drawer
   (Streamlit 1.58 puts no aria-expanded on the <section>, so don't pin a width there). */
@media (min-width: 821px) {
  section[data-testid="stSidebar"] { width: 360px !important; min-width: 360px !important; }
}
[data-testid="stChatInput"] textarea { font-size: .95rem; min-height: 3rem; }
[data-testid="stChatInput"] { border-radius: 12px; }
/* gmail-style inbox rows */
.eml { display:flex; gap:.6rem; align-items:flex-start; padding:.55rem .7rem; border-radius:10px;
  border:1px solid var(--line); margin-bottom:.4rem; background:rgba(148,163,184,0.03); }
.eml.unread { border-left:3px solid var(--accent); background:rgba(34,211,238,0.05); }
.eml .who { font-weight:600; font-size:.9rem; }
.eml .subj { color:#cbd5e1; font-size:.88rem; }
.eml .snip { color:#94a3b8; font-size:.8rem; }
.eml .meta { margin-left:auto; text-align:right; white-space:nowrap; font-size:.72rem; color:#94a3b8; }
.chip { font-family:var(--mono); font-size:.66rem; padding:.05rem .4rem; border-radius:6px; }
.bull { background:rgba(16,185,129,0.16); color:#6ee7b7; }
.bear { background:rgba(244,63,94,0.16); color:#fda4af; }
.neut { background:rgba(148,163,184,0.15); color:#cbd5e1; }
/* card depth + hover */
[data-testid="stVerticalBlockBorderWrapper"] { box-shadow: 0 10px 28px rgba(0,0,0,0.22);
  transition: border-color .15s ease; }
[data-testid="stVerticalBlockBorderWrapper"]:hover { border-color: rgba(34,211,238,0.3) !important; }
/* hero opportunity rows */
.opp { padding:.45rem .1rem; border-bottom:1px solid var(--line); font-size:.95rem; }
.opp:last-child { border-bottom:none; }
.opp .dot { color:#fb7185; }
.opp .sig { color:#fda4af; font-weight:700; }
.opp .mut { color:#94a3b8; }
/* styled scrollbars */
::-webkit-scrollbar { width:10px; height:10px; }
::-webkit-scrollbar-thumb { background:rgba(148,163,184,0.22); border-radius:8px; }
::-webkit-scrollbar-track { background:transparent; }
/* verdict */
.verdict { font-size:1.35rem; font-weight:800; letter-spacing:-.01em; margin:.2rem 0 .4rem; }
/* greeting orb — ported from Briefy greeting.css */
.greeting-orb {
  display:inline-flex; align-items:center; justify-content:center;
  width:32px; height:32px; border-radius:999px; font-size:1.1rem;
  flex-shrink:0; animation: orb-glow 3.6s ease-in-out infinite; }
.orb-morning {
  background: radial-gradient(circle at 30% 30%, #fef3c7 0%, #f59e0b 60%, rgba(245,158,11,0) 100%);
  box-shadow: 0 0 22px rgba(245,158,11,0.45); }
.orb-afternoon {
  background: radial-gradient(circle at 35% 35%, #fde047 0%, #fbbf24 55%, rgba(251,191,36,0) 100%);
  box-shadow: 0 0 24px rgba(251,191,36,0.40); }
.orb-evening {
  background: radial-gradient(circle at 35% 35%, #fed7aa 0%, #fb923c 55%, rgba(251,146,60,0) 100%);
  box-shadow: 0 0 24px rgba(251,146,60,0.40); animation-duration:4.5s; }
.orb-night {
  background: radial-gradient(circle at 35% 35%, #c7d2fe 0%, #818cf8 55%, rgba(129,140,248,0) 100%);
  box-shadow: 0 0 22px rgba(129,140,248,0.35); animation-duration:5s; }
@keyframes orb-glow {
  0%,100% { transform:scale(1);    filter:brightness(1); }
  50%      { transform:scale(1.08); filter:brightness(1.15); } }
/* ── responsive: phones/tablets (the desk is also checked on mobile) ── */
@media (max-width: 820px) {
  /* Collapsed-sidebar bleed fix (Streamlit 1.58 puts no aria-expanded on the <section>). The
     collapsed sidebar sits at ~1ch width at the left edge and its text WRAPS into that width as a
     vertical strip ("Copilot"/"ollama"). Use a CONTAINER QUERY: treat the sidebar as a size
     container and hide its content whenever the sidebar itself is rendered narrow (i.e. collapsed)
     — independent of HOW Streamlit collapses it, and without touching the expanded drawer. */
  section[data-testid="stSidebar"] { container-type: inline-size; overflow-x: hidden !important; }
  @container (max-width: 140px) {
    section[data-testid="stSidebar"] [data-testid="stSidebarContent"] { visibility: hidden !important; }
  }
  /* main column always owns the full viewport width */
  [data-testid="stMain"], section.main { width: 100% !important; min-width: 0 !important; margin-left: 0 !important; }
}
@media (max-width: 640px) {
  .block-container { padding-top: .8rem; padding-left: .9rem; padding-right: .9rem; max-width: 100%; }
  h1 { font-size: 1.5rem !important; line-height: 1.15 !important; }
  /* stack any row of columns (metrics, A/B panels, inbox header) instead of squishing them */
  [data-testid="stHorizontalBlock"] { flex-wrap: wrap !important; gap: .5rem !important; }
  [data-testid="stHorizontalBlock"] > div { flex: 1 1 100% !important; min-width: 100% !important; }
  [data-testid="stMetricValue"] { font-size: 1.15rem !important; }
  [data-testid="stMetric"] { padding: .45rem .7rem; }
  /* let the greeting/clock header wrap rather than collide (see mobile screenshot) */
  .hero-head { flex-wrap: wrap !important; gap: .35rem !important; }
  .hero-head .hero-clock { text-align: left !important; padding-top: 0 !important; }
  /* cards: a touch more separation + comfortable inner padding on a narrow screen */
  [data-testid="stVerticalBlockBorderWrapper"] { margin-bottom: .15rem; }
  [data-testid="stVerticalBlockBorderWrapper"] > div { padding: .35rem .55rem !important; }
  /* alerts read better with room to breathe */
  [data-testid="stAlert"] { padding: .6rem .8rem !important; }
  /* tables stay usable by scrolling horizontally rather than crushing columns */
  [data-testid="stDataFrame"] { font-size: .76rem; }
}
</style>""", unsafe_allow_html=True)


# ── data layer: serve the cache INSTANTLY, refresh in the background ──
# The mock feed is slow (~90s cold). So we never block on it for rendering: read the parquet cache
# (sub-second) and only fetch synchronously on the very first run when no cache exists. A manual
# refresh fetches in a daemon thread and the UI swaps in the new data silently when it lands.
@st.cache_data(show_spinner=False)
def load_cached():
    """Returns (df, token, mode, report). `mode` is the provenance shown to Jasper so live data
    is never confused with the bundled sample. `report` is validate()'s rejection audit trail.

    Resolution order, so a clean checkout (or a down feed) is NEVER an empty screen:
      cache (last-good parquet) → live fetch → bundled sample seed.
    """
    df = feed._read_cache()
    if df is not None and not df.empty:
        # report from the last live refresh in this deployment, if any (else synthesize a clean one)
        rep = feed.read_report() or {"ingested": int(len(df)), "kept": int(len(df)), "rejected": 0,
                                      "reasons": {}, "note": "loaded from last-good cache; the full "
                                      "rejection log is recorded on each live refresh"}
        return df, feed.CACHE_FILE.stat().st_mtime, "cache", rep
    with st.spinner("First load — fetching market history (one-time, ~1–2 min)…"):
        df, _ = feed.bulk(force=True)
    if df is not None and not df.empty:
        token = feed.CACHE_FILE.stat().st_mtime if feed.CACHE_FILE.exists() else 0.0
        return df, token, "live", (feed.read_report() or {})
    # feed unreachable AND no cache — render the bundled sample (real data, clearly labeled)
    df, rep = feed.seed(with_report=True)
    return df, 0.0, "seed", rep


# Source filter scopes the dashboard views: a subset of the already-validated frame, so no new
# I/O and no integrity re-check needed. `sources` is part of the cache key (a sorted tuple).
# guard() then runs the cross-source circuit breaker once here, so every panel consumes the same
# fat-finger-free frame (Story 3) and shares one source-attributed kill log (Story 4).
@st.cache_data(show_spinner=False)
def _scoped(token, sources):
    df = load_cached()[0]  # provenance/report unused here; analytics only need the frame
    if sources:
        df = df[df["source"].isin(sources)]
    return analytics.guard(df)   # -> (clean_df, dropped)


@st.cache_data(show_spinner=False)
def view_latest(token, sources=()):
    return analytics.latest_with_freshness(_scoped(token, sources)[0])


@st.cache_data(show_spinner=False)
def view_vwap(token, sources=()):
    return analytics.vwap(_scoped(token, sources)[0])


@st.cache_data(show_spinner=False)
def view_dislocations(token, sources=()):
    return analytics.dislocations(_scoped(token, sources)[0])


@st.cache_data(ttl=30, show_spinner=False)
def llm_health():
    return llm.health()


def start_bg_refresh():
    if st.session_state.get("refreshing"):
        return
    t = threading.Thread(target=lambda: feed.bulk(force=True), daemon=True)  # pure I/O, no st.*
    t.start()
    st.session_state["refreshing"] = True
    st.session_state["refresh_thread"] = t


@st.fragment(run_every=2)
def refresh_watcher():
    """Polls the background fetch; when it lands, busts the cache and reruns so data updates silently."""
    if st.session_state.get("refreshing"):
        st.caption("🔄 Updating market data in the background…")
        t = st.session_state.get("refresh_thread")
        if t and not t.is_alive():
            st.session_state["refreshing"] = False
            load_cached.clear()
            st.rerun()


df, token, data_mode, dq_report = load_cached()

# ── persistent Copilot (sidebar — always visible beside the data) ───
with st.sidebar:
    st.markdown("### 🔭 Copilot")
    h = llm_health()
    st.caption(f"{'🟢' if h.get('ok') else '🔴'} {h.get('provider')} · `{h.get('model')}`")
    if h.get("error"):
        st.caption(f"⚠️ {esc(str(h['error']))}")
    with st.expander("🔧 Test LLM"):
        # End-to-end check: distinguishes "model not reachable" from "narration rejected by the
        # grounding gate" — answers stay deterministic in BOTH cases, so the badge alone isn't enough.
        if st.button("Ping model"):
            ok, detail = llm.ping()
            (st.success if ok else st.error)(f"{'reply' if ok else 'failed'}: {esc(str(detail))}")
    for m in st.session_state["chat"]:
        with st.chat_message(m["role"]):
            st.write(m["content"])
            if m.get("facts"):
                st.caption("🟢 verified" if m.get("used_llm") else "⚙️ deterministic")
                with st.expander("facts receipt"):
                    st.json(m["facts"])
    q = st.chat_input("Ask… e.g. any arb opportunities?")
    if q and not df.empty:
        st.session_state["chat"].append({"role": "user", "content": q})
        with st.spinner("Computing & narrating…"):
            res = copilot.answer(q, df)
        st.session_state["chat"].append({"role": "assistant", "content": res["answer"],
                                         "facts": res["facts"], "used_llm": res["used_llm"]})
        st.rerun()
    if st.button("↻ Refresh feed", disabled=st.session_state.get("refreshing", False)):
        start_bg_refresh()
        st.rerun()
    refresh_watcher()

# ── main ────────────────────────────────────────────────────────────
import datetime as _dt
_now_h = _dt.datetime.now().hour
_period, _orb_cls, _phrase = (
    ("morning",   "orb-morning",   "Good morning")   if 5  <= _now_h < 12 else
    ("afternoon", "orb-afternoon", "Good afternoon") if 12 <= _now_h < 18 else
    ("evening",   "orb-evening",   "Good evening")   if 18 <= _now_h < 23 else
    ("night",     "orb-night",     "Good night")
)
_now_str = _dt.datetime.now().strftime("%a %d %b · %H:%M")
st.markdown(f"""
<div class="hero-head" style="display:flex;align-items:flex-start;justify-content:space-between;gap:1rem;flex-wrap:wrap;padding-top:.8rem;margin-bottom:.6rem;overflow:visible">
  <div>
    <div style="display:flex;align-items:center;gap:.55rem;margin-bottom:.3rem;line-height:1">
      <span class="greeting-orb {_orb_cls}"></span>
      <span style="font-size:.75rem;font-weight:600;letter-spacing:.12em;text-transform:uppercase;
        color:#67e8f9;font-family:var(--mono)">{_period}</span>
    </div>
    <h1 style="margin:0 0 .1rem 0;font-size:2.1rem;font-weight:800;letter-spacing:-.03em;line-height:1.1">
      {_phrase},&nbsp;<span style="background:linear-gradient(135deg,#c7d2fe 0%,#e2e8f0 60%);
        -webkit-background-clip:text;-webkit-text-fill-color:transparent">Jasper</span>
    </h1>
    <p style="margin:0;color:#64748b;font-size:.82rem;letter-spacing:.01em">
      🔭 Magic Spyglass &nbsp;·&nbsp; Valid dislocations surfaced. Noise filtered.
    </p>
  </div>
  <div class="hero-clock" style="text-align:right;flex-shrink:0;padding-top:.2rem">
    <div style="font-family:var(--mono);font-size:.95rem;font-weight:600;color:#e2e8f0;
      letter-spacing:.02em">{_now_str}</div>
    <div style="font-size:.72rem;color:#475569;margin-top:.1rem;letter-spacing:.05em">LOCAL TIME</div>
  </div>
</div>
""", unsafe_allow_html=True)
if df.empty:
    st.error("Feed unreachable, no cache, and the bundled sample failed to load. "
             "(fail-silent: nothing fabricated)")
    st.stop()

# Provenance banner — the sample must never be mistaken for live data (data-trust).
if data_mode == "seed":
    st.warning("📦 **Showing bundled sample data** — the live feed is unreachable and no cache "
               "exists. This is a real (but static) slice of history for demo, **not live**. "
               "Use “↻ Refresh feed” once connectivity returns.")

# ── source scope ────────────────────────────────────────────────────
# Each market source (broker_quote, exchange, …) is its own read on the market. Let Jasper
# narrow to a source — or a few — to see what that source alone is saying. Default = all.
all_src = sorted(s for s in df["source"].dropna().unique() if s)
sel = st.multiselect("Market sources", all_src, default=all_src,
                     help="Scope every panel below to these sources. All = the full market.")
src_key = tuple(sorted(sel)) if sel and len(sel) < len(all_src) else ()
dff, dropped = _scoped(token, src_key)   # guarded (fat-fingers removed), shared by curve + log

lat = view_latest(token, src_key)
vw = view_vwap(token, src_key)
dis = view_dislocations(token, src_key)
now = dff["timestamp"].max()
n_stale = int(lat["is_stale"].sum())

# loud stale banner — stale data loses deals, so it can't be a quiet metric
if n_stale:
    oldest_h = lat["freshness_sec"].max() / 3600
    st.error(f"⚠ {n_stale} instrument(s) STALE — oldest {oldest_h:.0f}h behind the feed. "
             "Do not trade these lines.")
# cross-source circuit breaker — what we auto-rejected, attributed to the source that sent it
if dropped:
    srcs = ", ".join(sorted({d["source"] for d in dropped}))
    st.warning(f"🛡 {len(dropped)} fat-finger quote(s) auto-rejected "
               f"(>{int(CONFIG.circuit_breaker_pct * 100)}% off peer consensus) — source(s): {srcs}.")
    with st.expander("rejected quotes (source-attributed)"):
        st.dataframe(dropped, hide_index=True, width="stretch")
c = st.columns(3)
c[0].metric("Instruments", lat.shape[0])
c[1].metric("Newest packet (UTC)", now.strftime("%m-%d %H:%M"))
c[2].metric("Stale", n_stale)

# ── DATA QUALITY: the ingestion audit trail (rejected records are no longer silent) ──
# Drops happen at the validate() chokepoint; this is the visible record of WHAT was rejected
# and WHY, so Jasper can trust the cleaned frame instead of taking it on faith.
with st.container(border=True):
    st.markdown("##### 🧾 DATA QUALITY · INGESTION AUDIT")
    rep = dq_report or {}
    ing, kept, rej = rep.get("ingested", len(df)), rep.get("kept", len(df)), rep.get("rejected", 0)
    rate = (kept / ing * 100) if ing else 100.0
    q = st.columns(4)
    q[0].metric("Records ingested", f"{ing:,}")
    q[1].metric("Accepted (clean)", f"{kept:,}")
    q[2].metric("Rejected", f"{rej:,}")
    q[3].metric("Pass rate", f"{rate:.1f}%")
    reasons = {k: v for k, v in (rep.get("reasons") or {}).items() if v}
    if rej and reasons:
        _LBL = {"bad_price": "Bad / out-of-bounds price", "bad_timestamp": "Unparseable timestamp",
                "missing_id": "Missing id", "missing_product": "Missing product",
                "duplicate_id": "Duplicate id (latest kept)"}
        chips = " ".join(
            f'<span class="chip neut">{esc(_LBL.get(k, k))}: <b>{v:,}</b></span>'
            for k, v in sorted(reasons.items(), key=lambda kv: -kv[1]))
        st.markdown(f'<div style="margin:.1rem 0 .2rem">{chips}</div>', unsafe_allow_html=True)
        samples = rep.get("samples") or []
        if samples:
            with st.expander(f"Rejected records — {min(len(samples), 12)} examples (source-attributed)"):
                st.dataframe(samples[:12], hide_index=True, width="stretch")
        st.caption("Dropped at the validate() chokepoint — every downstream number trusts what passed.")
    elif rep.get("note"):
        st.caption(rep["note"])
    else:
        st.caption("All ingested records passed validation — nothing rejected.")

# ── HERO: needs attention now ───────────────────────────────────────
with st.container(border=True):
    st.markdown("##### 🎯 TRADE OPPORTUNITIES")
    st.caption("Tradeable dislocations (volume-gated). The opportunity queue — what to act on first.")
    trd = dis[dis["tradeable"]] if not dis.empty else dis
    if trd.empty:
        st.success("No tradeable dislocations above the calibration band right now.")
    else:
        rows = ""
        for r in trd.head(5).to_dict("records"):
            sig = (f"{round(r['magnitude'] * 100, 2)}% source spread" if r["type"] == "source_disagreement"
                   else f"{r['magnitude']}σ move")
            rows += (f'<div class="opp"><span class="dot">●</span> <b>{esc(r["product_name"])}</b> '
                     f'<span class="mut">({esc(r["currency"])})</span> — <span class="sig">{esc(sig)}</span> '
                     f'<span class="mut">· {int(r["volume"])} vol · {esc(r["detail"])}</span></div>')
        st.markdown(rows, unsafe_allow_html=True)

# ── Pulse (full width) ──────────────────────────────────────────────
with st.container(border=True):
    st.markdown("##### 📈 LIVE PRICE BOARD")
    st.caption("Latest per instrument · age behind feed · ▲/▼ vs VWAP.")
    board = lat.merge(vw[["product_name", "unit", "currency", "vwap"]],
                      on=["product_name", "unit", "currency"], how="left").sort_values("freshness_sec")
    board["age"] = (board["freshness_sec"] / 60).round(0).astype(int).astype(str) + "m"
    board["vs VWAP"] = board.apply(
        lambda r: "▲" if (r["vwap"] == r["vwap"] and r["last_price"] >= r["vwap"]) else "▼", axis=1)
    # ⚠ = cross-source MAD outlier: shown (flag, don't drop) so Jasper distrusts before trading it
    board["⚠"] = board["suspect"].map(lambda s: "⚠" if s else "")
    disp = board[["product_name", "last_price", "currency", "unit", "vs VWAP", "⚠", "age", "is_stale"]]
    sty = (disp.style
           .map(lambda v: "color:#34d399;font-weight:700" if v == "▲" else "color:#fb7185;font-weight:700",
                subset=["vs VWAP"])
           .map(lambda v: "color:#fbbf24;font-weight:700" if v else "", subset=["⚠"])
           .format({"last_price": "{:,.2f}"}))
    # No explicit height: inline stays compact (default ~10 rows, scrollable) and the native
    # fullscreen ("open in full screen") expands to the whole viewport.
    st.dataframe(sty, width="stretch", hide_index=True)

# ── Forward curve (full width) ──────────────────────────────────────
with st.container(border=True):
    st.markdown("##### 📉 FORWARD CURVE & SELL TIMING")
    opts = {f"{r['product_name']} · {r['unit']} · {r['currency']}":
            (r["product_name"], r["unit"], r["currency"]) for r in lat.to_dict("records")}
    pick = st.selectbox("Instrument", list(opts), label_visibility="collapsed")
    name, unit, cur = opts[pick]
    curve = analytics.forward_curve(dff, name, unit=unit, currency=cur)
    if curve.get("status") == "ok":
        v = copilot._verdict_line(curve)
        vcolor = "#fb7185" if "SELL" in v else "#34d399" if "HOLD" in v else "#94a3b8"
        st.markdown(f'<div class="verdict" style="color:{vcolor}">{v}</div>', unsafe_allow_html=True)
        hx = [p["date"] for p in curve["history"]]
        hy = [p["price"] for p in curve["history"]]
        px = [p["date"] for p in curve["projections"]]
        py = [p["price"] for p in curve["projections"]]
        fig = go.Figure()
        fig.add_scatter(x=hx, y=hy, mode="lines", name="daily price", line=dict(color="#22d3ee", width=2))
        if "vwap" in curve:
            fig.add_hline(y=curve["vwap"], line_dash="dot", line_color="#94a3b8",
                          annotation_text=f"VWAP {curve['vwap']}")
        fig.add_scatter(x=[hx[-1]] + px, y=[hy[-1]] + py, mode="lines+markers",
                        name="projection", line=dict(color="#f59e0b", dash="dash", width=2))
        fig.update_layout(template="plotly_dark", height=420, showlegend=True,
                          legend=dict(orientation="h", y=1.05, x=0),
                          margin=dict(l=8, r=8, t=8, b=8),
                          paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)")
        st.plotly_chart(fig, width="stretch")
    else:
        st.info(f"No forward curve: {curve.get('reason', 'insufficient data')}")

# ── Inbox (Gmail-style mock) ────────────────────────────────────────
MOCK_EMAILS = [
    ("Klaas — Vopak Rotterdam", "08:14", "UCO cargoes — berth delays",
     "Rotterdam berth congestion worsening; UCO cargoes tight this week and sellers are pulling "
     "offers. Expect firmer prices, demand strong."),
    ("trading@greenfuels.nl", "08:42", "HVO Class II — prompt offer",
     "Can offer 2,000 MT HVO Class II prompt. Market feels soft, demand weak — we may need to "
     "discount to move it."),
    ("Ahmed — UCO supplier", "09:03", "POME availability building",
     "POME supply improving out of SE Asia, oversupply building, prices likely to drift lower."),
    ("desk@carbondesk.eu", "09:21", "Carbon EUA squeeze",
     "Carbon EUA rallying hard on tighter auctions and strong buying. Bullish near term."),
    ("Marie — BioRot", "09:47", "RME — client wants to sell",
     "Client is asking whether now is a good time to sell RME. Curve looks flat to us, no rush."),
    ("newsletter@argusmedia", "10:02", "TTF Biomethane weekly",
     "TTF Biomethane premiums steady; THG demand firm into year-end."),
]
_SENT_CLS = {"Bullish": "bull", "Bearish": "bear", "Neutral": "neut"}

with st.container(border=True):
    head = st.columns([3, 2])
    head[0].markdown("##### 📨 INBOX · 6 unread")
    digest = head[1].button("🧠 Summarize unread emails")
    st.caption("Client & counterparty mail. Asset is locked from the text (gazetteer); the chip is "
               "the deterministic keyword signal.")
    for who, t, subj, body in MOCK_EMAILS:
        asset = copilot._find_product(body, df) or "—"
        sent = copilot._naive_sentiment(body)
        st.markdown(
            f'<div class="eml unread"><div>🔵</div>'
            f'<div><div class="who">{esc(who)}</div><div class="subj">{esc(subj)}</div>'
            f'<div class="snip">{esc(body[:96])}…</div></div>'
            f'<div class="meta">{esc(t)}<br><span class="chip {_SENT_CLS[sent]}">{sent}</span> '
            f'<span class="chip neut">{esc(asset)}</span></div></div>', unsafe_allow_html=True)
    bull = sum(copilot._naive_sentiment(b) == "Bullish" for *_, b in MOCK_EMAILS)
    bear = sum(copilot._naive_sentiment(b) == "Bearish" for *_, b in MOCK_EMAILS)
    st.caption(f"Net read: 🟢 {bull} bullish · 🔴 {bear} bearish across 6 unread.")
    if digest:
        import datetime as _dt
        _h = _dt.datetime.now().hour
        _period = "Morning" if _h < 12 else "Afternoon" if _h < 17 else "Evening"
        with st.spinner(f"Generating {_period.lower()} briefing…"):
            brief = copilot.digest_inbox([(w, s, b) for w, _, s, b in MOCK_EMAILS], df, hour=_h)
        st.markdown(f"**{'🌅' if _h < 12 else '☀️' if _h < 17 else '🌙'} {_period} briefing**")
        st.markdown(brief)

# ── 🧪 Validation mode (proof for stakeholders: chaos injection + A/B) ────────
with st.expander("🧪 Validation mode — prove the guard works (fault injection + A/B)"):
    st.caption("Inject a synthetic bad tick into the live frame and watch RAW vs GUARDED diverge. "
               "Demo-only: nothing is persisted, nothing touches the feed.")
    opts2 = {f"{r['product_name']} · {r['unit']} · {r['currency']}":
             (r["product_name"], r["unit"], r["currency"]) for r in lat.to_dict("records")}
    cc = st.columns([3, 2, 2])
    pick2 = cc[0].selectbox("Instrument", list(opts2), key="chaos_inst")
    spike = cc[1].slider("Fault size", -1.0, 1.0, 0.6, 0.05,
                         help="±50%+ → trips the circuit breaker (fat-finger); smaller → kept as a real dislocation")
    vol = cc[2].number_input("Volume (MT)", 1, 100000, 500)
    pname, punit, pcur = opts2[pick2]
    chaos = analytics.inject_fault(dff, pname, punit, pcur, spike, volume=vol)
    raw_board = analytics.latest_with_freshness(chaos)        # what the OLD logic displayed
    g_clean, g_dropped = analytics.guard(chaos)
    g_board = analytics.latest_with_freshness(g_clean)        # what Jasper sees now

    def _row(board):
        m = board[(board["product_name"] == pname) & (board["unit"] == punit) & (board["currency"] == pcur)]
        return m.iloc[0] if not m.empty else None
    rraw, rg = _row(raw_board), _row(g_board)
    a, b = st.columns(2)
    a.markdown("###### 🔴 RAW (old logic)")
    if rraw is not None:
        a.metric(pick2, f"{rraw['last_price']:,.2f} {pcur}",
                 delta=f"{spike * 100:+.0f}% injected", delta_color="inverse")
    b.markdown("###### 🛡 GUARDED (new logic)")
    if rg is not None:
        flag = " ⚠ suspect" if bool(rg["suspect"]) else ""
        b.metric(pick2 + flag, f"{rg['last_price']:,.2f} {pcur}",
                 delta="held at consensus" if g_dropped else "unchanged")

    inj = [d for d in g_dropped if d["source"] == "chaos_inject"]
    if inj:
        st.success(f"🛡 Circuit breaker fired — prevented a **{inj[0]['saved_capital']:,.0f} {pcur}** "
                   f"bad position ({inj[0]['deviation_pct']:.1f}% off consensus {inj[0]['consensus']:,.2f}).")
    else:
        st.info("Inside the band — kept as a legitimate dislocation (no breaker, no false positive).")

    # ── Source reputation leaderboard (real drops this window + the injected one) ──
    alld = list(dropped) + inj
    if alld:
        from collections import defaultdict
        agg = defaultdict(lambda: [0, 0.0])
        for d in alld:
            agg[d["source"]][0] += 1
            agg[d["source"]][1] += d.get("saved_capital", 0.0)
        lb = sorted(([s, n, round(c, 2)] for s, (n, c) in agg.items()), key=lambda r: -r[1])
        st.markdown("###### 🏷 Source reputation — rejections this guarded window")
        st.dataframe({"source": [r[0] for r in lb], "rejected": [r[1] for r in lb],
                      "bad_capital_blocked": [r[2] for r in lb]}, hide_index=True, width="stretch")
