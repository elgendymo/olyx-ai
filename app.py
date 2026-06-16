"""Magic Spyglass — broker-edge dashboard for Jasper (OLYX).

Single-glance layout (Briefy-style): persistent copilot in the sidebar, and a main column that
surfaces what needs attention NOW — no tabs, because a trading desk monitors, it doesn't browse.
Thin presentation over the tested engines (feed/analytics/copilot); the UI computes nothing.
Analytics are cached on a cheap token (bulk fetch time), not by hashing the 50k frame (14A).
"""
import threading

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
/* wider sidebar + bigger chat input */
section[data-testid="stSidebar"] { width: 380px !important; min-width: 380px !important; }
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
</style>""", unsafe_allow_html=True)


# ── data layer: serve the cache INSTANTLY, refresh in the background ──
# The mock feed is slow (~90s cold). So we never block on it for rendering: read the parquet cache
# (sub-second) and only fetch synchronously on the very first run when no cache exists. A manual
# refresh fetches in a daemon thread and the UI swaps in the new data silently when it lands.
@st.cache_data(show_spinner=False)
def load_cached():
    df = feed._read_cache()
    if df is None or df.empty:
        with st.spinner("First load — fetching market history (one-time, ~1–2 min)…"):
            df, _ = feed.bulk(force=True)
    token = feed.CACHE_FILE.stat().st_mtime if feed.CACHE_FILE.exists() else 0.0
    return df, token


@st.cache_data(show_spinner=False)
def view_latest(token):
    return analytics.latest_with_freshness(load_cached()[0])


@st.cache_data(show_spinner=False)
def view_vwap(token):
    return analytics.vwap(load_cached()[0])


@st.cache_data(show_spinner=False)
def view_dislocations(token):
    return analytics.dislocations(load_cached()[0])


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


df, token = load_cached()

# ── persistent Copilot (sidebar — always visible beside the data) ───
with st.sidebar:
    st.markdown("### 🔭 Copilot")
    h = llm_health()
    st.caption(f"{'🟢' if h.get('ok') else '🔴'} {h.get('provider')} · `{h.get('model')}`")
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
st.markdown('<h1 style="margin-bottom:.1rem">🔭 <span style="background:linear-gradient('
            '90deg,#e2e8f0,#22d3ee);-webkit-background-clip:text;-webkit-text-fill-color:'
            'transparent">Magic Spyglass</span></h1>', unsafe_allow_html=True)
st.caption("Valid pricing dislocations, surfaced. Noise, ignored.")
if df.empty:
    st.error("Feed unreachable and no cached data. (fail-silent: nothing fabricated)")
    st.stop()

lat = view_latest(token)
vw = view_vwap(token)
dis = view_dislocations(token)
now = df["timestamp"].max()
n_stale = int(lat["is_stale"].sum())

# loud stale banner — stale data loses deals, so it can't be a quiet metric
if n_stale:
    oldest_h = lat["freshness_sec"].max() / 3600
    st.error(f"⚠ {n_stale} instrument(s) STALE — oldest {oldest_h:.0f}h behind the feed. "
             "Do not trade these lines.")
c = st.columns(3)
c[0].metric("Instruments", lat.shape[0])
c[1].metric("Newest packet (UTC)", now.strftime("%m-%d %H:%M"))
c[2].metric("Stale", n_stale)

# ── HERO: needs attention now ───────────────────────────────────────
with st.container(border=True):
    st.markdown("##### 🎯 NEEDS ATTENTION NOW")
    st.caption("Tradeable dislocations (volume-gated). The opportunity queue — what to act on first.")
    trd = dis[dis["tradeable"]] if not dis.empty else dis
    if trd.empty:
        st.success("No tradeable dislocations above the calibration band right now.")
    else:
        rows = ""
        for r in trd.head(5).to_dict("records"):
            sig = (f"{round(r['magnitude'] * 100, 2)}% source spread" if r["type"] == "source_disagreement"
                   else f"{r['magnitude']}σ move")
            rows += (f'<div class="opp"><span class="dot">●</span> <b>{r["product_name"]}</b> '
                     f'<span class="mut">({r["currency"]})</span> — <span class="sig">{sig}</span> '
                     f'<span class="mut">· {int(r["volume"])} vol · {r["detail"]}</span></div>')
        st.markdown(rows, unsafe_allow_html=True)

# ── Pulse (full width) ──────────────────────────────────────────────
with st.container(border=True):
    st.markdown("##### 📈 PULSE")
    st.caption("Latest per instrument · age behind feed · ▲/▼ vs VWAP.")
    board = lat.merge(vw[["product_name", "unit", "currency", "vwap"]],
                      on=["product_name", "unit", "currency"], how="left").sort_values("freshness_sec")
    board["age"] = (board["freshness_sec"] / 60).round(0).astype(int).astype(str) + "m"
    board["vs VWAP"] = board.apply(
        lambda r: "▲" if (r["vwap"] == r["vwap"] and r["last_price"] >= r["vwap"]) else "▼", axis=1)
    disp = board[["product_name", "last_price", "currency", "unit", "vs VWAP", "age", "is_stale"]]
    sty = (disp.style
           .map(lambda v: "color:#34d399;font-weight:700" if v == "▲" else "color:#fb7185;font-weight:700",
                subset=["vs VWAP"])
           .format({"last_price": "{:,.2f}"}))
    st.dataframe(sty, width="stretch", hide_index=True, height=380)

# ── Forward curve (full width) ──────────────────────────────────────
with st.container(border=True):
    st.markdown("##### 🔮 FORWARD CURVE")
    opts = {f"{r['product_name']} · {r['unit']} · {r['currency']}":
            (r["product_name"], r["unit"], r["currency"]) for r in lat.to_dict("records")}
    pick = st.selectbox("Instrument", list(opts), label_visibility="collapsed")
    name, unit, cur = opts[pick]
    curve = analytics.forward_curve(df, name, unit=unit, currency=cur)
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
    head = st.columns([4, 1])
    head[0].markdown("##### 📨 INBOX · 6 unread")
    digest = head[1].button("🧠 AI digest")
    st.caption("Client & counterparty mail. Asset is locked from the text (gazetteer); the chip is "
               "the deterministic keyword signal.")
    for who, t, subj, body in MOCK_EMAILS:
        asset = copilot._find_product(body, df) or "—"
        sent = copilot._naive_sentiment(body)
        st.markdown(
            f'<div class="eml unread"><div>🔵</div>'
            f'<div><div class="who">{who}</div><div class="subj">{subj}</div>'
            f'<div class="snip">{body[:96]}…</div></div>'
            f'<div class="meta">{t}<br><span class="chip {_SENT_CLS[sent]}">{sent}</span> '
            f'<span class="chip neut">{asset}</span></div></div>', unsafe_allow_html=True)
    bull = sum(copilot._naive_sentiment(b) == "Bullish" for *_, b in MOCK_EMAILS)
    bear = sum(copilot._naive_sentiment(b) == "Bearish" for *_, b in MOCK_EMAILS)
    st.caption(f"Net read: 🟢 {bull} bullish · 🔴 {bear} bearish across 6 unread.")
    if digest:
        with st.spinner("Summarizing…"):
            res = copilot.summarize_inbox("\n".join(b for *_, b in MOCK_EMAILS), df)
        st.info(f"**AI digest** — {res['summary']}")
