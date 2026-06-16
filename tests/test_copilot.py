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


# ── inbox sentiment ─────────────────────────────────────────────────
def test_inbox_naive_sentiment_offline(monkeypatch):
    monkeypatch.setattr(copilot.llm, "chat", lambda *a, **k: None)
    res = copilot.summarize_inbox("Port delays in Rotterdam, UCO supply tight, prices rising.\nStrong demand.")
    assert res["sentiment"] == "Bullish" and res["n_messages"] == 2 and res["used_llm"] is False


def test_inbox_bearish_and_empty(monkeypatch):
    monkeypatch.setattr(copilot.llm, "chat", lambda *a, **k: None)
    assert copilot.summarize_inbox("oversupply glut, prices falling, weak demand")["sentiment"] == "Bearish"
    empty = copilot.summarize_inbox("")
    assert empty["n_messages"] == 0 and empty["sentiment"] == "Neutral"


def test_inbox_uses_llm_summary_when_available(monkeypatch):
    monkeypatch.setattr(copilot.llm, "chat", lambda *a, **k: "Bullish on UCO. Sentiment: Bullish on UCO")
    res = copilot.summarize_inbox("UCO supply tight")
    assert res["used_llm"] is True and "Bullish" in res["summary"]
