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
    # ── Pricing dislocations / arbitrage ────────────────────────────
    # Covers: "any arb?", "spread on UCO?", "mismatch", "source disagreement", z-score spikes
    ("dislocations", (
        "dislocat", "opportunit", "disagree", "mispric", "contradict",
        # arb / spread shorthand
        "arb", "arbitrage", "any arb", "trade on", "exploit",
        "spread", "basis", "basis risk", "calendar spread", "inter-market",
        "tight", "loose", "widening", "narrowing", "compression",
        # source conflict
        "source conflict", "cross", "mismatch", "diverge", "divergence",
        # z-score / outlier
        "outlier", "spike", "anomal", "z-score", "sigma", "deviation", "abnormal",
        # opportunity queue
        "opportunity queue", "queue", "attention", "flag", "alert",
    )),

    # ── Forward curve / timing ───────────────────────────────────────
    # Covers: "is SAF in contango?", "good time to sell?", "backwardation?", "project 90d"
    ("forward_curve", (
        "curve", "forecast", "forward", "project", "trajectory", "outlook",
        # timing questions
        "sell", "selling", "when to sell", "good time", "right time", "timing",
        "hold", "holding", "wait", "defer", "roll",
        # curve shape
        "contango", "backwardation", "term structure", "shape",
        "uptrend", "downtrend", "flat", "slope", "direction",
        # horizon
        "30 day", "60 day", "90 day", "30d", "60d", "90d",
        "next month", "next quarter", "q1", "q2", "q3", "q4",
        # projections
        "expect", "prediction", "target", "price target",
        "seasonal", "seasonality", "end of year", "eoy",
    )),

    # ── VWAP / volume-weighted average ──────────────────────────────
    # Covers: "VWAP for UCO", "volume weighted", "fair value"
    ("vwap", (
        "vwap", "weighted", "average price", "avg price",
        "volume weighted", "fair value", "benchmark", "reference price",
        "market average", "mid", "midpoint",
    )),

    # ── Latest prices / freshness / pulse ───────────────────────────
    # Covers: "what's UCO trading at?", "HVO quote?", "any fresh data?", highest/lowest
    ("freshness", (
        "fresh", "stale", "lag", "latest", "last price", "last quote",
        "quote", "pulse", "now", "current", "live", "real-time",
        # price queries
        "price", "pricing", "what is", "how much", "rate", "level",
        "bid", "offer", "ask", "indicative",
        # high / low
        "highest", "lowest", "most expensive", "cheapest",
        "maximum", "minimum", "max price", "min price",
        "top price", "bottom price", "best price",
        # product shorthands (brokers type these alone: "uco?", "rme?")
        "uco", "hvo", "rme", "pome", "tallow", "hbe", "hbe-o",
        "saf", "hefa", "saf hefa", "etha", "emethanol",
        "biomethane", "ttf", "go wind", "go solar", "carbon eua", "eua",
        "ucome", "glycerine", "crude glycerine",
        # data quality
        "data", "feed", "update", "old", "age", "behind", "delay",
        "how old", "when was", "last seen",
        # compliance products often asked by price
        "thg", "ere", "certificate", "credit",
    )),
]

_CACHE = {}

# For the curve "should I sell?" answer: the VERDICT is deterministic; the LLM only TRANSLATES the
# numbers into plain state and is FORBIDDEN from any directional/advice word (and we verify it, not
# just prompt it — tone the model sneaks in can't be caught by number-grounding).
_STATE_SYSTEM = (
    "Describe ONLY the current state of this single instrument using the numbers in FACTS "
    "(price, recent range, VWAP if present). One short sentence, plain and factual. FORBIDDEN: the "
    "words buy, sell, hold, long, short, recommend, should, bullish, bearish, rally, dump, or any "
    "trading advice or opinion on direction. Do not name other assets. Invent no numbers.")
_DIRECTIONAL = re.compile(
    r"\b(buy|sell|sold|hold|long|short|recommend\w*|should|must|bullish|bearish|rally|rallying|"
    r"dump|upside|downside|momentum|good time|wait)\b", re.I)
_VERDICT_LABEL = {"downtrend": "SELL SIGNAL", "uptrend": "HOLD SIGNAL", "flat": "NEUTRAL"}


def _find_product(query, df):
    q = query.lower()
    for p in df["product_name"].unique():
        if p and p.lower() in q:
            return p
    return None


def _single_asset(facts):
    """Name of the one instrument these facts describe, or None if they span >1 (or none).

    The LLM only narrates numbers when facts are scoped to a SINGLE asset — then a cross-wire is
    impossible because no other asset's numbers are in context (multi-asset cross-wire fix)."""
    if facts.get("intent") == "forward_curve":
        c = facts.get("curve") or {}
        return c["product_name"] if c.get("status") == "ok" else None
    rows = facts.get("instruments") or facts.get("items") or []
    names = {r.get("instrument") for r in rows}
    return next(iter(names)) if len(names) == 1 else None


