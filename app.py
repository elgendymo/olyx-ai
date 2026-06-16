"""Magic Spyglass — broker-edge dashboard for Jasper (OLYX).

Single-glance layout (Briefy-style): persistent copilot in the sidebar, and a main column that
surfaces what needs attention NOW — no tabs, because a trading desk monitors, it doesn't browse.
Thin presentation over the tested engines (feed/analytics/copilot); the UI computes nothing.
Analytics are cached on a cheap token (bulk fetch time), not by hashing the 50k frame (14A).
"""
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
.block-container { padding-top: 2.2rem; max-width: 1400px; }
[data-testid="stVerticalBlockBorderWrapper"] {
  background: linear-gradient(180deg, rgba(34,211,238,0.045), rgba(0,0,0,0));
  border: 1px solid rgba(148,163,184,0.16) !important; border-radius: 14px;
}
[data-testid="stMarkdownContainer"] h5 {
  font-family: ui-monospace, SFMono-Regular, monospace; letter-spacing: .09em;
  font-size: .78rem; color: #7dd3fc; text-transform: uppercase; margin-bottom: .15rem;
}
[data-testid="stMetric"] { background: rgba(148,163,184,0.05); border-radius: 10px; padding: .4rem .75rem; }
section[data-testid="stSidebar"] { background: #0d1526; border-right: 1px solid rgba(148,163,184,0.12); }
[data-testid="stChatMessage"] { background: rgba(148,163,184,0.045); border-radius: 10px; }
[data-testid="stDataFrame"] { font-size: .85rem; }
.stButton > button { border: 1px solid rgba(34,211,238,0.4); border-radius: 8px; }
</style>""", unsafe_allow_html=True)


# ── cached data layer (token-keyed; df fetched once per TTL) ────────
@st.cache_data(ttl=CONFIG.cache_ttl, show_spinner="Loading market history…")
def load_bulk():
    return feed.bulk()                                   # (df, fetched_at)


@st.cache_data(show_spinner=False)
def view_latest(token):
    return analytics.latest_with_freshness(load_bulk()[0])


@st.cache_data(show_spinner=False)
def view_vwap(token):
    return analytics.vwap(load_bulk()[0])


@st.cache_data(show_spinner=False)
def view_dislocations(token):
    return analytics.dislocations(load_bulk()[0])


@st.cache_data(ttl=30, show_spinner=False)
def llm_health():
    return llm.health()


df, token = load_bulk()

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
    if st.button("↻ Refresh feed"):
        load_bulk.clear()
        st.rerun()

# ── main ────────────────────────────────────────────────────────────
st.title("🔭 Magic Spyglass")
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
        for r in trd.head(4).to_dict("records"):
            sig = (f"{round(r['magnitude'] * 100, 2)}% source spread" if r["type"] == "source_disagreement"
                   else f"{r['magnitude']}σ move")
            st.markdown(f"🔴 **{r['product_name']}** ({r['currency']}) — {sig} · "
                        f"{int(r['volume'])} vol · {r['detail']}")

# ── Pulse board + chart ─────────────────────────────────────────────
left, right = st.columns([1, 1])
with left:
    with st.container(border=True):
        st.markdown("##### 📈 PULSE")
        st.caption("Latest per instrument · age behind feed · ▲/▼ vs VWAP.")
        board = lat.merge(vw[["product_name", "unit", "currency", "vwap"]],
                          on=["product_name", "unit", "currency"], how="left")
        board["age"] = (board["freshness_sec"] / 60).round(0).astype(int).astype(str) + "m"
        board["vs VWAP"] = board.apply(
            lambda r: "▲" if (r["vwap"] == r["vwap"] and r["last_price"] >= r["vwap"]) else "▼", axis=1)
        st.dataframe(board[["product_name", "last_price", "currency", "vs VWAP", "age", "is_stale"]],
                     width="stretch", hide_index=True, height=300)

with right:
    with st.container(border=True):
        st.markdown("##### 🔮 FORWARD CURVE")
        opts = {f"{r['product_name']} · {r['unit']} · {r['currency']}":
                (r["product_name"], r["unit"], r["currency"]) for r in lat.to_dict("records")}
        pick = st.selectbox("Instrument", list(opts), label_visibility="collapsed")
        name, unit, cur = opts[pick]
        curve = analytics.forward_curve(df, name, unit=unit, currency=cur)
        if curve.get("status") == "ok":
            st.markdown(f"**{copilot._verdict_line(curve)}**")
            hx = [p["date"] for p in curve["history"]]
            hy = [p["price"] for p in curve["history"]]
            px = [p["date"] for p in curve["projections"]]
            py = [p["price"] for p in curve["projections"]]
            fig = go.Figure()
            fig.add_scatter(x=hx, y=hy, mode="lines", name="daily", line=dict(color="#22d3ee"))
            if "vwap" in curve:
                fig.add_hline(y=curve["vwap"], line_dash="dot", line_color="#94a3b8",
                              annotation_text=f"VWAP {curve['vwap']}")
            fig.add_scatter(x=[hx[-1]] + px, y=[hy[-1]] + py, mode="lines+markers",
                            name="projection", line=dict(color="#f59e0b", dash="dash"))
            fig.update_layout(template="plotly_dark", height=240, showlegend=False,
                              margin=dict(l=8, r=8, t=8, b=8),
                              paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)")
            st.plotly_chart(fig, width="stretch")
        else:
            st.info(f"No forward curve: {curve.get('reason', 'insufficient data')}")

# ── Inbox ───────────────────────────────────────────────────────────
with st.container(border=True):
    st.markdown("##### 📨 INBOX")
    st.caption("Paste unread messages. Asset is locked deterministically; the model only summarizes "
               "sentiment — it cannot invent an instrument.")
    txt = st.text_area("Unread messages", height=120, label_visibility="collapsed",
                       placeholder="Rotterdam port delays, UCO cargoes tight, sellers pulling offers…")
    if st.button("Summarize") and txt.strip():
        with st.spinner("Summarizing…"):
            res = copilot.summarize_inbox(txt, df)
        badge = {"Bullish": "🟢 Bullish", "Bearish": "🔴 Bearish", "Neutral": "⚪ Neutral"}[res["sentiment"]]
        st.markdown(f"**{badge}** · {res['n_messages']} message(s)"
                    + (f" · **{res['asset']}**" if res["asset"] else ""))
        st.write(res["summary"])
