"""Magic Spyglass — broker-edge dashboard for Jasper (OLYX).

Phase 1: runnable skeleton only. Four tabs, dark cards, chat state initialized.
Data/analytics/copilot logic lands in later phases.
"""
import streamlit as st

st.set_page_config(page_title="Magic Spyglass", page_icon="🔭", layout="wide")

# C3: chat history must survive reruns/tab switches — init once, here, from Phase 1.
if "chat" not in st.session_state:
    st.session_state["chat"] = []

st.title("🔭 Magic Spyglass")
st.caption("Valid pricing dislocations, surfaced. Noise, ignored.")

pulse, opps, inbox, copilot = st.tabs(["Pulse", "Opportunities", "Inbox", "Copilot"])

with pulse:
    st.subheader("Market Pulse")
    st.info("Phase 3 — live prices, freshness, and per-product charts.")

with opps:
    st.subheader("Opportunity Spotter")
    st.info("Phase 3 — ranked, volume-gated dislocations.")

with inbox:
    st.subheader("Inbox")
    st.info("Phase 4 — paste unread messages, get an LLM sentiment summary.")

with copilot:
    st.subheader("Broker Copilot")
    st.info("Phase 4/5 — ask about a product; answers cite the exact numbers.")
    for msg in st.session_state["chat"]:
        st.chat_message(msg["role"]).write(msg["content"])
