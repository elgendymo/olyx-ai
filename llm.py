"""Swappable LLM client — one function, many providers, fail-silent (Briefy's pattern).

Demo default is a LOCAL model via Ollama (no API key, no egress): `llama3.1:8b`, which
Briefy benchmarked as its "gold standard" small tool-caller. Swap provider/model with one
env var; cloud providers (anthropic, openai) are thin adapters that read their own key.

Critical design choice: this layer NARRATES, it never computes. The copilot hands it a
finished facts dict and asks for prose. That is deliberate — small local models reliably
mangle structured/JSON output (Briefy observed 3B models emitting junk JSON), but narrating
given numbers is well within their range. We never ask the model for a number or a tool call.

`chat()` returns the assistant text, or **None on any failure** (timeout, no key, model down,
bad provider). The contract is that None is normal: the caller degrades to showing the raw
facts. That is the whole resilience story for this layer.

Env:
  OLYX_LLM_PROVIDER  ollama (default) | anthropic | openai | offline
  OLYX_LLM_MODEL     override the model id (default per provider)
  OLLAMA_HOST        default http://localhost:11434
  OLYX_LLM_TIMEOUT   seconds (default 60 — local 8B is slow)
"""
import logging
import os

import requests

log = logging.getLogger("llm")

PROVIDER = os.environ.get("OLYX_LLM_PROVIDER", "ollama").lower()
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
TIMEOUT = float(os.environ.get("OLYX_LLM_TIMEOUT", "60"))

# qwen2.5:7b beat llama3.1:8b in our narration bake-off (correctly read the dislocation count;
# more articulate). Briefy preferred llama3.1:8b for TOOL-CALLING/JSON — different job. Swap via env.
_DEFAULT_MODEL = {"ollama": "qwen2.5:7b",
                  "anthropic": "claude-haiku-4-5-20251001",
                  "openai": "gpt-4o-mini"}
MODEL = os.environ.get("OLYX_LLM_MODEL") or _DEFAULT_MODEL.get(PROVIDER, "qwen2.5:7b")


# ── provider adapters (same shape; raw HTTP like Briefy) ────────────
def _ollama(system, user, max_tokens, temperature):
    body = {"model": MODEL, "stream": False,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            # temperature 0 + fixed seed = greedy/reproducible-ISH (NOT guaranteed — see README)
            "options": {"temperature": temperature, "num_predict": max_tokens, "seed": 0}}
    r = requests.post(f"{OLLAMA_HOST}/api/chat", json=body, timeout=TIMEOUT)
    r.raise_for_status()
    return (r.json().get("message", {}).get("content") or "").strip()


def _anthropic(system, user, max_tokens, temperature):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    r = requests.post("https://api.anthropic.com/v1/messages",
                      headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                               "content-type": "application/json"},
                      json={"model": MODEL, "max_tokens": max_tokens, "temperature": temperature,
                            "system": system, "messages": [{"role": "user", "content": user}]},
                      timeout=TIMEOUT)
    r.raise_for_status()
    return "".join(b.get("text", "") for b in r.json().get("content", [])).strip()


def _openai(system, user, max_tokens, temperature):
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        return None
    r = requests.post("https://api.openai.com/v1/chat/completions",
                      headers={"Authorization": f"Bearer {key}", "content-type": "application/json"},
                      json={"model": MODEL, "max_tokens": max_tokens, "temperature": temperature,
                            "messages": [{"role": "system", "content": system},
                                         {"role": "user", "content": user}]},
                      timeout=TIMEOUT)
    r.raise_for_status()
    return (r.json()["choices"][0]["message"]["content"] or "").strip()


def _offline(system, user, max_tokens, temperature):
    return None   # explicit "no model" — the UI still works on the deterministic facts


_DISPATCH = {"ollama": _ollama, "anthropic": _anthropic, "openai": _openai, "offline": _offline}


# ── public API ──────────────────────────────────────────────────────
def chat(system, user, max_tokens=400, temperature=0.0):
    """Assistant text, or None on ANY failure (caller must handle None by showing raw facts)."""
    fn = _DISPATCH.get(PROVIDER)
    if fn is None:
        log.warning("unknown LLM provider %r — install one of %s", PROVIDER, list(_DISPATCH))
        return None
    try:
        out = fn(system, user, max_tokens, temperature)
        return out or None
    except Exception as e:                       # timeout, conn refused, http error, bad json
        log.warning("LLM call failed (%s/%s): %s", PROVIDER, MODEL, e)
        return None


def health():
    """Best-effort readiness for the UI badge: is the configured provider usable right now?"""
    if PROVIDER == "ollama":
        try:
            tags = requests.get(f"{OLLAMA_HOST}/api/tags", timeout=3).json()
            names = [m.get("name", "") for m in tags.get("models", [])]
            ok = any(MODEL == n or MODEL == n.split(":")[0] or n.startswith(MODEL) for n in names)
            return {"provider": PROVIDER, "model": MODEL, "ok": ok, "models": names}
        except Exception as e:
            return {"provider": PROVIDER, "model": MODEL, "ok": False, "error": str(e)[:80]}
    if PROVIDER in ("anthropic", "openai"):
        key = os.environ.get("ANTHROPIC_API_KEY" if PROVIDER == "anthropic" else "OPENAI_API_KEY")
        return {"provider": PROVIDER, "model": MODEL, "ok": bool(key)}
    return {"provider": PROVIDER, "model": MODEL, "ok": PROVIDER == "offline"}
