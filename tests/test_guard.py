"""Cross-source circuit breaker + suspect flagging (defense-in-depth epic)."""
import pandas as pd

import analytics
from config import CONFIG


def _frame(rows):
    df = pd.DataFrame(rows)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    for c in ["unit", "currency"]:
        df[c] = df.get(c, "MT" if c == "unit" else "EUR")
    return df


def test_circuit_breaker_drops_fat_finger_and_attributes_source():
    now = "2026-06-10T08:00:00Z"
    df = _frame([
        # two sane sources ~1500, one fat-finger 10x
        {"product_name": "RME", "source": "broker_quote", "price": 1500.0, "volume": 10, "timestamp": now},
        {"product_name": "RME", "source": "exchange",     "price": 1510.0, "volume": 10, "timestamp": now},
        {"product_name": "RME", "source": "bad_vendor",   "price": 15000.0, "volume": 10, "timestamp": now},
    ])
    clean, dropped = analytics.guard(df)
    assert len(dropped) == 1
    assert dropped[0]["source"] == "bad_vendor"          # Story 4: attributed
    assert (clean["source"] == "bad_vendor").sum() == 0  # Story 1: killed


def test_legit_dislocation_inside_band_is_kept():
    now = "2026-06-10T08:00:00Z"
    df = _frame([
        {"product_name": "UCO", "source": "a", "price": 1000.0, "volume": 10, "timestamp": now},
        {"product_name": "UCO", "source": "b", "price": 1040.0, "volume": 10, "timestamp": now},  # +4%, real
    ])
    clean, dropped = analytics.guard(df)
    assert dropped == []                  # a tradeable dislocation is money, not bad data
    assert len(clean) == 2


def test_lone_source_passes_through():
    df = _frame([{"product_name": "HVO", "source": "a", "price": 9.99e8, "volume": 10,
                  "timestamp": "2026-06-10T08:00:00Z"}])
    clean, dropped = analytics.guard(df)
    assert dropped == [] and len(clean) == 1   # nothing to cross-check against


def test_suspect_flag_on_board():
    now = "2026-06-10T08:00:00Z"
    later = "2026-06-10T08:05:00Z"
    df = _frame([
        {"product_name": "FAME", "source": "a", "price": 800.0, "volume": 10, "timestamp": now},
        {"product_name": "FAME", "source": "b", "price": 805.0, "volume": 10, "timestamp": now},
        # newest tick is a MAD outlier but under the 20% breaker -> flagged, not dropped
        {"product_name": "FAME", "source": "c", "price": 905.0, "volume": 10, "timestamp": later},
    ])
    lat = analytics.latest_with_freshness(df)
    assert bool(lat.iloc[0]["suspect"]) is True


def test_fault_injection_spike_caught_drift_kept():
    """Chaos seam: a catastrophic spike trips the breaker (with saved_capital); a normal cross-source
    dislocation under the 50% line is kept (it's an opportunity, not bad data)."""
    now = "2026-06-10T08:00:00Z"
    base = _frame([
        {"product_name": "RME", "source": "a", "price": 1000.0, "volume": 10, "timestamp": now},
        {"product_name": "RME", "source": "b", "price": 1000.0, "volume": 10, "timestamp": now},
    ])
    spiked = analytics.inject_fault(base, "RME", "MT", "EUR", 1.0, volume=500)   # +100% = fat finger
    _, dropped = analytics.guard(spiked)
    inj = [d for d in dropped if d["source"] == "chaos_inject"]
    assert len(inj) == 1 and inj[0]["saved_capital"] > 0   # caught + € quantified

    drift = analytics.inject_fault(base, "RME", "MT", "EUR", 0.25, volume=500)  # 25% spread = real
    _, dropped2 = analytics.guard(drift)
    assert [d for d in dropped2 if d["source"] == "chaos_inject"] == []  # dislocation kept, not dropped


if __name__ == "__main__":
    test_circuit_breaker_drops_fat_finger_and_attributes_source()
    test_fault_injection_spike_caught_drift_kept()
    test_legit_dislocation_inside_band_is_kept()
    test_lone_source_passes_through()
    test_suspect_flag_on_board()
    print("guard self-check OK")
