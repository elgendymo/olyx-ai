"""Swappable LLM client — one function, many providers, fail-silent (Briefy's pattern).

Demo default is a LOCAL model via Ollama (no API key, no egress): `qwen2.5:7b`, which won our
narration bake-off (read dislocations correctly, more articulate). Swap provider/model with one
env var; cloud providers (anthropic, openai, huggingface) are thin adapters that read their own key.

**Auto-detection (the deploy story):** when `BROKER_LLM_PROVIDER` is unset we pick automatically —
local boxes run Ollama, but Streamlit Cloud can't (no daemon), so a Hugging Face token in the
environment/secrets is the signal we're hosted → route to HF's Qwen. Same model family
(`qwen2.5:7b` local ≈ `Qwen/Qwen2.5-7B-Instruct` on HF) so narration stays consistent across
environments. Set `BROKER_LLM_PROVIDER` explicitly to override the heuristic.

Critical design choice: this layer NARRATES, it never computes. The copilot hands it a
finished facts dict and asks for prose. That is deliberate — small local models reliably
mangle structured/JSON output (Briefy observed 3B models emitting junk JSON), but narrating
given numbers is well within their range. We never ask the model for a number or a tool call.

`chat()` returns the assistant text, or **None on any failure** (timeout, no key, model down,
bad provider). The contract is that None is normal: the caller degrades to showing the raw
facts. That is the whole resilience story for this layer.

Env:
  BROKER_LLM_PROVIDER  unset=auto (ollama local / huggingface when an HF token is present)
                       | ollama | anthropic | openai | huggingface | offline
  BROKER_LLM_MODEL     override the model id (default per provider)
  OLLAMA_HOST        default http://localhost:11434
  HF_TOKEN          Hugging Face access token (also reads HUGGINGFACEHUB_API_TOKEN) — on
                    Streamlit Cloud put it in Secrets; Streamlit exports secrets as env vars.
  BROKER_LLM_TIMEOUT   seconds (default 60 — local 8B is slow)
"""
import logging
import os

import requests

log = logging.getLogger("llm")

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
TIMEOUT = float(os.environ.get("BROKER_LLM_TIMEOUT", "60"))

# Hugging Face token under either of the two conventional names (HF CLI vs langchain).
HF_TOKEN = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACEHUB_API_TOKEN")


def _resolve_provider(explicit, hf_token):
    """Pick the provider. Explicit env wins; else auto: HF token present ⇒ we're hosted
    (Streamlit Cloud can't run Ollama) ⇒ huggingface, otherwise local ollama."""
    if explicit:
        return explicit.strip().lower()
    return "huggingface" if hf_token else "ollama"


PROVIDER = _resolve_provider(os.environ.get("BROKER_LLM_PROVIDER"), HF_TOKEN)

# qwen2.5:7b beat llama3.1:8b in our narration bake-off (correctly read the dislocation count;
# more articulate). Briefy preferred llama3.1:8b for TOOL-CALLING/JSON — different job. Swap via env.
# HF default is the same Qwen family so cloud narration matches local.
_DEFAULT_MODEL = {"ollama": "qwen2.5:7b",
                  "huggingface": "Qwen/Qwen2.5-7B-Instruct",
                  "anthropic": "claude-haiku-4-5-20251001",
                  "openai": "gpt-4o-mini"}
MODEL = os.environ.get("BROKER_LLM_MODEL") or _DEFAULT_MODEL.get(PROVIDER, "qwen2.5:7b")


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


def _huggingface(system, user, max_tokens, temperature):
    # HF Inference Providers expose an OpenAI-compatible chat route — same body shape as _openai.
    if not HF_TOKEN:
        return None
    r = requests.post("https://router.huggingface.co/v1/chat/completions",
                      headers={"Authorization": f"Bearer {HF_TOKEN}", "content-type": "application/json"},
                      json={"model": MODEL, "max_tokens": max_tokens, "temperature": temperature,
                            "messages": [{"role": "system", "content": system},
                                         {"role": "user", "content": user}]},
                      timeout=TIMEOUT)
    r.raise_for_status()
    return (r.json()["choices"][0]["message"]["content"] or "").strip()


def _offline(system, user, max_tokens, temperature):
    return None   # explicit "no model" — the UI still works on the deterministic facts


_DISPATCH = {"ollama": _ollama, "huggingface": _huggingface,
             "anthropic": _anthropic, "openai": _openai, "offline": _offline}


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
    if PROVIDER == "huggingface":
        # no cheap liveness probe (a tags-style endpoint) — token presence is our readiness signal.
        return {"provider": PROVIDER, "model": MODEL, "ok": bool(HF_TOKEN)}
    if PROVIDER in ("anthropic", "openai"):
        key = os.environ.get("ANTHROPIC_API_KEY" if PROVIDER == "anthropic" else "OPENAI_API_KEY")
        return {"provider": PROVIDER, "model": MODEL, "ok": bool(key)}
    return {"provider": PROVIDER, "model": MODEL, "ok": PROVIDER == "offline"}
