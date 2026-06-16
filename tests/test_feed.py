"""feed.py tests — pure validation + resilience (mocked transport, no network, no sleeps).

Covers review 9A/10A/11A: exhaustive validate(), backoff attempt-count + fail-silent,
NDJSON stream parsing, and the last-good-cache degradation contract (3A).
"""
import json

import pandas as pd
import pytest
import requests

import feed


# ── 11A: one builder for canonical records + edge variants ──────────
def _rec(**over):
    base = dict(id="1", product_id="p1", product_name="UCO", source="broker_quote",
                price=1500.0, currency="EUR", unit="MT",
                timestamp="2026-06-10T08:59:51.735Z", volume=43)
    base.update(over)
    return base


def _df(records):
    return pd.DataFrame(records)


# ── validate() ──────────────────────────────────────────────────────
def test_validate_drops_dirty_rows():
    df = feed.validate(_df([
        _rec(id="ok"),
        _rec(id="badprice", price=0),            # non-positive
        _rec(id="nullprice", price=None),        # null
        _rec(id="badts", timestamp="not-a-date"),
        _rec(id="", price=1200),                 # blank id
        _rec(id="noprod", product_name=""),      # blank product
    ]))
    assert list(df["id"]) == ["ok"]


def test_validate_dedupes_on_id_keep_last():
    df = feed.validate(_df([_rec(id="x", price=100), _rec(id="x", price=200)]))
    assert len(df) == 1 and df.loc[0, "price"] == 200


def test_validate_dedupe_keeps_latest_timestamp():
    # input order is OLDEST last on purpose — must keep the LATEST by timestamp, not by row order
    df = feed.validate(_df([
        _rec(id="x", price=200, timestamp="2026-06-10T10:00:00Z"),
        _rec(id="x", price=100, timestamp="2026-06-09T10:00:00Z"),
    ]))
    assert len(df) == 1 and df.loc[0, "price"] == 200


def test_validate_drops_absurd_price_overflow_guard():
    # found by Hypothesis: 1.8e306 is finite & >0 but overflows downstream -> must reject
    df = feed.validate(_df([_rec(id="huge", price=1.8e306), _rec(id="ok", price=1500)]))
    assert list(df["id"]) == ["ok"]


def test_validate_absurd_volume_neutralized_to_zero():
    df = feed.validate(_df([_rec(id="v", price=1500, volume=1e12)]))
    assert len(df) == 1 and df.loc[0, "volume"] == 0.0   # keep price, neutralize weight


def test_validate_drops_nonfinite_price():
    df = feed.validate(_df([_rec(id="inf", price=float("inf")),
                            _rec(id="nan", price=float("nan")),
                            _rec(id="ok", price=1500)]))
    assert list(df["id"]) == ["ok"]


def test_validate_negative_volume_clamped_not_dropped():
    df = feed.validate(_df([_rec(id="v", price=1500, volume=-5)]))
    assert len(df) == 1 and df.loc[0, "volume"] == 0.0   # price kept, weight neutralized


def test_validate_numeric_string_price_kept():
    df = feed.validate(_df([_rec(id="s", price="1500.5")]))
    assert len(df) == 1 and df.loc[0, "price"] == 1500.5


def test_validate_european_decimal_price_dropped_not_misparsed():
    # "1.524,74" must NOT silently become 1.52 — drop it rather than corrupt
    df = feed.validate(_df([_rec(id="eu", price="1.524,74")]))
    assert len(df) == 0


def test_validate_blank_unit_currency_filled_unknown():
    df = feed.validate(_df([_rec(id="u", unit="", currency=None)]))
    assert len(df) == 1
    assert df.loc[0, "unit"] == "UNKNOWN" and df.loc[0, "currency"] == "UNKNOWN"


def test_validate_strips_whitespace_in_product():
    df = feed.validate(_df([_rec(id="w", product_name="  UCO  ")]))
    assert df.loc[0, "product_name"] == "UCO"


def test_validate_future_timestamp_kept():
    df = feed.validate(_df([_rec(id="fut", timestamp="2099-01-01T00:00:00Z")]))
    assert len(df) == 1            # forward data is legit; freshness is feed-relative (C2)


def test_validate_offset_timestamp_normalized_to_utc():
    df = feed.validate(_df([_rec(id="o", timestamp="2026-06-10T10:00:00+02:00")]))
    assert df.loc[0, "timestamp"] == pd.Timestamp("2026-06-10T08:00:00Z")


def test_validate_mixed_currency_same_product_both_survive():
    df = feed.validate(_df([_rec(id="a", product_name="UCO", currency="EUR"),
                            _rec(id="b", product_name="UCO", currency="USD")]))
    assert set(df["currency"]) == {"EUR", "USD"}   # C4: must not collapse across currencies


def test_validate_timestamp_is_utc_aware():
    df = feed.validate(_df([_rec()]))
    assert str(df["timestamp"].dt.tz) == "UTC"


