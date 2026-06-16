"""Broker copilot — deterministic compute, LLM narrates, narration VERIFIED.

Flow: route (explicit keywords, 7A) -> compute a TIGHT facts dict (1-3 labeled numbers per intent,
not raw rows — less surface, less hallucination) -> LLM narrates ONLY those numbers -> we VERIFY the
narration only cites numbers that are actually in the facts (number-grounding). If the narration
invents a number, or the LLM is offline, we fall back to a deterministic rendering of the same facts.

The LLM is lipstick: the numbers are computed deterministically, and a hallucinated number is made
*undisplayable* by the grounding check. `answer()` returns {answer, facts, intent, used_llm,
grounded} and the UI shows `answer` AND `facts` (the receipt).
"""
import json
import logging
import re

import analytics
import llm

log = logging.getLogger("copilot")

_SYSTEM = (
    "You are a terse commodity-broker assistant for biofuel trader Jasper. Answer in 1-2 plain "
    "sentences. Use ONLY the numbers and fields in the FACTS JSON, and cite figures exactly as given "
    "with their unit/currency. Do NOT mention any asset, price, percentage, or date not present in "
    "FACTS. If FACTS has status 'insufficient_data'/'no_data', say the data is insufficient and give "
    "the reason. If FACTS has a 'recommendation', base your timing answer on it. Never compute or "
    "infer new numbers."
)

_INTENTS = [
    ("dislocations", ("dislocat", "opportunit", "disagree", "spread", "arb", "mispric", "contradict")),
    ("forward_curve", ("curve", "forecast", "sell", "forward", "project", "trend", "trajectory", "outlook")),
    ("vwap", ("vwap", "weighted", "average", "avg")),
    ("freshness", ("fresh", "stale", "lag", "latest", "price", "quote", "pulse", "now")),
]

_CACHE = {}


def _find_product(query, df):
    q = query.lower()
    for p in df["product_name"].unique():
        if p and p.lower() in q:
            return p
    return None


def _route(query, df):
    """(intent, tight facts dict). Facts hold only the few numbers needed to answer — labeled,
    unit-tagged, pre-selected — so the model copies rather than reconstructs (#2)."""
    q = query.lower()
    intent = next((name for name, kws in _INTENTS if any(k in q for k in kws)), "help")
    product = _find_product(query, df)

    if intent == "dislocations":
        res = analytics.dislocations(df)
        items = []
        for r in res.head(5).to_dict("records"):
            m = {"instrument": r["product_name"], "currency": r["currency"], "type": r["type"],
                 "price": r["latest_price"], "volume": round(r["volume"]),
                 "tradeable": bool(r["tradeable"]), "sources": int(r["n_sources"])}
            if r["type"] == "source_disagreement":
                m["spread_pct"] = round(r["magnitude"] * 100, 2)
            else:
                m["sigma"] = round(r["magnitude"], 2)
            items.append(m)
        return intent, {"intent": intent, "opportunities_found": int(len(res)), "items": items}

    if intent == "forward_curve":
        product = product or (df["product_name"].mode().iloc[0] if not df.empty else None)
        fc = analytics.forward_curve(df, product) if product else {"status": "no_data", "reason": "no product"}
        if fc.get("status") == "ok":
            curve = {k: fc[k] for k in ("status", "product_name", "unit", "currency",
                                        "current_price", "slope_per_day", "recommendation",
                                        "n_days", "low", "high", "projections")}
        else:
            curve = fc                                  # status + reason only
        return intent, {"intent": intent, "product": product, "curve": curve}

    if intent == "vwap":
        vw = analytics.vwap(df)
        if product:
            vw = vw[vw["product_name"] == product]
        items = [{"instrument": r["product_name"], "currency": r["currency"], "unit": r["unit"],
                  "vwap": (r["vwap"] if r["vwap"] == r["vwap"] else None), "n": int(r["n"])}
                 for r in vw.head(8).to_dict("records")]
        return intent, {"intent": intent, "instruments": items}

    if intent == "freshness":
        lat = analytics.latest_with_freshness(df)
        if product:
            lat = lat[lat["product_name"] == product]
        items = [{"instrument": r["product_name"], "price": r["last_price"], "currency": r["currency"],
                  "unit": r["unit"], "age_minutes": round(r["freshness_sec"] / 60, 1),
                  "stale": bool(r["is_stale"])} for r in lat.head(6).to_dict("records")]
        return intent, {"intent": intent, "instruments": items}

    return "help", {"intent": "help", "capabilities": [
        "latest prices & freshness", "pricing dislocations / opportunities",
        "VWAP per instrument", "forward curve / is-now-a-good-time-to-sell"]}


# ── number grounding (#1) ───────────────────────────────────────────
_DATE_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}|\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b|\b20\d\d\b|"
    # month names must be whole words (\b both sides) so "dec" doesn't eat "declining"
    r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
    r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b\.?\s+\d{1,2}",
    re.I)
_NUM_RE = re.compile(r"-?\d[\d,]*\.?\d*")


def _numbers_in(obj):
    """All numeric values inside a facts dict (booleans excluded)."""
    out = []
    if isinstance(obj, bool):
        return out
    if isinstance(obj, (int, float)):
        return [float(obj)]
    if isinstance(obj, dict):
        for v in obj.values():
            out += _numbers_in(v)
    elif isinstance(obj, list):
        for v in obj:
            out += _numbers_in(v)
    return out


