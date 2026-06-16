"""copilot.py tests — routing, deterministic facts, the narrate-vs-fallback contract (12A).

We stub llm.chat (no live model): assert the copilot returns the deterministic facts dict, falls
back to facts-text verbatim when the LLM returns None, narrates when it doesn't, and caches.
"""
import pandas as pd
import pytest

import copilot
import feed


def _rec(**o):
    base = dict(id="x", product_id="p", product_name="UCO", source="exchange",
                price=1000.0, currency="EUR", unit="MT",
                timestamp="2026-06-10T08:00:00Z", volume=100)
    base.update(o)
    return base


def _frame(rows):
    recs = []
    for i, r in enumerate(rows):
        r = {**r}
        r.setdefault("id", f"r{i}")
        recs.append(_rec(**r))
    return feed.validate(pd.DataFrame(recs))


@pytest.fixture(autouse=True)
def _clear_cache():
    copilot._CACHE.clear()


# ── routing ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("query,intent", [
    ("any dislocations or arb opportunities?", "dislocations"),
    ("is now a good time to sell UCO?", "forward_curve"),
    ("what's the vwap for UCO?", "vwap"),
    ("how fresh is the latest price?", "freshness"),
    ("hello what can you do", "help"),
])
def test_routing_picks_intent(monkeypatch, query, intent):
    monkeypatch.setattr(copilot.llm, "chat", lambda *a, **k: None)
    df = _frame([{"price": 1000 + i, "timestamp": f"2026-06-0{i+1}T08:00:00Z"} for i in range(5)])
    assert copilot.answer(query, df)["intent"] == intent


def test_finds_product_in_query():
    df = _frame([{"product_name": "HVO Class IV"}, {"product_name": "UCO"}])
    assert copilot._find_product("sell HVO Class IV now?", df) == "HVO Class IV"


# ── compute≠narrate contract ────────────────────────────────────────
def test_falls_back_to_facts_text_when_llm_offline(monkeypatch):
    monkeypatch.setattr(copilot.llm, "chat", lambda *a, **k: None)        # LLM down
    df = _frame([{"price": 1234.5, "volume": 200}])
    res = copilot.answer("latest price?", df)
    assert res["used_llm"] is False
    assert "1234.5" in res["answer"]          # deterministic fallback still cites the number
    assert res["facts"]["intent"] == "freshness"


def test_uses_llm_narration_when_grounded(monkeypatch):
    monkeypatch.setattr(copilot.llm, "chat", lambda *a, **k: "UCO last traded at 1234.5 EUR.")
    df = _frame([{"price": 1234.5}])
    res = copilot.answer("latest price?", df)
    assert res["used_llm"] is True and res["grounded"] is True
    assert res["answer"] == "UCO last traded at 1234.5 EUR."


def test_rejects_ungrounded_narration(monkeypatch):
    # the model invents 9999.99 (not in facts) -> reject, fall back to deterministic text
    monkeypatch.setattr(copilot.llm, "chat", lambda *a, **k: "UCO is screaming up to 9999.99 EUR!")
    df = _frame([{"price": 1234.5}])
    res = copilot.answer("latest price?", df)
    assert res["used_llm"] is False and res["grounded"] is False
    assert "1234.5" in res["answer"] and "9999.99" not in res["answer"]


def test_grounding_ignores_dates(monkeypatch):
    monkeypatch.setattr(copilot.llm, "chat",
                        lambda *a, **k: "As of 2026-06-01, UCO sits at 1234.5 EUR.")
    res = copilot.answer("latest price?", _frame([{"price": 1234.5}]))
    assert res["grounded"] is True            # 2026/06/01 are dates, not bogus numbers


def test_grounding_tolerates_rounding(monkeypatch):
    monkeypatch.setattr(copilot.llm, "chat", lambda *a, **k: "UCO at 1165.3 EUR.")
    res = copilot.answer("latest price?", _frame([{"price": 1165.26}]))
    assert res["grounded"] is True            # 1165.3 ≈ 1165.26 within tolerance


def test_is_grounded_handles_negative_and_signless():
    facts = {"slope": -0.8712, "price": 1805.92}
    assert copilot._is_grounded("slope -0.8712 per day, price 1805.92", facts)
    assert copilot._is_grounded("declining 0.8712/day at 1805.92", facts)   # sign in words
    assert not copilot._is_grounded("price is 9999.0", facts)               # fabricated


def test_qualitative_answer_is_grounded(monkeypatch):
    monkeypatch.setattr(copilot.llm, "chat", lambda *a, **k: "Prices look broadly stable.")
    res = copilot.answer("latest price?", _frame([{"price": 1234.5}]))
    assert res["grounded"] is True            # no numbers to verify


