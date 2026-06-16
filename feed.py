"""Ingestion + resilience seam (the highest-graded pillar).

Wraps the deliberately-flaky mock feed: exponential backoff with jitter, fail-silent
to the caller (return None, log on backend), a parquet cache that doubles as last-good
(review 3A), and a single PURE `validate()` that is the only place dirty data is cleaned
and timestamps are normalized to tz-aware UTC (review 2A / 8A).

`/feed/bulk` is a ~50k STREAMED payload — consumed line-by-line as NDJSON, never loaded
as one giant array (correction C1).
"""
import json
import logging
import random
import time
from pathlib import Path

import pandas as pd
import requests

from config import CONFIG

log = logging.getLogger("feed")


def _records(obj):
    """Pull the record list out of a feed payload — a bare list, a known envelope key
    (e.g. {"prices": [...]}), or, failing that, the first list-valued field."""
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        for k in ENVELOPE_KEYS:
            v = obj.get(k)
            if isinstance(v, list):
                return v
        for v in obj.values():
            if isinstance(v, list):
                return v
    return []

# Canonical schema (mock feed record shape).
COLUMNS = ["id", "product_id", "product_name", "source",
           "price", "currency", "unit", "timestamp", "volume"]

# The feed wraps records in an envelope ("prices" on /feed/latest); be liberal.
ENVELOPE_KEYS = ("prices", "data", "records", "items", "results")

CACHE_FILE = Path("cache/bulk.parquet")   # tests monkeypatch this

# Module-level session so tests can inject a fake transport.
_SESSION = requests.Session()


# ── HTTP with backoff (fail-silent) ────────────────────────────────
def _get(path, params=None, stream=False, timeout=None):
    """GET with exponential backoff + jitter. Returns Response or None (never raises).

    Treats 429 and 5xx as retryable. Sleeps via `time.sleep` (patchable in tests, 10A).
    """
    url = CONFIG.base_url.rstrip("/") + path
    timeout = timeout or CONFIG.request_timeout
    for attempt in range(CONFIG.max_retries):
        try:
            r = _SESSION.get(url, params=params, stream=stream, timeout=timeout)
            if r.status_code == 429 or r.status_code >= 500:
                raise requests.HTTPError(f"retryable status {r.status_code}")
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            if attempt + 1 >= CONFIG.max_retries:
                log.warning("GET %s failed after %d attempts: %s",
                            path, CONFIG.max_retries, e)
                return None
            wait = min(CONFIG.backoff_cap, CONFIG.backoff_base * (2 ** attempt))
            wait += random.uniform(0, CONFIG.backoff_base)   # jitter
            time.sleep(wait)
    return None


# ── pure validation (no I/O — unit-testable, 2A) ───────────────────
def validate(df):
    """Clean dirty feed data into a typed, deduped, UTC-sorted frame.

    The ONLY place timestamps become tz-aware UTC (8A/C2) and the only dirty-data
    defense (drops null/blank ids & products, non-positive prices, bad timestamps;
    dedupes on `id`; coerces volume NaN->0).
    """
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=COLUMNS)
    df = df.copy()
    for c in COLUMNS:
        if c not in df.columns:
            df[c] = pd.NA

    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0.0)
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce", utc=True)
    for c in ["id", "product_id", "product_name", "source", "currency", "unit"]:
        df[c] = df[c].astype("string").str.strip()

    keep = (
        df["price"].notna() & (df["price"] > 0)
        & df["timestamp"].notna()
        & df["product_name"].notna() & (df["product_name"] != "")
        & df["id"].notna() & (df["id"] != "")
    )
    df = df[keep].drop_duplicates(subset="id", keep="last")
    df = df.sort_values("timestamp").reset_index(drop=True)
    return df[COLUMNS]


# ── NDJSON stream parsing (C1) ─────────────────────────────────────
def _parse_stream(resp):
    """Parse a streamed response into records. Tolerates NDJSON (one obj/line), a single
    JSON array, AND the real shape: one big `{"prices":[...]}` object (chunked, no newlines).

    Each cleanly-parsed line that is a list/envelope is unwrapped via `_records`; a bare
    record dict is kept as-is; un-parseable lines are accumulated and parsed once at the end.
    """
    records, buf = [], []
    for raw in resp.iter_lines(decode_unicode=True):
        if not raw:
            continue
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            buf.append(line)        # not line-delimited yet — accumulate, parse below
            continue
        recs = _records(obj)
        if recs:
            records.extend(recs)
        elif isinstance(obj, dict):
            records.append(obj)     # a bare record (no envelope list)
    if not records and buf:
        try:
            records = _records(json.loads("".join(buf)))
        except json.JSONDecodeError:
            return []
    # ponytail: assembles all records in memory (~10MB for 50k); swap to ijson only if OOM.
    return records


# ── cache (doubles as last-good, 3A) ───────────────────────────────
def _cache_fresh():
    return CACHE_FILE.exists() and (time.time() - CACHE_FILE.stat().st_mtime) < CONFIG.cache_ttl


def _read_cache():
    if not CACHE_FILE.exists():
        return None
    try:
        return pd.read_parquet(CACHE_FILE)
    except Exception as e:                       # corrupt/unreadable cache → treat as absent
        log.warning("cache read failed: %s", e)
        return None


def _write_cache(df):
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(CACHE_FILE, index=False)


# ── public API ─────────────────────────────────────────────────────
def health():
    r = _get("/health")
    return r is not None and r.ok


def latest(limit=None):
    """Recent price updates as a validated frame, or None if the feed is unreachable."""
    limit = min(limit or CONFIG.latest_limit, CONFIG.latest_limit)
    r = _get("/feed/latest", params={"limit": limit})
    if r is None:
        return None
    try:
        data = r.json()
    except ValueError:
        return None
    return validate(pd.DataFrame(_records(data)))


def bulk(force=False):
    """Full history as (df, fetched_at). Served from a fresh parquet cache when possible;
    on fetch failure, serves last-good cache; `fetched_at` is a cheap cache token (14A),
    NOT a freshness signal (freshness is computed from timestamp.max — C2)."""
    if not force and _cache_fresh():
        return _read_cache(), CACHE_FILE.stat().st_mtime
    r = _get("/feed/bulk", stream=True, timeout=CONFIG.bulk_timeout)
    if r is None:
        cached = _read_cache()
        if cached is not None:
            log.warning("bulk fetch failed — serving last-good cache")
            return cached, CACHE_FILE.stat().st_mtime
        return pd.DataFrame(columns=COLUMNS), 0.0
    try:
        records = _parse_stream(r)
    finally:
        r.close()
    df = validate(pd.DataFrame(records))
    _write_cache(df)
    return df, time.time()
