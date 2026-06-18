"""llm.py tests — provider dispatch, request shape, and the fail-silent contract.

We CANNOT meaningfully unit-test generation quality (nondeterministic) — so we test the
plumbing and, above all, that chat() returns None on every failure path (the copilot's
degradation depends on it). One opt-in live test (BROKER_LIVE_LLM=1) pings the real model.
"""
import os

import pytest

import llm


class _Resp:
    def __init__(self, payload, status=200):
        self._p, self.status = payload, status
    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")
    def json(self):
        return self._p


def _fake_post(payload=None, raises=None, capture=None):
    def post(url, json=None, headers=None, timeout=None):
        if capture is not None:
            capture["url"], capture["body"], capture["headers"] = url, json, headers
        if raises:
            raise raises
        return _Resp(payload)
    return post


# ── ollama happy path + request shape ───────────────────────────────
def test_ollama_returns_content_and_shapes_request(monkeypatch):
    cap = {}
    monkeypatch.setattr(llm, "PROVIDER", "ollama")
    monkeypatch.setattr(llm, "MODEL", "llama3.1:8b")
    monkeypatch.setattr(llm.requests, "post",
                        _fake_post(payload={"message": {"content": " UCO is up 3%  "}}, capture=cap))
    out = llm.chat("you are terse", "how is UCO?")
    assert out == "UCO is up 3%"                       # stripped
    body = cap["body"]
    assert body["model"] == "llama3.1:8b" and body["stream"] is False
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["system", "user"]
    assert body["options"]["temperature"] == 0.0 and body["options"]["seed"] == 0


# ── gemini adapter (OpenAI-compatible Gemini endpoint) ──────────────
def test_gemini_returns_content_and_shapes_request(monkeypatch):
    cap = {}
    monkeypatch.setattr(llm, "PROVIDER", "gemini")
    monkeypatch.setattr(llm, "MODEL", "gemini-2.0-flash")
    monkeypatch.setattr(llm, "GEMINI_API_KEY", "AIza_xxx")
    monkeypatch.setattr(llm.requests, "post",
                        _fake_post(payload={"choices": [{"message": {"content": " UCO is up 3% "}}]},
                                   capture=cap))
    out = llm.chat("you are terse", "how is UCO?")
    assert out == "UCO is up 3%"
    assert cap["url"] == "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    assert cap["headers"]["Authorization"] == "Bearer AIza_xxx"
    body = cap["body"]
    assert body["model"] == "gemini-2.0-flash"
    assert [m["role"] for m in body["messages"]] == ["system", "user"]


def test_gemini_without_key_returns_none(monkeypatch):
    monkeypatch.setattr(llm, "PROVIDER", "gemini")
    monkeypatch.setattr(llm, "GEMINI_API_KEY", None)
    assert llm.chat("s", "u") is None


# ── provider auto-detection (local ollama vs hosted gemini) ─────────
def test_resolve_provider_explicit_env_wins():
    assert llm._resolve_provider("Anthropic", "AIza_xxx") == "anthropic"
    assert llm._resolve_provider("ollama", None) == "ollama"


def test_resolve_provider_auto_picks_gemini_when_key_present():
    # the Streamlit Cloud case: no Ollama daemon, a Gemini key in secrets -> route to Gemini.
    assert llm._resolve_provider(None, "AIza_xxx") == "gemini"


def test_resolve_provider_auto_defaults_to_ollama_locally():
    assert llm._resolve_provider(None, None) == "ollama"


def test_health_gemini_ok_pings_models(monkeypatch):
    monkeypatch.setattr(llm, "PROVIDER", "gemini")
    monkeypatch.setattr(llm, "GEMINI_API_KEY", "AIza_xxx")
    monkeypatch.setattr(llm.requests, "get",
                        lambda *a, **k: _Resp({"models": [{"name": "models/gemini-2.0-flash"}]}))
    h = llm.health()
    assert h["ok"] is True and h["provider"] == "gemini"


def test_health_gemini_not_ok_without_key(monkeypatch):
    monkeypatch.setattr(llm, "PROVIDER", "gemini")
    monkeypatch.setattr(llm, "GEMINI_API_KEY", None)
    assert llm.health()["ok"] is False


