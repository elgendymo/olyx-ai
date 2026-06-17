"""Single tuning surface — the trader's calibration knob (review 5A).

Every threshold lives here so Jasper can tune dislocation sensitivity, the volume
floor, lookback, etc. without hunting through analytics code.
"""
import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Config:
    # ── feed / ingestion ──
    base_url: str = os.environ.get(
        "FEED_BASE_URL", "https://renewable-price-feed-mock.onrender.com"
    )
    request_timeout: float = 10.0       # seconds per HTTP attempt (small endpoints)
    bulk_timeout: float = 120.0         # /feed/bulk is ~50k chunked rows; cold Render is slow
    max_retries: int = 4                # total attempts before giving up
    backoff_base: float = 0.5           # exp backoff: base * 2**attempt (+ jitter)
    backoff_cap: float = 8.0            # max single sleep
    cache_ttl: float = 300.0            # parquet cache considered fresh for N seconds
    latest_limit: int = 100             # /feed/latest max
    price_min: float = 0.01             # reject sub-cent prices (round to 0.00 at 2dp = misleading)
    price_max: float = 1e9              # reject absurd prices (overflow guard; nautilus PRICE_MAX idea)
    volume_max: float = 1e7             # reject absurd volumes (weight overflow guard)

    # ── analytics (used Phase 3) ──
    dislocation_pct: float = 0.02       # source-disagreement band (2%)
    disagreement_window_hours: float = 48.0  # only compare CONTEMPORANEOUS source quotes (not drift)
    zscore_n: float = 3.0               # std-devs from rolling mean to flag
    min_volume: float = 50.0            # tradeable-signal floor (MT/units) — gates noise
    mad_k: float = 5.0                  # outlier band: drop prices > k·(scaled MAD) from instrument median
    circuit_breaker_pct: float = 0.50   # cross-source fat-finger kill: AUTO-DROP only catastrophic ticks
    #                                     (>50% off peer consensus). Real renewable dislocations top out
    #                                     ~30%; true fat-fingers were 800%+ — 20% ate genuine opportunities
    #                                     AND dropped good ticks via a junk-contaminated median. Subtler
    #                                     anomalies are FLAGGED (⚠ suspect / dislocation queue), not dropped.
    lookback_days: int = 90             # window for stats/curves (13A: slice, don't use all 50k)
    curve_horizons: tuple = (30, 60, 90)
    stale_after: float = 172800.0       # 48h freshness threshold (vs robust feed_now — C2). Instruments
    #                                     quote daily/weekly, so a 1h gate flagged ~all of them (noise).


CONFIG = Config()