def _is_grounded(text, facts):
    """True if every number in the narration matches a number in the facts (rounding tolerance).
    Dates are stripped first so '2026-06-01' doesn't read as the bogus numbers 2026/6/1. A purely
    qualitative answer (no numbers) is considered grounded — the trust risk is fabricated figures."""
    allowed = _numbers_in(facts)
    cleaned = _DATE_RE.sub(" ", text)
    for tok in _NUM_RE.findall(cleaned):
        tok = tok.strip(",.").replace(",", "")
        if not tok or tok == ".":
            continue
        try:
            n = float(tok)
        except ValueError:
            continue
        # compare on magnitude: sign is often carried in words ("0.87/day decline"), and the
        # regex can't reliably attach a leading minus. Sign-flip hallucinations are rare here.
        if not any(abs(abs(n) - abs(a)) <= max(0.5, 0.02 * abs(a)) for a in allowed):
            return False
    return True


def _facts_to_text(facts):
    """Deterministic rendering — the offline fallback AND the verifiable receipt."""
    intent = facts.get("intent")
    if intent == "dislocations":
        items = facts["items"]
        if not items:
            return "No pricing dislocations above the calibration band right now."
        head = "; ".join(
            f"{i['instrument']} ({i['currency']}) "
            f"{('spread ' + str(i['spread_pct']) + '%') if 'spread_pct' in i else (str(i['sigma']) + 'σ')}"
            f"{'' if i['tradeable'] else ' [low-vol]'}" for i in items[:3])
        return f"{facts['opportunities_found']} dislocation(s). Top: {head}."
    if intent == "forward_curve":
        c = facts.get("curve") or {}
        if c.get("status") != "ok":
            return f"No forward curve for {facts.get('product')}: {c.get('reason', 'no data')}."
        p = c["projections"][-1]
        return (f"{c['product_name']} ({c['currency']}/{c['unit']}) at {c['current_price']}; "
                f"{c['recommendation']}. {p['horizon_days']}d projection {p['price']} "
                f"(band {p['lo']}–{p['hi']}).")
    if intent == "vwap":
        items = facts["instruments"]
        if not items:
            return "No VWAP available for that selection."
        return "; ".join(f"{i['instrument']} ({i['currency']}) VWAP "
                         f"{i['vwap'] if i['vwap'] is not None else 'n/a (zero volume)'}"
                         for i in items[:5])
    if intent == "freshness":
        items = facts["instruments"]
        if not items:
            return "No quotes loaded."
        return "; ".join(f"{i['instrument']} {i['price']} {i['currency']} "
                         f"({i['age_minutes']}m old{', STALE' if i['stale'] else ''})" for i in items[:5])
    return "I can answer: " + ", ".join(facts.get("capabilities", []))


def answer(query, df):
    """Route -> tight facts -> LLM narrates -> VERIFY grounding -> fall back if ungrounded/offline."""
    if df is None or df.empty:
        return {"answer": "No market data is loaded yet.", "facts": {}, "intent": "empty",
                "used_llm": False, "grounded": True}
    intent, facts = _route(query, df)
    facts_json = json.dumps(facts, sort_keys=True, default=str)
    key = (query.strip().lower(), facts_json)
    if key in _CACHE:
        return _CACHE[key]

    fallback = _facts_to_text(facts)
    narration = llm.chat(_SYSTEM, f"Question: {query}\n\nFACTS:\n{facts_json}")
    grounded = bool(narration) and _is_grounded(narration, facts)
    if narration and not grounded:
        log.warning("rejected ungrounded narration: %s", narration[:120])
    res = {"answer": narration if grounded else fallback, "facts": facts, "intent": intent,
           "used_llm": grounded, "grounded": grounded}
    _CACHE[key] = res
    return res


# ── inbox sentiment mock (5.1) ──────────────────────────────────────
_BULL = ("up", "rise", "rising", "surge", "shortage", "delay", "tight", "rally", "bid", "demand", "buy")
_BEAR = ("down", "fall", "falling", "drop", "oversupply", "glut", "weak", "dump", "offer", "sell", "crash")


def _naive_sentiment(text):
    t = text.lower()
    b, s = sum(t.count(w) for w in _BULL), sum(t.count(w) for w in _BEAR)
    if b == s:
        return "Neutral"
    return "Bullish" if b > s else "Bearish"


def summarize_inbox(text):
    """LLM summary + deterministic naive keyword sentiment (authoritative label + offline fallback)."""
    text = (text or "").strip()
    n = len([ln for ln in text.splitlines() if ln.strip()])
    if not text:
        return {"summary": "Inbox empty.", "sentiment": "Neutral", "n_messages": 0, "used_llm": False}
    naive = _naive_sentiment(text)
    system = ("You summarize a commodity broker's unread messages in ONE line. Mention only assets "
              "that literally appear in the messages. End with ' Sentiment: Bullish|Bearish|Neutral'.")
    out = llm.chat(system, text, max_tokens=200)
    return {"summary": out or f"{n} unread message(s); LLM offline — keyword sentiment only.",
            "sentiment": naive, "n_messages": n, "used_llm": out is not None}
