"""Magic Spyglass — broker-edge dashboard for Jasper (OLYX).

Thin presentation layer over the tested engines: feed (ingest+validate), analytics (VWAP /
dislocations / curve), copilot (compute->verify->narrate). All numbers come from the deterministic
layer; the UI never computes. Analytics are cached on a cheap token (bulk fetch time), NOT by
hashing the 50k frame (14A). Data integrity lives upstream — this file just renders it.
"""
import plotly.graph_objects as go
import streamlit as st

import analytics
import copilot
import feed
import llm
from config import CONFIG

st.set_page_config(page_title="Magic Spyglass", page_icon="🔭", layout="wide")
if "chat" not in st.session_state:                       # C3: survives reruns/tab switches
    st.session_state["chat"] = []


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


def _instrument_curve(df, name, unit, currency):
    return analytics.forward_curve(df, name, unit=unit, currency=currency)


# ── header ──────────────────────────────────────────────────────────
df, token = load_bulk()
st.title("🔭 Magic Spyglass")
st.caption("Valid pricing dislocations, surfaced. Noise, ignored.")

h = llm_health()
hb = "🟢" if h.get("ok") else "🔴"
top = st.columns([2, 2, 2, 1])
if df.empty:
    st.error("Feed unreachable and no cached data. (fail-silent: nothing fabricated)")
    st.stop()
now = df["timestamp"].max()
lat = view_latest(token)
top[0].metric("Instruments", lat.shape[0])
top[1].metric("Newest packet (UTC)", now.strftime("%m-%d %H:%M"))
top[2].metric("Stale", int(lat["is_stale"].sum()))
top[3].caption(f"{hb} {h.get('provider')}\n\n`{h.get('model')}`")
if st.button("↻ Refresh feed"):
    load_bulk.clear()
    st.rerun()

pulse, opps, inbox, cop = st.tabs(["Pulse", "Opportunities", "Inbox", "Copilot"])

# ── Pulse ───────────────────────────────────────────────────────────
with pulse:
    st.subheader("Market Pulse")
    options = {f"{r['product_name']} · {r['unit']} · {r['currency']}":
               (r["product_name"], r["unit"], r["currency"]) for r in lat.to_dict("records")}
    pick = st.selectbox("Instrument", list(options))
    name, unit, cur = options[pick]
    curve = _instrument_curve(df, name, unit, cur)

    fig = go.Figure()
    if curve.get("status") == "ok":
        hx = [p["date"] for p in curve["history"]]
        hy = [p["price"] for p in curve["history"]]
        fig.add_scatter(x=hx, y=hy, mode="lines", name="daily price", line=dict(color="#22d3ee"))
        if "vwap" in curve:
            fig.add_hline(y=curve["vwap"], line_dash="dot", line_color="#94a3b8",
                          annotation_text=f"VWAP {curve['vwap']}")
        px = [p["date"] for p in curve["projections"]]
        py = [p["price"] for p in curve["projections"]]
        fig.add_scatter(x=[hx[-1]] + px, y=[hy[-1]] + py, mode="lines+markers",
                        name="projection", line=dict(color="#f59e0b", dash="dash"))
        verdict = copilot._verdict_line(curve)
        st.markdown(f"**{verdict}**")
    else:
        st.info(f"No forward curve: {curve.get('reason', 'insufficient data')}")
    fig.update_layout(template="plotly_dark", height=320, margin=dict(l=8, r=8, t=8, b=8),
                      paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)")
    st.plotly_chart(fig, width="stretch")

    st.caption("Latest quote per instrument — age is minutes behind the newest packet (C2).")
    show = lat.copy()
    show["age_min"] = (show["freshness_sec"] / 60).round(1)
    st.dataframe(show[["product_name", "last_price", "currency", "unit", "source", "age_min", "is_stale"]],
                 width="stretch", hide_index=True)

# ── Opportunities ───────────────────────────────────────────────────
with opps:
    st.subheader("Opportunity Spotter")
    st.caption("Source disagreements & z-score moves, **volume-gated**: a 2% move on 1 MT is noise; "
               "on 500 MT it's a signal. Sorted tradeable-first.")
    dis = view_dislocations(token)
    if dis.empty:
        st.success("No pricing dislocations above the calibration band right now.")
    else:
        d = dis.copy()
        d["priority"] = d["tradeable"].map({True: "🔴 tradeable", False: "⚪ low-vol"})
        d["signal"] = d.apply(lambda r: f"{round(r['magnitude'] * 100, 2)}% spread"
                              if r["type"] == "source_disagreement" else f"{r['magnitude']}σ", axis=1)
        st.dataframe(d[["priority", "product_name", "currency", "type", "signal",
                        "latest_price", "volume", "n_sources", "detail"]],
                     width="stretch", hide_index=True)

# ── Inbox ───────────────────────────────────────────────────────────
with inbox:
    st.subheader("Inbox")
    st.caption("Paste unread messages. The asset name is locked deterministically; the model only "
               "summarizes sentiment — it cannot invent an instrument.")
    txt = st.text_area("Unread messages", height=140,
                       placeholder="Rotterdam port delays, UCO cargoes tight, sellers pulling offers…")
    if st.button("Summarize") and txt.strip():
        with st.spinner("Summarizing…"):
            res = copilot.summarize_inbox(txt, df)
        badge = {"Bullish": "🟢 Bullish", "Bearish": "🔴 Bearish", "Neutral": "⚪ Neutral"}[res["sentiment"]]
        st.markdown(f"**{badge}** · {res['n_messages']} message(s)"
                    + (f" · asset **{res['asset']}**" if res["asset"] else ""))
        st.write(res["summary"])

# ── Copilot ─────────────────────────────────────────────────────────
with cop:
    st.subheader("Broker Copilot")
    st.caption("Numbers are computed deterministically and the narration is number-grounded; the "
               "facts receipt below each answer is the source of truth.")
    for m in st.session_state["chat"]:
        with st.chat_message(m["role"]):
            st.write(m["content"])
            if m.get("facts"):
                tag = "🟢 verified narration" if m.get("used_llm") else "⚙️ deterministic"
                st.caption(tag)
                with st.expander("facts receipt"):
                    st.json(m["facts"])
    q = st.chat_input("e.g. Any arbitrage opportunities? · Is now a good time to sell HVO Class II?")
    if q:
        st.session_state["chat"].append({"role": "user", "content": q})
        with st.spinner("Computing & narrating…"):
            res = copilot.answer(q, df)
        st.session_state["chat"].append({"role": "assistant", "content": res["answer"],
                                         "facts": res["facts"], "used_llm": res["used_llm"]})
        st.rerun()
