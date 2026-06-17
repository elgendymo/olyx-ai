"""Ingestion hardening — the feed is untrusted third-party data (zero-trust)."""
from html import escape

import pandas as pd

import feed
from config import CONFIG


def _df(**over):
    base = dict(id="x", product_id="p", product_name="UCO", source="exchange",
                price=1000.0, currency="EUR", unit="MT",
                timestamp="2026-06-10T08:00:00Z", volume=10)
    base.update(over)
    return pd.DataFrame([base])


def test_validate_strips_control_chars_and_null_bytes():
    out = feed.validate(_df(product_name="UC\x00O\x07\x1f", source="ex\x9fchange"))
    assert out.iloc[0]["product_name"] == "UCO"        # null + control chars gone
    assert out.iloc[0]["source"] == "exchange"


def test_validate_caps_string_length():
    out = feed.validate(_df(product_name="A" * 10_000))
    assert len(out.iloc[0]["product_name"]) == CONFIG.max_str_len   # 10k-char DoS string truncated


def test_validate_keeps_injection_string_for_render_layer_to_escape():
    # validate does NOT mangle <>; the render layer html-escapes. Prove the escape neutralizes it.
    payload = '<img src=x onerror=alert(1)>'
    out = feed.validate(_df(product_name=payload))
    assert "<" not in escape(out.iloc[0]["product_name"])   # render boundary makes it inert
    assert escape(out.iloc[0]["product_name"]) == "&lt;img src=x onerror=alert(1)&gt;"


class _FakeResp:
    """Minimal streamed-response stand-in for _parse_stream."""
    def __init__(self, lines):
        self._lines = lines

    def iter_lines(self, decode_unicode=True):
        return iter(self._lines)


def test_parse_stream_caps_record_count():
    flood = ['{"id":"%d","product_name":"UCO","price":1,"timestamp":"2026-06-10T08:00:00Z"}' % i
             for i in range(CONFIG.max_records + 5_000)]
    recs = feed._parse_stream(_FakeResp(flood))
    assert len(recs) <= CONFIG.max_records          # hostile flood can't grow unbounded


def test_parse_stream_skips_oversized_line():
    huge = '{"id":"big","x":"' + "Z" * (CONFIG.max_line_bytes + 10) + '"}'
    good = '{"id":"ok","product_name":"UCO","price":1,"timestamp":"2026-06-10T08:00:00Z"}'
    recs = feed._parse_stream(_FakeResp([huge, good]))
    assert [r["id"] for r in recs] == ["ok"]        # 2MB+ line dropped, good record kept


if __name__ == "__main__":
    for fn in [test_validate_strips_control_chars_and_null_bytes, test_validate_caps_string_length,
               test_validate_keeps_injection_string_for_render_layer_to_escape,
               test_parse_stream_caps_record_count, test_parse_stream_skips_oversized_line]:
        fn()
    print("security self-check OK")