def test_health_gemini_not_ok_on_bad_key(monkeypatch):
    # a present-but-invalid key must read RED now (the old check only saw the env var).
    monkeypatch.setattr(llm, "PROVIDER", "gemini")
    monkeypatch.setattr(llm, "GEMINI_API_KEY", "bad")
    monkeypatch.setattr(llm.requests, "get", lambda *a, **k: _Resp({}, status=400))
    assert llm.health()["ok"] is False


# ── ping() diagnostic (surfaces the real reply or the API's error) ──
def test_ping_ok_returns_reply(monkeypatch):
    monkeypatch.setattr(llm, "PROVIDER", "gemini")
    monkeypatch.setattr(llm, "GEMINI_API_KEY", "AIza_xxx")
    monkeypatch.setattr(llm.requests, "post",
                        _fake_post(payload={"choices": [{"message": {"content": "OK"}}]}))
    ok, detail = llm.ping()
    assert ok is True and detail == "OK"


def test_ping_surfaces_http_error_body(monkeypatch):
    class _ErrResp:
        status_code = 400
        text = '{"error":{"message":"API key not valid"}}'
    def _raise_post(*a, **k):
        e = llm.requests.HTTPError("400")
        e.response = _ErrResp()
        raise e
    monkeypatch.setattr(llm, "PROVIDER", "gemini")
    monkeypatch.setattr(llm, "GEMINI_API_KEY", "bad")
    monkeypatch.setattr(llm.requests, "post", _raise_post)
    ok, detail = llm.ping()
    assert ok is False and "API key not valid" in detail


def test_ping_no_key_reports_skip(monkeypatch):
    monkeypatch.setattr(llm, "PROVIDER", "gemini")
    monkeypatch.setattr(llm, "GEMINI_API_KEY", None)
    ok, detail = llm.ping()
    assert ok is False and "missing API key" in detail


# ── fail-silent contract (the important part) ───────────────────────
def test_chat_returns_none_on_exception(monkeypatch):
    monkeypatch.setattr(llm, "PROVIDER", "ollama")
    monkeypatch.setattr(llm.requests, "post", _fake_post(raises=ConnectionError("refused")))
    assert llm.chat("s", "u") is None


def test_chat_returns_none_on_http_error(monkeypatch):
    monkeypatch.setattr(llm, "PROVIDER", "ollama")
    monkeypatch.setattr(llm.requests, "post", lambda *a, **k: _Resp({}, status=503))
    assert llm.chat("s", "u") is None             # raise_for_status throws -> caught -> None


def test_chat_returns_none_on_empty_content(monkeypatch):
    monkeypatch.setattr(llm, "PROVIDER", "ollama")
    monkeypatch.setattr(llm.requests, "post", _fake_post(payload={"message": {"content": "   "}}))
    assert llm.chat("s", "u") is None


def test_unknown_provider_returns_none(monkeypatch):
    monkeypatch.setattr(llm, "PROVIDER", "definitely-not-a-provider")
    assert llm.chat("s", "u") is None


def test_offline_provider_returns_none(monkeypatch):
    monkeypatch.setattr(llm, "PROVIDER", "offline")
    assert llm.chat("s", "u") is None


def test_anthropic_without_key_returns_none(monkeypatch):
    monkeypatch.setattr(llm, "PROVIDER", "anthropic")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert llm.chat("s", "u") is None


# ── health badge ────────────────────────────────────────────────────
def test_health_ollama_detects_model(monkeypatch):
    monkeypatch.setattr(llm, "PROVIDER", "ollama")
    monkeypatch.setattr(llm, "MODEL", "llama3.1:8b")
    monkeypatch.setattr(llm.requests, "get",
                        lambda *a, **k: _Resp({"models": [{"name": "llama3.1:8b"}]}))
    h = llm.health()
    assert h["ok"] is True and h["provider"] == "ollama"


# ── opt-in live smoke (skipped unless BROKER_LIVE_LLM=1) ──────────────
@pytest.mark.skipif(os.environ.get("BROKER_LIVE_LLM") != "1",
                    reason="set BROKER_LIVE_LLM=1 to hit the real local model")
def test_live_model_narrates():
    out = llm.chat("You are a terse trading assistant.",
                   "Say the number 42 back to me in a sentence.")
    assert out and "42" in out
