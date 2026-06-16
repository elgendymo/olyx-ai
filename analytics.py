"""Pure analytics over a validated price frame — where the product is won.

No I/O, no LLM, no wall clock. Every function:
  - groups by (product_name, unit, currency) — C4. Cross-currency/unit math is meaningless,
    and the live feed quotes every product in multiple currencies (60 instrument groups).
  - measures "now" as the feed's own `timestamp.max()` — C2, never the wall clock.
  - guards its degenerate cases explicitly (6A): empty frames, zero total volume (÷0),
    fewer than 2 points for a regression, single-source "disagreement".
  - rounds outputs at the boundary so two identical runs are byte-identical (determinism;
    float64 vectorized math internally, fixed-dp on the way out).
"""
import numpy as np
import pandas as pd

from config import CONFIG

GROUP = ["product_name", "unit", "currency"]


def feed_now(df):
    """Reference instant for all freshness — the newest packet, not the wall clock (C2)."""
    return df["timestamp"].max()


def _window(df, days, now=None):
    if df is None or df.empty:
        return df
    now = now if now is not None else feed_now(df)
    return df[df["timestamp"] >= now - pd.Timedelta(days=days)]


def _inliers(prices):
    """Boolean mask of non-outlier prices (median ± mad_k·scaled-MAD). Robust to single feed
    spikes (SRS §5.3 noise filtering). All-equal / tiny samples -> keep everything (the median
    row is always an inlier, so a filtered group never empties)."""
    med = prices.median()
    mad = (prices - med).abs().median()
    if pd.isna(mad) or mad == 0:
        return pd.Series(True, index=prices.index)
    return (prices - med).abs() <= CONFIG.mad_k * 1.4826 * mad   # 1.4826 -> MAD ≈ std


def latest_with_freshness(df):
    """Latest quote per instrument + seconds behind the newest packet (REQ-MP-03)."""
    cols = GROUP + ["last_price", "source", "timestamp", "volume", "freshness_sec", "is_stale"]
    if df is None or df.empty:
        return pd.DataFrame(columns=cols)
    now = feed_now(df)
    g = df.sort_values("timestamp").groupby(GROUP, as_index=False).last()
    g = g.rename(columns={"price": "last_price"})
    g["freshness_sec"] = (now - g["timestamp"]).dt.total_seconds().round(1)
    g["is_stale"] = g["freshness_sec"] > CONFIG.stale_after
    g["last_price"] = g["last_price"].round(2)
    return g[cols].sort_values("freshness_sec").reset_index(drop=True)


def vwap(df, window_days=None):
    """Volume-weighted average price per instrument over a trailing window (REQ-MP-02).

    Guard: a group whose total volume is 0 yields vwap=NaN (no ÷0, no fake price)."""
    cols = GROUP + ["vwap", "total_volume", "n"]
    if df is None or df.empty:
        return pd.DataFrame(columns=cols)
    w = _window(df, window_days or CONFIG.lookback_days)
    rows = []
    for keys, g in w.groupby(GROUP):
        g = g[_inliers(g["price"])]               # drop outlier ticks before weighting (§5.3)
        tv = float(g["volume"].sum())
        v = round(float((g["price"] * g["volume"]).sum() / tv), 2) if tv > 0 else np.nan
        rows.append({**dict(zip(GROUP, keys)), "vwap": v, "total_volume": tv, "n": int(len(g))})
    return pd.DataFrame(rows, columns=cols)


def dislocations(df, window_days=None):
    """Ranked pricing dislocations per instrument (REQ-OS). Two detectors, volume-gated:
      (a) source_disagreement — last price per source spreads beyond CONFIG.dislocation_pct,
      (b) zscore — latest price is >CONFIG.zscore_n std-devs from the window mean.
    `tradeable` = latest volume ≥ CONFIG.min_volume (a 2% move on 1 MT is noise; on 500 MT it's
    a signal). Sorted tradeable-first, then by magnitude — the opportunity queue."""
    cols = GROUP + ["type", "latest_price", "magnitude", "volume",
                    "tradeable", "n_sources", "detail"]
    if df is None or df.empty:
        return pd.DataFrame(columns=cols)
    wd = window_days or CONFIG.lookback_days
    now = feed_now(df)
    w = _window(df, wd, now)
    recent_cut = now - pd.Timedelta(hours=CONFIG.disagreement_window_hours)
    out = []
    for keys, g in w.groupby(GROUP):
        gk = dict(zip(GROUP, keys))
        g = g.sort_values("timestamp")
        latest = g.iloc[-1]
        lp, lv = float(latest["price"]), float(latest["volume"])
        tradeable = lv >= CONFIG.min_volume

        # (a) source disagreement — compare only CONTEMPORANEOUS quotes (recent window),
        # else we'd flag stale-vs-fresh price drift as disagreement. Needs ≥2 live sources.
        recent = g[g["timestamp"] >= recent_cut]
        per_src = recent.groupby("source").last()["price"]
        if len(per_src) >= 2:
            hi, lo, mid = float(per_src.max()), float(per_src.min()), float(per_src.median())
            pct = (hi - lo) / mid if mid else 0.0
            if pct > CONFIG.dislocation_pct:
                out.append({**gk, "type": "source_disagreement", "latest_price": round(lp, 2),
                            "magnitude": round(pct, 4), "volume": lv, "tradeable": tradeable,
                            "n_sources": int(len(per_src)),
                            "detail": f"{len(per_src)} sources spread {round(pct * 100, 2)}% "
                                      f"({round(lo, 2)}–{round(hi, 2)})"})

        # (b) z-score vs window — needs ≥3 points and non-zero spread
        prices = g["price"]
        if len(prices) >= 3:
            mean, std = float(prices.mean()), float(prices.std(ddof=0))
            if std > 0:
                z = (lp - mean) / std
                if abs(z) > CONFIG.zscore_n:
                    out.append({**gk, "type": "zscore", "latest_price": round(lp, 2),
                                "magnitude": round(z, 3), "volume": lv, "tradeable": tradeable,
                                "n_sources": int(g["source"].nunique()),
                                "detail": f"{round(z, 2)}σ from {wd}d mean {round(mean, 2)}"})

    res = pd.DataFrame(out, columns=cols)
    if res.empty:
        return res
    res["_m"] = res["magnitude"].abs()
    res = (res.sort_values(["tradeable", "_m"], ascending=[False, False])
              .drop(columns="_m").reset_index(drop=True))
    return res