def _mentions_foreign_asset(text, asset, known):
    """True if the prose names a known product other than `asset` (anti-drift / misattribution)."""
    t = text.lower()
    return any(p.lower() in t for p in known
              if p and p != asset and p.lower() not in (asset or "").lower())


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
            vw = analytics.vwap(df)                     # grounded VWAP for the same instrument
            row = vw[(vw["product_name"] == product) & (vw["unit"] == curve["unit"])
                     & (vw["currency"] == curve["currency"])]
            if len(row):
                v = row.iloc[0]["vwap"]
                if v == v:                              # not NaN
                    curve["vwap"] = round(float(v), 2)
        else:
            curve = fc                                  # status + reason only
        return intent, {"intent": intent, "product": product, "curve": curve}

    if intent == "vwap":
        vw = analytics.vwap(df)
        if product:
            vw = vw[vw["product_name"] == product]
        else:
            vw = vw.sort_values("vwap", ascending=False)
        items = [{"instrument": r["product_name"], "currency": r["currency"], "unit": r["unit"],
                  "vwap": (r["vwap"] if r["vwap"] == r["vwap"] else None), "n": int(r["n"])}
                 for r in vw.head(20).to_dict("records")]
        return intent, {"intent": intent, "instruments": items}

    if intent == "freshness":
        lat = analytics.latest_with_freshness(df)
        if product:
            lat = lat[lat["product_name"] == product]
        elif any(w in q for w in ("highest", "most expensive", "maximum", "max price", "biggest price")):
            lat = lat.sort_values("last_price", ascending=False)
        elif any(w in q for w in ("lowest", "cheapest", "minimum", "min price", "smallest price")):
            lat = lat.sort_values("last_price", ascending=True)
        else:
            lat = lat.sort_values("freshness_sec", ascending=True)   # freshest first (default)
        items = [{"instrument": r["product_name"], "price": r["last_price"], "currency": r["currency"],
                  "unit": r["unit"], "age_minutes": round(r["freshness_sec"] / 60, 1),
                  "stale": bool(r["is_stale"])} for r in lat.head(20).to_dict("records")]
        return intent, {"intent": intent, "instruments": items}

    return "help", {"intent": "help", "capabilities": [
        "latest price / quote for any instrument (UCO, HVO, SAF, RME, POME, tallow, biomethane, EUA…)",
        "highest / lowest price across all products",
        "pricing dislocations, arb opportunities, source spread, z-score spikes",
        "VWAP / volume-weighted average / fair value per instrument",
        "forward curve, contango/backwardation, sell-timing signal (30/60/90d)",
        "data freshness — stale flags, feed lag, last-seen age",
        "inbox summarisation — paste any broker message or cargo offer",
    ]}


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


def _trend_key(curve):
    rec = curve.get("recommendation", "")
    return "downtrend" if "downtrend" in rec else "uptrend" if "uptrend" in rec else "flat"


def _verdict_line(curve):
    """Deterministic, honest signal — NOT a command (it's a linear-fit projection, not an oracle)."""
    p = curve["projections"][-1]
    pct = (p["price"] - curve["current_price"]) / curve["current_price"] * 100 if curve["current_price"] else 0.0
    label = _VERDICT_LABEL[_trend_key(curve)]
    return f"[{label}] {_trend_key(curve)}, {pct:+.1f}% projected {p['horizon_days']}d."


def _curve_state_text(curve):
    """Deterministic state sentence — the fallback when the LLM translation is rejected/offline."""
    vw = f", VWAP {curve['vwap']}" if "vwap" in curve else ""
    return (f"{curve['product_name']} at {curve['current_price']} {curve['currency']}/{curve['unit']}"
            f"{vw}; range {curve['low']}–{curve['high']} over {curve['n_days']}d.")


def _answer_curve(query, facts, df):
    """Verdict (deterministic) + state translation (LLM, directional words FORBIDDEN and verified)."""
    c = facts["curve"]
    if c.get("status") != "ok":
        return _facts_to_text(facts), False, True            # no-data reason, deterministic
    verdict = _verdict_line(c)
    facts_json = json.dumps(facts, sort_keys=True, default=str)
    translation = llm.chat(_STATE_SYSTEM, f"FACTS:\n{facts_json}")
    known = set(df["product_name"].unique())
    ok = (translation and _is_grounded(translation, facts)
          and not _mentions_foreign_asset(translation, c["product_name"], known)
          and not _DIRECTIONAL.search(translation))       # enforce the word-ban, don't just prompt it
    if translation and not ok:
        log.warning("rejected curve translation (ungrounded/foreign/directional): %s", translation[:120])
    state = translation if ok else _curve_state_text(c)
    return f"{verdict} {state}", ok, True