def test_validate_volume_nan_becomes_zero():
    df = feed.validate(_df([_rec(volume=None)]))
    assert df.loc[0, "volume"] == 0.0


def test_validate_empty_returns_schema_frame():
    df = feed.validate(pd.DataFrame())
    assert list(df.columns) == feed.COLUMNS and len(df) == 0


# ── backoff / fail-silent (10A) ─────────────────────────────────────
class _FakeResp:
    def __init__(self, status=200, payload=None, lines=None):
        self.status_code = status
        self.ok = status < 400
        self._payload = payload
        self._lines = lines or []
    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))
    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload
    def iter_lines(self, decode_unicode=False):
        yield from self._lines
    def close(self):
        pass


class _FakeSession:
    def __init__(self, behavior):
        self.behavior = behavior      # callable(call_index) -> _FakeResp | raises
        self.calls = 0
    def get(self, url, params=None, stream=False, timeout=None):
        i = self.calls
        self.calls += 1
        return self.behavior(i)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(feed.time, "sleep", lambda *_: None)


def test_get_retries_then_returns_none(monkeypatch):
    def always_fail(_):
        raise requests.ConnectionError("down")
    sess = _FakeSession(always_fail)
    monkeypatch.setattr(feed, "_SESSION", sess)
    assert feed._get("/health") is None
    assert sess.calls == feed.CONFIG.max_retries        # exhausted all attempts


def test_get_retries_on_500_then_succeeds(monkeypatch):
    def flaky(i):
        return _FakeResp(500) if i == 0 else _FakeResp(200)
    sess = _FakeSession(flaky)
    monkeypatch.setattr(feed, "_SESSION", sess)
    r = feed._get("/health")
    assert r is not None and sess.calls == 2


def test_latest_unwraps_prices_envelope(monkeypatch):
    # the real feed wraps records as {"prices":[...]} — regression for the live-smoke miss
    payload = {"prices": [_rec(id="a"), _rec(id="b")], "metadata": {"count": 2}}
    monkeypatch.setattr(feed, "_SESSION", _FakeSession(lambda i: _FakeResp(200, payload=payload)))
    df = feed.latest(5)
    assert sorted(df["id"]) == ["a", "b"]


def test_records_extracts_envelope_and_list():
    assert [r["id"] for r in feed._records({"prices": [_rec(id="z")]})] == ["z"]
    assert feed._records([_rec(id="q")])[0]["id"] == "q"
    assert feed._records({"nope": 1}) == []


def test_health_true_on_200(monkeypatch):
    monkeypatch.setattr(feed, "_SESSION", _FakeSession(lambda i: _FakeResp(200)))
    assert feed.health() is True


# ── NDJSON stream parsing (C1) ──────────────────────────────────────
def test_parse_stream_ndjson():
    lines = [json.dumps(_rec(id="a")), "", json.dumps(_rec(id="b"))]
    recs = feed._parse_stream(_FakeResp(lines=lines))
    assert [r["id"] for r in recs] == ["a", "b"]


def test_parse_stream_single_envelope_object():
    # the REAL /feed/bulk shape: one big {"prices":[...]} object, no newlines
    blob = json.dumps({"prices": [_rec(id="a"), _rec(id="b")], "metadata": {}})
    recs = feed._parse_stream(_FakeResp(lines=[blob]))
    assert [r["id"] for r in recs] == ["a", "b"]


def test_parse_stream_falls_back_to_json_array():
    blob = json.dumps([_rec(id="a"), _rec(id="b")])
    recs = feed._parse_stream(_FakeResp(lines=[blob[:10], blob[10:]]))  # split mid-token
    assert [r["id"] for r in recs] == ["a", "b"]


# ── last-good cache degradation (3A) ────────────────────────────────
def test_bulk_serves_last_good_on_failure(monkeypatch, tmp_path):
    cache = tmp_path / "bulk.parquet"
    feed.validate(_df([_rec(id="cached")])).to_parquet(cache, index=False)
    monkeypatch.setattr(feed, "CACHE_FILE", cache)
    monkeypatch.setattr(feed, "_cache_fresh", lambda: False)            # force a fetch attempt
    monkeypatch.setattr(feed, "_get", lambda *a, **k: None)             # fetch fails
    df, _ = feed.bulk()
    assert list(df["id"]) == ["cached"]


def test_bulk_parses_and_caches_on_success(monkeypatch, tmp_path):
    cache = tmp_path / "bulk.parquet"
    monkeypatch.setattr(feed, "CACHE_FILE", cache)
    monkeypatch.setattr(feed, "_cache_fresh", lambda: False)
    lines = [json.dumps(_rec(id="a")), json.dumps(_rec(id="b"))]
    monkeypatch.setattr(feed, "_get", lambda *a, **k: _FakeResp(lines=lines))
    df, _ = feed.bulk()
    assert sorted(df["id"]) == ["a", "b"] and cache.exists()
