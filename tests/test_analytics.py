"""analytics.py tests — pure math + guards (6A) + C4 grouping + determinism.

Frames are built through feed.validate() so tests exercise the real typed/UTC pipeline
and stay DRY (one record builder).
"""
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

import analytics
import feed
from config import CONFIG

_BASE = datetime(2026, 1, 1, 8, tzinfo=timezone.utc)


def _ts(day):
    """ISO timestamp `day` days after the base — for building long, ordered series."""
    return (_BASE + timedelta(days=day)).isoformat().replace("+00:00", "Z")


def _rec(**over):
    base = dict(id="x", product_id="p", product_name="UCO", source="exchange",
                price=1000.0, currency="EUR", unit="MT",
                timestamp="2026-06-10T08:00:00Z", volume=100)
    base.update(over)
    return base


def _frame(rows):
    # give each row a unique id unless caller set one (dedupe would otherwise collapse them)
    recs = []
    for i, r in enumerate(rows):
        r = {**r}
        r.setdefault("id", f"r{i}")
        recs.append(_rec(**r))
    return feed.validate(pd.DataFrame(recs))


# ── empty-frame guards ──────────────────────────────────────────────
def test_empty_df_guards():
    empty = feed.validate(pd.DataFrame())
    assert analytics.latest_with_freshness(empty).empty
    assert analytics.vwap(empty).empty
    assert analytics.dislocations(empty).empty
    assert analytics.forward_curve(empty, "UCO")["status"] == "no_data"


# ── freshness ───────────────────────────────────────────────────────
def test_latest_freshness_relative_to_feed_max():
    df = _frame([
        {"price": 1000, "timestamp": "2026-06-10T08:00:00Z"},
        {"price": 1010, "timestamp": "2026-06-10T09:00:00Z"},  # newest packet = now
    ])
    out = analytics.latest_with_freshness(df)
    assert len(out) == 1                       # one instrument (same product/unit/currency)
    assert out.loc[0, "last_price"] == 1010.0  # latest by timestamp
    assert out.loc[0, "freshness_sec"] == 0.0  # it IS the newest packet


# ── VWAP ────────────────────────────────────────────────────────────
def test_vwap_is_volume_weighted():
    df = _frame([{"price": 100, "volume": 1}, {"price": 200, "volume": 3}])
    # (100*1 + 200*3) / 4 = 175
    assert analytics.vwap(df).loc[0, "vwap"] == 175.0


def test_vwap_zero_volume_is_nan_not_divzero():
    df = _frame([{"price": 100, "volume": 0}, {"price": 200, "volume": 0}])
    assert pd.isna(analytics.vwap(df).loc[0, "vwap"])


def test_vwap_ignores_price_outlier():
    # a single 999999 spike must not pull VWAP away from the ~100 cluster (MAD filter, §5.3)
    rows = [{"price": 100 + (i % 3), "volume": 100, "timestamp": _ts(i)} for i in range(8)]
    rows.append({"price": 999999, "volume": 100, "timestamp": _ts(8)})
    assert analytics.vwap(_frame(rows)).loc[0, "vwap"] < 200


def test_forward_curve_robust_to_spike():
    rows = [{"price": 100 + 5 * i, "timestamp": _ts(i)} for i in range(10)]
    rows.append({"price": 5_000_000, "timestamp": _ts(5)})   # outlier day
    fc = analytics.forward_curve(_frame(rows), "UCO", unit="MT", currency="EUR", horizons=(30,))
    assert fc["status"] == "ok"
    assert fc["projections"][0]["price"] < 1000          # spike rejected; trend stays sane


def test_inliers_keeps_all_when_uniform():
    s = pd.Series([100.0, 100.0, 100.0])
    assert analytics._inliers(s).all()


def test_vwap_groups_by_currency_C4():
    df = _frame([{"price": 100, "currency": "EUR"}, {"price": 130, "currency": "USD"}])
    out = analytics.vwap(df)
    assert len(out) == 2 and set(out["currency"]) == {"EUR", "USD"}


# ── dislocations ────────────────────────────────────────────────────
def test_dislocation_source_disagreement():
    df = _frame([
        {"source": "exchange", "price": 100, "volume": 500},
        {"source": "broker_quote", "price": 110, "volume": 500},  # ~9.5% spread > 2%
    ])
    res = analytics.dislocations(df)
    assert "source_disagreement" in set(res["type"])
    assert res[res["type"] == "source_disagreement"].iloc[0]["tradeable"]  # high volume


