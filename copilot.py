"""Broker copilot — deterministic compute, LLM narrates (the trust architecture).

The flow, every time: route the question to ONE analytics function (explicit keyword map, 7A),
compute the facts deterministically in pandas, then ask the LLM to narrate ONLY those numbers.
The LLM never computes and never sees the raw feed — only the finished facts dict. If the LLM is
unavailable, we fall back to a deterministic plain-text rendering of the same facts, so the copilot
always answers (REQ-BC, fail-silent). Answers are cached on (question, facts) so repeats are free.

`answer()` returns {answer, facts, intent, used_llm} — the UI shows `answer` AND `facts`, so Jasper
can verify the narration against the numbers it came from.
"""
import json
import logging

import analytics
import llm

log = logging.getLogger("copilot")

_SYSTEM = (
    "You are a terse commodity-broker assistant for a biofuel trader. Answer in 1-3 sentences. "
    "Use ONLY the numbers in the FACTS JSON and cite the exact figures inline. If FACTS has a "
    "status like 'insufficient_data' or 'no_data', say the data is insufficient and give the "
    "reason. Never invent numbers, products, currencies, or trends that are not in FACTS."
)

# intent -> keywords (first match wins; order matters: specific before generic)
_INTENTS = [
    ("dislocations", ("dislocat", "opportunit", "disagree", "spread", "arb", "mispric", "contradict")),
    ("forward_curve", ("curve", "forecast", "sell", "forward", "project", "trend", "trajectory", "outlook")),
    ("vwap", ("vwap", "weighted", "average", "avg")),
    ("freshness", ("fresh", "stale", "lag", "latest", "price", "quote", "pulse", "now")),
]

_CACHE = {}


def _find_product(query, df):
    """First product whose name appears in the query (case-insensitive), else None."""
    q = query.lower()
    for p in df["product_name"].unique():
        if p and p.lower() in q:
            return p
    return None


def _route(query, df):
    """(intent, facts) — facts is a JSON-serializable dict of deterministic results."""
    q = query.lower()
    intent = next((name for name, kws in _INTENTS if any(k in q for k in kws)), "help")
    product = _find_product(query, df)

    if intent == "dislocations":
        return intent, {"intent": intent,
                        "dislocations": analytics.dislocations(df).head(10).to_dict("records")}
    if intent == "forward_curve":
        product = product or (df["product_name"].mode().iloc[0] if not df.empty else None)
        return intent, {"intent": intent, "product": product,
                        "curve": analytics.forward_curve(df, product) if product else None}
    if intent == "vwap":
        vw = analytics.vwap(df)
        if product:
            vw = vw[vw["product_name"] == product]
        return intent, {"intent": intent, "vwap": vw.head(15).to_dict("records")}
    if intent == "freshness":
        lat = analytics.latest_with_freshness(df)
        if product:
            lat = lat[lat["product_name"] == product]
        return intent, {"intent": intent, "latest": lat.head(15).to_dict("records")}

    return "help", {"intent": "help", "capabilities": [
        "latest prices & freshness", "pricing dislocations / opportunities",
        "VWAP per instrument", "forward curve / is-now-a-good-time-to-sell"]}


def _facts_to_text(facts):
    """Deterministic plain-text rendering — the offline fallback AND the verifiable receipt."""
    intent = facts.get("intent")
    if intent == "dislocations":
        rows = facts["dislocations"]
        if not rows:
            return "No pricing dislocations above the calibration band right now."
        head = "; ".join(f"{r['product_name']} ({r['currency']}) {r['type']} "
                         f"mag {r['magnitude']}{' [tradeable]' if r['tradeable'] else ' [low-vol]'}"
                         for r in rows[:3])
        return f"{len(rows)} dislocation(s). Top: {head}."
    if intent == "forward_curve":
        c = facts.get("curve")
        if not c or c.get("status") != "ok":
            return f"No forward curve for {facts.get('product')}: {(c or {}).get('reason', 'no data')}."
        p = c["projections"][-1]
        return (f"{c['product_name']} ({c['currency']}/{c['unit']}): slope {c['slope_per_day']}/day "
                f"over {c['n_days']}d; {p['horizon_days']}d projection {p['price']} "
                f"(band {p['lo']}–{p['hi']}).")
    if intent == "vwap":
        rows = facts["vwap"]
        if not rows:
            return "No VWAP available for that selection."
        return "; ".join(f"{r['product_name']} ({r['currency']}) VWAP "
                         f"{r['vwap'] if r['vwap'] == r['vwap'] else 'n/a (zero volume)'}"
                         for r in rows[:5])
    if intent == "freshness":
        rows = facts["latest"]
        if not rows:
            return "No quotes loaded."
        return "; ".join(f"{r['product_name']} {r['last_price']} {r['currency']} "
                         f"({int(r['freshness_sec'])}s behind{', STALE' if r['is_stale'] else ''})"
                         for r in rows[:5])
    return "I can answer: " + ", ".join(facts.get("capabilities", []))


def answer(query, df):
    """Route -> compute facts -> LLM narrates (or deterministic fallback). Cached on (query, facts)."""
    if df is None or df.empty:
        return {"answer": "No market data is loaded yet.", "facts": {}, "intent": "empty",
                "used_llm": False}
    intent, facts = _route(query, df)
    facts_json = json.dumps(facts, sort_keys=True, default=str)
    key = (query.strip().lower(), facts_json)
    if key in _CACHE:
        return _CACHE[key]

    fallback = _facts_to_text(facts)
    narration = llm.chat(_SYSTEM, f"Question: {query}\n\nFACTS:\n{facts_json}")
    res = {"answer": narration or fallback, "facts": facts, "intent": intent,
           "used_llm": narration is not None}
    _CACHE[key] = res
    return res


# ── inbox sentiment mock (5.1) ──────────────────────────────────────
_BULL = ("up", "rise", "rising", "surge", "shortage", "delay", "tight", "rally", "bid", "demand", "buy")
_BEAR = ("down", "fall", "falling", "drop", "oversupply", "glut", "weak", "dump", "offer", "sell", "crash")


def _naive_sentiment(text):
    """Deterministic keyword tilt — the offline fallback and a sanity check on the LLM."""
    t = text.lower()
    b, s = sum(t.count(w) for w in _BULL), sum(t.count(w) for w in _BEAR)
    if b == s:
        return "Neutral"
    return "Bullish" if b > s else "Bearish"


def summarize_inbox(text):
    """Distill a block of unread messages into {summary, sentiment, n_messages, used_llm}.
    LLM summarizes; if unavailable, deterministic keyword sentiment + a count keep the card useful."""
    text = (text or "").strip()
    n = len([ln for ln in text.splitlines() if ln.strip()])
    if not text:
        return {"summary": "Inbox empty.", "sentiment": "Neutral", "n_messages": 0, "used_llm": False}
    naive = _naive_sentiment(text)
    system = ("You summarize a commodity broker's unread messages. Reply with ONE line: a 1-2 "
              "sentence summary, then ' Sentiment: Bullish|Bearish|Neutral on <asset>'. Be concrete.")
    out = llm.chat(system, text, max_tokens=200)
    return {"summary": out or f"{n} unread message(s); LLM offline — keyword sentiment only.",
            "sentiment": naive, "n_messages": n, "used_llm": out is not None}