def answer(query, df):
    """Route -> tight facts. Curve answers split verdict (deterministic) from translation (LLM, no
    directional words). Other single-asset facts are narrated + number-grounded + foreign-asset
    checked; multi-asset answers are deterministic (each number bound to its asset by construction)."""
    if df is None or df.empty:
        return {"answer": "No market data is loaded yet.", "facts": {}, "intent": "empty",
                "used_llm": False, "grounded": True, "asset": None}
    intent, facts = _route(query, df)
    facts_json = json.dumps(facts, sort_keys=True, default=str)
    key = (query.strip().lower(), facts_json)
    if key in _CACHE:
        return _CACHE[key]

    if intent == "forward_curve":
        ans, used_llm, grounded = _answer_curve(query, facts, df)
        res = {"answer": ans, "facts": facts, "intent": intent, "used_llm": used_llm,
               "grounded": grounded, "asset": facts.get("product")}
        _CACHE[key] = res
        return res

    fallback = _facts_to_text(facts)
    asset = _single_asset(facts)
    answer_text, used_llm, grounded = fallback, False, True
    if asset is not None:                                 # only narrate an isolated single-asset context
        narration = llm.chat(_SYSTEM, f"Question: {query}\n\nFACTS:\n{facts_json}")
        known = set(df["product_name"].unique())
        if narration and _is_grounded(narration, facts) and not _mentions_foreign_asset(narration, asset, known):
            answer_text, used_llm = narration, True
        elif narration:
            log.warning("rejected narration (ungrounded/foreign asset): %s", narration[:120])
            grounded = False                              # a narration was produced but failed verification
    res = {"answer": answer_text, "facts": facts, "intent": intent,
           "used_llm": used_llm, "grounded": grounded, "asset": asset}
    _CACHE[key] = res
    return res


# ── inbox sentiment mock (5.1) ──────────────────────────────────────
# Directional words only, matched on word boundaries — avoids "prices"->rise, "sellers"->sell,
# "offers"->offer false positives that plagued naive substring counting.
_BULL = ("rise", "rising", "rises", "surge", "surging", "shortage", "tight", "tightening",
         "rally", "rallying", "firmer", "strong", "squeeze", "spike")
_BEAR = ("fall", "falling", "drop", "dropping", "oversupply", "glut", "weak", "soft",
         "softer", "discount", "lower", "crash", "drift", "drifting")
_BULL_RE = re.compile(r"\b(?:" + "|".join(_BULL) + r")\b", re.I)
_BEAR_RE = re.compile(r"\b(?:" + "|".join(_BEAR) + r")\b", re.I)


def _naive_sentiment(text):
    b, s = len(_BULL_RE.findall(text)), len(_BEAR_RE.findall(text))
    if b == s:
        return "Neutral"
    return "Bullish" if b > s else "Bearish"


def summarize_inbox(text, df):
    """Asset name is LOCKED deterministically by the gazetteer (`_find_product`); the LLM only writes
    asset-free sentiment prose; Python assembles `"{asset} — {summary}"`. The model is structurally
    incapable of inventing an asset (e.g. "Crude Oil") because it never authors the name. No known
    instrument in the text -> skip the LLM entirely. Deterministic keyword sentiment stays authoritative."""
    text = (text or "").strip()
    n = len([ln for ln in text.splitlines() if ln.strip()])
    if not text:
        return {"summary": "Inbox empty.", "sentiment": "Neutral", "asset": None,
                "n_messages": 0, "used_llm": False}
    asset = _find_product(text, df) if (df is not None and not df.empty) else None
    naive = _naive_sentiment(text)
    if asset is None:                                     # gazetteer found no known instrument
        return {"summary": "Unrecognized instrument.", "sentiment": naive, "asset": None,
                "n_messages": n, "used_llm": False}
    system = ("Summarize the market sentiment of these broker messages in ONE short clause. "
              "Do NOT name any company, product, asset, price, or number — describe the drivers only.")
    out = llm.chat(system, text, max_tokens=120)
    known = set(df["product_name"].unique())
    if out and not _mentions_foreign_asset(out, asset, known):
        return {"summary": f"{asset} — {out}", "sentiment": naive, "asset": asset,
                "n_messages": n, "used_llm": True}
    return {"summary": f"{asset} — {n} message(s), {naive.lower()} keyword signal.",
            "sentiment": naive, "asset": asset, "n_messages": n, "used_llm": False}
