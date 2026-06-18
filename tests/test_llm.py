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
