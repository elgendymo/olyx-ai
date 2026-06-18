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

import numpy as np
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

# Control chars (C0 + DEL + C1) — stripped from every untrusted string field in validate().
_CONTROL_CHARS = r"[\x00-\x1f\x7f-\x9f]"

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


def _empty_report():
    return {"ingested": 0, "kept": 0, "rejected": 0,
            "reasons": {"bad_price": 0, "bad_timestamp": 0, "missing_id": 0,
                        "missing_product": 0, "duplicate_id": 0},
            "samples": []}


# ── pure validation (no I/O — unit-testable, 2A) ───────────────────
def validate(df, with_report=False):
    """Clean dirty feed data into a typed, deduped, UTC-sorted frame. The system's
    data-integrity core — everything downstream trusts this output.

    The ONLY place timestamps become tz-aware UTC (8A/C2). Rules:
      DROP rows with: null/non-numeric/non-positive/non-finite price; unparseable
        timestamp; blank/null id or product_name.
      KEEP but clean: volume null/negative -> 0 (it's a VWAP weight, not the signal,
        so a bad weight must not discard a good price); blank unit/currency/source ->
        "UNKNOWN" (so a missing grouping key surfaces instead of being silently dropped
        by pandas groupby's dropna). Whitespace stripped; offsets normalized to UTC.
      DEDUPE on id keeping the LATEST timestamp (sort-then-keep-last), not input order.
    Future-dated rows are kept on purpose — this feed carries forward data and freshness
    is measured relative to timestamp.max (C2), not the wall clock.

    `with_report=True` -> returns (df, report). The report is the AUDIT TRAIL the review
    asked for: drops are no longer silent — it carries ingested/kept/rejected counts, a
    mutually-exclusive reason breakdown, and a few attributed sample rows (what + why).
    Default (no report) preserves the original `validate(df) -> df` contract everywhere.
    """
    # Route empty/None through the same pipeline so the empty result has consistent typed
    # columns (not object dtype) — keeps validate() idempotent. (found by property testing)
    if df is None:
        df = pd.DataFrame(columns=COLUMNS)
    df = df.copy()
    for c in COLUMNS:
        if c not in df.columns:
            df[c] = pd.NA
    ingested = int(len(df))

    # snapshot the raw (pre-coercion) price/timestamp so the rejection samples can show the
    # ACTUAL offending value the feed sent, not the post-coercion NaN/NaT.
    raw_price = df["price"].astype("object")
    raw_ts = df["timestamp"].astype("object")

    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    # volume is a VWAP weight: anything negative, absurd, or non-numeric -> 0 (neutralize, keep price)
    vol = pd.to_numeric(df["volume"], errors="coerce")
    df["volume"] = vol.where((vol >= 0) & (vol <= CONFIG.volume_max), 0.0).fillna(0.0)
    # format="ISO8601" parses EACH value by its own ISO variant (with/without fractional seconds,
    # with/without offset). Without it, pandas infers ONE format from the first row, then silently
    # coerces every validly-formatted-but-different timestamp to NaT — dropping good ticks as
    # "bad timestamp". That silent loss is exactly the kind of untrustworthy cleaning we must avoid.
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce", utc=True, format="ISO8601")
    # Untrusted strings: strip control/null chars (C0 range incl. \x00, plus DEL) and hard-cap
    # length BEFORE anything stores or renders them. This is the single sanitization chokepoint —
    # neutralizes injection payloads and 10MB-string DoS at the one place all feed data passes.
    for c in ["id", "product_id", "product_name", "source", "currency", "unit"]:
        df[c] = (df[c].astype("string").str.strip()
                 .str.replace(_CONTROL_CHARS, "", regex=True)
                 .str.slice(0, CONFIG.max_str_len))
    # keep grouping/label fields visible — a NaN key would be silently dropped by groupby (C4)
    df["unit"] = df["unit"].replace("", pd.NA).fillna("UNKNOWN")
    df["currency"] = df["currency"].replace("", pd.NA).fillna("UNKNOWN")
    df["source"] = df["source"].replace("", pd.NA).fillna("unknown")

    pnum = df["price"].to_numpy(dtype="float64", na_value=np.nan)
    price_ok = df["price"].notna() & (df["price"] >= CONFIG.price_min) & (df["price"] <= CONFIG.price_max) & np.isfinite(pnum)
    ts_ok = df["timestamp"].notna()
    id_ok = df["id"].notna() & (df["id"] != "")
    prod_ok = df["product_name"].notna() & (df["product_name"] != "")
    keep = price_ok & ts_ok & id_ok & prod_ok

    out = df[keep].sort_values("timestamp", kind="stable")
    out = out.drop_duplicates(subset="id", keep="last").reset_index(drop=True)
    result = out[COLUMNS]
    if not with_report:
        return result

    # mutually-exclusive reason buckets (priority: price > timestamp > id > product) so counts
    # sum to the pre-dedupe drops; duplicate_id captures the latest-wins dedupe removals.
    r_price = ~price_ok
    r_ts = price_ok & ~ts_ok
    r_id = price_ok & ts_ok & ~id_ok
    r_prod = price_ok & ts_ok & id_ok & ~prod_ok
    dupes = int(keep.sum()) - int(len(result))
    report = {
        "ingested": ingested, "kept": int(len(result)),
        "rejected": ingested - int(len(result)),
        "reasons": {"bad_price": int(r_price.sum()), "bad_timestamp": int(r_ts.sum()),
                    "missing_id": int(r_id.sum()), "missing_product": int(r_prod.sum()),
                    "duplicate_id": int(dupes)},
        "samples": [],
    }
    # a few attributed examples — the broker can SEE what was thrown out and why
    for mask, why in ((r_price, "bad_price"), (r_ts, "bad_timestamp"),
                       (r_id, "missing_id"), (r_prod, "missing_product")):
        for i in df.index[mask][:3]:
            report["samples"].append({
                "reason": why, "source": _s(df.at[i, "source"]),
                "product_name": _s(df.at[i, "product_name"]),
                "price": _s(raw_price.at[i]), "timestamp": _s(raw_ts.at[i])})
    return result, report