def test_dislocation_ignores_stale_source():
    # one fresh source + one source whose last quote is 10 days old -> not contemporaneous,
    # so NOT a disagreement (this was a real live false-positive: 28% "spreads" from drift)
    df = _frame([
        {"source": "exchange", "price": 100, "volume": 500, "timestamp": _ts(40)},
        {"source": "argus_mock", "price": 150, "volume": 500, "timestamp": _ts(30)},  # 10d stale
    ])
    res = analytics.dislocations(df)
    assert "source_disagreement" not in set(res["type"])


def test_dislocation_quiet_market_is_empty():
    df = _frame([{"price": 100 + (i % 2), "volume": 100, "timestamp": f"2026-06-1{i}T08:00:00Z"}
                 for i in range(5)])
    assert analytics.dislocations(df).empty


def test_dislocation_zscore_spike():
    rows = [{"price": 100, "volume": 200, "timestamp": _ts(i)} for i in range(20)]
    rows.append({"price": 300, "volume": 200, "timestamp": _ts(20)})  # spike, latest, ~4.5σ
    res = analytics.dislocations(_frame(rows))
    assert "zscore" in set(res["type"])


def test_dislocation_volume_gate_marks_low_volume_untradeable():
    rows = [{"price": 100, "volume": 200, "timestamp": _ts(i)} for i in range(20)]
    rows.append({"price": 300, "volume": 1, "timestamp": _ts(20)})    # same spike, on 1 unit
    res = analytics.dislocations(_frame(rows))
    spike = res[res["type"] == "zscore"].iloc[0]
    assert bool(spike["tradeable"]) is False


# ── forward curve ───────────────────────────────────────────────────
def test_forward_curve_positive_slope_projects_up():
    rows = [{"price": 100 + 10 * i, "timestamp": f"2026-06-0{i}T08:00:00Z"} for i in range(1, 8)]
    fc = analytics.forward_curve(_frame(rows), "UCO", unit="MT", currency="EUR", horizons=(30,))
    assert fc["status"] == "ok" and fc["slope_per_day"] > 0
    assert fc["projections"][0]["price"] > fc["history"][-1]["price"]
    assert "uptrend" in fc["recommendation"] and fc["current_price"] == fc["history"][-1]["price"]


def test_forward_curve_clamps_negative_projection_to_zero():
    # steep downtrend -> linear fit would project below zero; must clamp (commodity prices ≥ 0)
    rows = [{"price": max(200 - 30 * i, 1), "timestamp": _ts(i)} for i in range(7)]
    fc = analytics.forward_curve(_frame(rows), "UCO", unit="MT", currency="EUR", horizons=(90,))
    assert fc["status"] == "ok"
    assert fc["projections"][0]["price"] >= 0.0
    assert fc["projections"][0]["lo"] >= 0.0


def test_forward_curve_insufficient_points_returns_sentinel():
    df = _frame([{"price": 100, "timestamp": "2026-06-10T08:00:00Z"}])
    fc = analytics.forward_curve(df, "UCO")            # <2 distinct days
    assert fc["status"] == "insufficient_data" and "need" in fc["reason"]


def test_forward_curve_picks_most_traded_group_when_unspecified():
    rows = ([{"price": 100 + i, "currency": "EUR", "timestamp": f"2026-06-0{i}T08:00:00Z"} for i in range(1, 6)]
            + [{"price": 50, "currency": "USD", "timestamp": "2026-06-02T08:00:00Z"}])
    fc = analytics.forward_curve(_frame(rows), "UCO")
    assert fc["status"] == "ok" and fc["currency"] == "EUR"   # more rows


# ── determinism ─────────────────────────────────────────────────────
def test_determinism_identical_runs():
    rows = [{"price": 100 + 7 * i, "volume": 10 + i, "timestamp": f"2026-06-0{i}T08:00:00Z"}
            for i in range(1, 9)]
    df = _frame(rows)
    a = analytics.dislocations(df).to_dict()
    b = analytics.dislocations(df).to_dict()
    assert a == b
    assert analytics.forward_curve(df, "UCO") == analytics.forward_curve(df, "UCO")
