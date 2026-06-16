"""Property-based tests (Hypothesis) for the pure layer — data-integrity invariants.

Where example-based tests check specific dirty rows, these assert that for ANY input the
output ALWAYS satisfies the data contract. This is the automated version of the
"loop until every edge case is covered" work: Hypothesis hunts the cases we didn't enumerate.

Scope = pure layer only (validate + analytics). I/O, LLM, and UI are out (nondeterministic).
"""
import numpy as np
import pandas as pd
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import analytics
import feed

settings.register_profile("ci", deadline=None, max_examples=120,
                          suppress_health_check=[HealthCheck.too_slow])
settings.load_profile("ci")

_EPS = 0.01  # boundary tolerance for 2dp-rounded outputs

# ── strategies: deliberately messy raw feed records ─────────────────
_text = st.text(max_size=8)
_price = st.one_of(
    st.floats(allow_nan=True, allow_infinity=True, width=64),
    st.integers(min_value=-1_000_000, max_value=1_000_000_000),
    st.text(max_size=6), st.just("1.524,74"), st.just("1500.5"),
    st.none(), st.booleans(),
)
_ts = st.one_of(
    st.datetimes(min_value=pd.Timestamp("2000-01-01").to_pydatetime(),
                 max_value=pd.Timestamp("2050-01-01").to_pydatetime()).map(lambda d: d.isoformat() + "Z"),
    st.just("2026-06-10T08:00:00+02:00"), _text, st.none(),
)
_vol = st.one_of(st.integers(-1000, 1000),
                 st.floats(allow_nan=True, allow_infinity=True, width=64), st.none(), _text)
_label = st.one_of(_text, st.none(), st.sampled_from(["UCO", "HVO", "POME", "  UCO  ", ""]))

_record = st.fixed_dictionaries({
    "id": st.one_of(_text, st.none()),
    "product_id": st.one_of(_text, st.none()),
    "product_name": _label,
    "source": st.one_of(_text, st.none(), st.sampled_from(["exchange", "argus_mock", ""])),
    "price": _price,
    "currency": st.one_of(st.sampled_from(["EUR", "USD", "", None]), _text),
    "unit": st.one_of(st.sampled_from(["MT", "tCO2", "", None]), _text),
    "timestamp": _ts,
    "volume": _vol,
})
_frames = st.lists(_record, max_size=30).map(lambda recs: feed.validate(pd.DataFrame(recs)))


# ── validate(): output invariants for ANY input ─────────────────────
@given(_frames)
def test_validate_output_always_satisfies_contract(df):
    assert list(df.columns) == feed.COLUMNS
    if df.empty:
        return
    assert df["id"].is_unique
    p = df["price"].to_numpy(dtype="float64")
    assert np.all(np.isfinite(p)) and np.all(p > 0)
    assert df["timestamp"].notna().all() and str(df["timestamp"].dt.tz) == "UTC"
    assert df["timestamp"].is_monotonic_increasing
    assert (df["volume"] >= 0).all()
    for c in ["product_name", "unit", "currency", "source"]:        # no NaN grouping key (C4)
        assert df[c].notna().all()


@given(_frames)
def test_validate_is_idempotent(df):
    again = feed.validate(df)
    pd.testing.assert_frame_equal(df.reset_index(drop=True), again.reset_index(drop=True))


# ── analytics: invariants over validated frames ─────────────────────
@given(_frames)
def test_vwap_within_group_price_range(df):
    for _, r in analytics.vwap(df).iterrows():
        if pd.isna(r["vwap"]):
            continue
        grp = df[(df["product_name"] == r["product_name"]) & (df["unit"] == r["unit"])
                 & (df["currency"] == r["currency"])]
        assert grp["price"].min() - _EPS <= r["vwap"] <= grp["price"].max() + _EPS


@given(_frames)
def test_analytics_never_emit_nan_or_inf(df):
    dis = analytics.dislocations(df)
    for col in ["magnitude", "latest_price", "volume"]:
        assert np.all(np.isfinite(dis[col].to_numpy(dtype="float64"))) if len(dis) else True
    lat = analytics.latest_with_freshness(df)
    if len(lat):
        assert (lat["freshness_sec"] >= 0).all()                    # now = feed max (C2)
        lp = lat["last_price"].to_numpy(dtype="float64")
        assert np.all(np.isfinite(lp)) and np.all(lp > 0)


@given(_frames)
def test_forward_curve_finite_and_nonnegative(df):
    if df.empty:
        return
    for product in df["product_name"].unique()[:3]:
        fc = analytics.forward_curve(df, product)
        assert "status" in fc                    # always a dict now, never None
        if fc["status"] != "ok":
            assert "reason" in fc
            continue
        assert np.isfinite(fc["slope_per_day"])
        for pr in fc["projections"]:
            assert pr["price"] >= 0 and pr["lo"] >= 0 and pr["hi"] >= 0
            assert np.isfinite(pr["price"])


@given(_frames)
def test_analytics_deterministic(df):
    pd.testing.assert_frame_equal(analytics.vwap(df), analytics.vwap(df))
    pd.testing.assert_frame_equal(analytics.dislocations(df), analytics.dislocations(df))