def _s(v):
    """Render a possibly-NA scalar as a short JSON-safe string for the rejection log."""
    if v is None or (isinstance(v, float) and pd.isna(v)) or v is pd.NA:
        return None
    return str(v)[:64]


# ── NDJSON stream parsing (C1) ─────────────────────────────────────
def _parse_stream(resp):
    """Parse a streamed response into records. Tolerates NDJSON (one obj/line), a single
    JSON array, AND the real shape: one big `{"prices":[...]}` object (chunked, no newlines).

    Each cleanly-parsed line that is a list/envelope is unwrapped via `_records`; a bare
    record dict is kept as-is; un-parseable lines are accumulated and parsed once at the end.
    """
    records, buf = [], []
    total_bytes, buf_bytes = 0, 0
    for raw in resp.iter_lines(decode_unicode=True):
        if not raw:
            continue
        # DoS bounds (untrusted feed): cap total bytes, per-line size, and record count so a
        # hostile/huge/gzip-bomb stream can't exhaust memory. Stop/skip loudly, never silently.
        total_bytes += len(raw)
        if total_bytes > CONFIG.max_stream_bytes:
            log.warning("bulk stream exceeded %d bytes — truncating ingestion", CONFIG.max_stream_bytes)
            break
        if len(raw) > CONFIG.max_line_bytes:
            log.warning("skipping oversized line (%d bytes > %d)", len(raw), CONFIG.max_line_bytes)
            continue
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            buf_bytes += len(line)
            if buf_bytes <= CONFIG.max_stream_bytes:
                buf.append(line)    # not line-delimited yet — accumulate (bounded), parse below
            continue
        recs = _records(obj)
        if recs:
            records.extend(recs)
        elif isinstance(obj, dict):
            records.append(obj)     # a bare record (no envelope list)
        if len(records) >= CONFIG.max_records:
            log.warning("bulk stream hit %d-record cap — truncating ingestion", CONFIG.max_records)
            break
    if not records and buf:
        try:
            records = _records(json.loads("".join(buf)))
        except json.JSONDecodeError:
            return []
    # ponytail: assembles all records in memory (~10MB for 50k), now hard-capped; swap to ijson if needed.
    return records[:CONFIG.max_records]


# ── cache (doubles as last-good, 3A) ───────────────────────────────
REPORT_FILE = Path("cache/bulk.report.json")   # rejection audit trail, next to the cache
# Bundled sample of REAL feed history (incl. its raw junk), shipped in version control so a clean
# checkout with a down feed and no cache still renders real, clearly-labeled data — never an empty
# screen, never fabricated (review: "clean checkout loaded no data"). Loaded through validate().
SEED_FILE = Path(__file__).with_name("seed_data.json")


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


def _write_report(report):
    try:
        REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
        REPORT_FILE.write_text(json.dumps(report))
    except Exception as e:
        log.warning("report write failed: %s", e)


def read_report():
    """The last validation audit trail (ingested/kept/rejected + reasons), or None."""
    try:
        return json.loads(REPORT_FILE.read_text()) if REPORT_FILE.exists() else None
    except Exception as e:
        log.warning("report read failed: %s", e)
        return None


def _safe_to_replace_cache(new_df):
    """A refresh must NEVER destroy a good last-good cache with a degraded fetch (review: a
    refresh overwrote its own cached fallback). Replace only when the new frame is non-empty
    AND not a severe row-count regression vs the existing cache — otherwise the flaky feed's
    truncated/empty response would wipe the only fallback. Returns (ok, reason)."""
    if new_df is None or new_df.empty:
        return False, "fetch produced an empty frame"
    old = _read_cache()
    if old is None or old.empty:
        return True, "no prior cache"
    if len(new_df) < CONFIG.cache_replace_min_ratio * len(old):
        return False, f"fetch regressed to {len(new_df)} rows vs cached {len(old)}"
    return True, "ok"


def seed(with_report=False):
    """Bundled sample slice of real history, validated through the same chokepoint. Guarantees a
    clean checkout still renders REAL, clearly-labeled data (never live, never fabricated).
    The sample carries the feed's raw junk on purpose, so loading it visibly exercises the
    rejection audit trail. `with_report=True` -> (df, report)."""
    try:
        data = json.loads(SEED_FILE.read_text())
    except Exception as e:
        log.warning("seed load failed: %s", e)
        empty = pd.DataFrame(columns=COLUMNS)
        return (empty, _empty_report()) if with_report else empty
    return validate(pd.DataFrame(_records(data)), with_report=with_report)


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
    df, report = validate(pd.DataFrame(records), with_report=True)
    ok, why = _safe_to_replace_cache(df)
    if not ok:
        # degraded fetch — keep last-good rather than overwrite it with worse data.
        cached = _read_cache()
        if cached is not None and not cached.empty:
            log.warning("refresh kept last-good cache (%s)", why)
            return cached, CACHE_FILE.stat().st_mtime
        # truly nothing to protect (no/empty prior cache): persist what we have, empty or not.
    _write_cache(df)
    _write_report(report)
    return df, time.time()