# ── single-asset isolation (the cross-wire fix) ─────────────────────
def test_multi_asset_skips_llm_uses_deterministic(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(copilot.llm, "chat",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or "narr")
    df = _frame([{"product_name": "UCO", "price": 1000}, {"product_name": "POME", "price": 1300}])
    res = copilot.answer("latest prices?", df)            # no product named -> 2 instruments -> multi
    assert res["asset"] is None and res["used_llm"] is False and called["n"] == 0
    assert "UCO" in res["answer"] and "POME" in res["answer"]   # deterministic, correctly bound


def test_single_asset_isolation_blocks_crosswire(monkeypatch):
    df = _frame([{"product_name": "UCO", "price": 1165.26}, {"product_name": "POME", "price": 1300.0}])
    # ask about POME -> facts scoped to POME only; model tries to cite UCO's price
    monkeypatch.setattr(copilot.llm, "chat", lambda *a, **k: "POME is trading at 1165.26 EUR.")
    res = copilot.answer("latest price for POME?", df)
    assert res["asset"] == "POME"
    assert res["used_llm"] is False           # 1165.26 not in POME's isolated facts -> rejected
    assert "1165.26" not in res["answer"]


def test_single_asset_rejects_foreign_asset_name(monkeypatch):
    df = _frame([{"product_name": "UCO", "price": 1000}, {"product_name": "POME", "price": 1300}])
    monkeypatch.setattr(copilot.llm, "chat", lambda *a, **k: "UCO at 1000, and POME is moving too.")
    res = copilot.answer("latest price for UCO?", df)
    assert res["asset"] == "UCO" and res["used_llm"] is False   # names POME -> drift -> rejected


def test_answer_caches_on_query_and_facts(monkeypatch):
    calls = {"n": 0}
    def fake(*a, **k):
        calls["n"] += 1
        return "narrated"
    monkeypatch.setattr(copilot.llm, "chat", fake)
    df = _frame([{"price": 1000}])
    copilot.answer("latest price?", df)
    copilot.answer("latest price?", df)        # identical -> cache hit, no 2nd LLM call
    assert calls["n"] == 1


def test_empty_df_short_circuits(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(copilot.llm, "chat", lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    res = copilot.answer("price?", feed.validate(pd.DataFrame()))
    assert res["intent"] == "empty" and res["used_llm"] is False and called["n"] == 0


def test_facts_are_deterministic(monkeypatch):
    monkeypatch.setattr(copilot.llm, "chat", lambda *a, **k: None)
    df = _frame([{"price": 1000 + i, "volume": 100, "timestamp": f"2026-06-0{i+1}T08:00:00Z"} for i in range(5)])
    a = copilot.answer("vwap?", df)["facts"]
    copilot._CACHE.clear()
    b = copilot.answer("vwap?", df)["facts"]
    assert a == b


# ── inbox: asset locked by gazetteer, LLM writes asset-free prose ───
def test_inbox_injects_detected_asset(monkeypatch):
    monkeypatch.setattr(copilot.llm, "chat", lambda *a, **k: "supply looks tight and demand strong")
    df = _frame([{"product_name": "UCO"}])
    res = copilot.summarize_inbox("Rotterdam delays, UCO cargoes tight, demand strong.", df)
    assert res["asset"] == "UCO" and res["summary"].startswith("UCO —")
    assert res["used_llm"] is True and res["sentiment"] == "Bullish"


def test_inbox_unrecognized_instrument_skips_llm(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(copilot.llm, "chat",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or "x")
    df = _frame([{"product_name": "UCO"}])
    res = copilot.summarize_inbox("oversupply glut, prices falling, weak demand", df)  # no known asset
    assert res["asset"] is None and res["summary"] == "Unrecognized instrument." and called["n"] == 0


def test_inbox_rejects_foreign_asset_in_summary(monkeypatch):
    monkeypatch.setattr(copilot.llm, "chat", lambda *a, **k: "POME is rallying hard")  # ignored instruction
    df = _frame([{"product_name": "UCO"}, {"product_name": "POME"}])
    res = copilot.summarize_inbox("UCO supply tight", df)
    assert res["asset"] == "UCO" and res["used_llm"] is False and "POME" not in res["summary"]


def test_inbox_empty(monkeypatch):
    monkeypatch.setattr(copilot.llm, "chat", lambda *a, **k: None)
    res = copilot.summarize_inbox("", _frame([{"product_name": "UCO"}]))
    assert res["n_messages"] == 0 and res["sentiment"] == "Neutral"