def forward_curve(df, product, unit=None, currency=None, horizons=None, window_days=None):
    """Linear forward curve for ONE instrument: fit price ~ day over the window, project out
    each horizon (answers "is now a good time to sell?", 5.2). Always returns a dict with a
    `status`: "ok" with the curve, or "no_data"/"insufficient_data" with a human `reason` — so
    the UI and copilot can explain WHY there's no curve instead of silently showing nothing (6A).
    If unit/currency omitted, picks the most-traded group deterministically (count desc, then alpha)."""
    horizons = horizons or CONFIG.curve_horizons
    if df is None or df.empty:
        return {"status": "no_data", "product_name": product, "reason": "no data loaded"}
    sel = df[df["product_name"] == product]
    if unit is not None:
        sel = sel[sel["unit"] == unit]
    if currency is not None:
        sel = sel[sel["currency"] == currency]
    if sel.empty:
        return {"status": "no_data", "product_name": product,
                "reason": f"no quotes for {product!r} in the selected unit/currency"}
    if unit is None or currency is None:
        counts = sel.groupby(["unit", "currency"]).size().reset_index(name="n")
        counts = counts.sort_values(["n", "unit", "currency"], ascending=[False, True, True])
        unit, currency = counts.iloc[0]["unit"], counts.iloc[0]["currency"]
        sel = sel[(sel["unit"] == unit) & (sel["currency"] == currency)]

    w = _window(sel, window_days or CONFIG.lookback_days)
    w = w[_inliers(w["price"])]                    # reject outlier spikes before fitting (§5.3)
    daily = w.set_index("timestamp")["price"].resample("1D").mean().dropna()
    if len(daily) < 2:
        return {"status": "insufficient_data", "product_name": product, "unit": unit,
                "currency": currency, "n_days": int(len(daily)),
                "reason": f"only {len(daily)} day(s) in the {window_days or CONFIG.lookback_days}d "
                          "window; need ≥2 to project a trend"}

    base = daily.index[0]
    x = ((daily.index - base).days).to_numpy(dtype=float)
    y = daily.to_numpy(dtype=float)
    slope, intercept = np.polyfit(x, y, 1)
    band = float(np.std(y - (slope * x + intercept))) if len(y) > 2 else 0.0

    now = feed_now(df)
    projections = []
    for h in horizons:
        target = now + pd.Timedelta(days=int(h))
        p = slope * (target - base).days + intercept
        # clamp at 0 — a linear fit can extrapolate a commodity price below zero, which is
        # nonsensical and would mislead the broker.
        projections.append({"horizon_days": int(h), "date": target.date().isoformat(),
                            "price": round(max(float(p), 0.0), 2),
                            "lo": round(max(float(p - band), 0.0), 2),
                            "hi": round(max(float(p + band), 0.0), 2)})

    current = round(float(daily.iloc[-1]), 2)
    chg = (projections[-1]["price"] - current) / current if current else 0.0
    recommendation = ("downtrend — selling sooner likely beats waiting" if chg <= -0.02
                      else "uptrend — holding may capture upside" if chg >= 0.02
                      else "flat — no strong timing signal")
    return {
        "status": "ok", "product_name": product, "unit": unit, "currency": currency,
        "current_price": current, "slope_per_day": round(float(slope), 4),
        "recommendation": recommendation, "n_days": int(len(daily)),
        "low": round(float(daily.min()), 2), "high": round(float(daily.max()), 2),
        "history": [{"date": d.date().isoformat(), "price": round(float(v), 2)}
                    for d, v in daily.items()],
        "projections": projections,
    }
