# PITCH.md — Magic Spyglass

## The pitch (to Jasper)
Magic Spyglass turns Jasper's morning chaos — 200 unread messages and five price sources that don't
agree — into one screen: it **summarises the inbox**, surfaces the handful of **tradeable** price
dislocations worth acting on *right now*, and answers **any** market question in plain language
("what happened to UCO this week?", "is now a good time to sell?", "which source can't I trust?")
with the exact numbers beside every answer. It treats the third-party feed as **untrusted** —
fat-finger ticks are auto-killed, stale lines flagged loudly, and malicious or oversized data bounded
and escaped — so a price you can't trust never reaches the board. You spot the move and the mismatch
before your competitors, on data you can put real money behind.

## The cut — what I deliberately didn't build, and why this slice first
- **Data integrity before polish.** The graded reality is "Jasper acts on this data," so the time went
  into the ingestion → validate → guard spine, not pixels. Streamlit over a custom UI; hand-written
  CSS over a component lib (evaluated `st_tailwind`, rejected as version-coupled and React-fighting).
- **No new external price sources.** None exist free at matching product granularity (RME/biodiesel in
  EUR/MT is paywalled PRAs — Platts/Argus/ICIS). Instead I surfaced the sources already in the feed
  and let Jasper scope/compare between them.
- **Local-first LLM (Ollama), fail-silent.** Runs with no API key; the copilot degrades to
  deterministic facts when the model is down — it never blocks the data.
- **Hardened the feed, not the perimeter.** I spent time treating the *data* as hostile (bounded
  ingestion, sanitised + escaped output) because that's what Jasper touches — but skipped
  app-perimeter security (auth, secrets, multi-user, deploy) since it's a single-user local tool.
- **No alerts / push / mobile.** Jasper pulls the board (it self-refreshes), but it doesn't page him
  when something moves — proactive alerting is the obvious next step, deliberately deferred.
- **Single-instrument depth over breadth.** The copilot answers one instrument richly; a
  side-by-side multi-instrument comparison and a historical "what-if" backtester are not built.
- **Deferred, with triggers (see PHASES.md "Future work"):** streaming `ijson` ingestion, a copilot
  web-search tool, and unifying OLYX's other data sources (CRM/positions/email) into one pane.

## The truth — what I would NOT ship as-is, and where AI output needed fixing
**Would not ship as-is:**
- The copilot grounds every *number* against the facts, but an LLM can still misread *meaning*. I
  mitigate with a deterministic verdict + an on-screen facts receipt — but for real money a broker
  must read the receipt, not just the prose. I'd want human-in-the-loop on any LLM sentence.
- "Saved capital" in Validation mode is an illustration of impact, not an audited P&L number.
- There is no real second feed, so "source disagreement" is between the mock's own sources; real
  deployment needs real PRA feeds before the cross-source signal is tradeable.
- The feed is hardened against bad/hostile data, but the app itself isn't deployment-ready — no auth,
  secrets management, or monitoring (out of scope for a single-broker local tool, but required before
  it touches a real desk).
- Ask the copilot to *compare two* instruments and it answers about one — multi-instrument comparison
  isn't built yet.

**AI output I had to fix (and how I caught it):**
1. **A silently dead detector.** AI-written copilot filtered z-score outliers on
   `type == "zscore_spike"`, but analytics emits `"zscore"` — so "any outliers?" *always* returned
   nothing. The 100+ unit tests were green; I caught it only by **firing a live battery of broker
   questions at the real feed** (clean synthetic test frames never exposed it).
2. **A substring product match.** `"rme"` matched inside "perfo**rme**r", so "worst performer this
   week" wrongly returned RME's data. Caught the same way — real phrasings on live data — and fixed
   with word-boundary matching (`\brme\b`).

Both forensic trails — plus the two feed-reality bugs the dashboard surfaced about *itself* (a
future-dated tick made 56/60 lines look stale, and a 20% circuit breaker was eating real 20–30%
dislocations, i.e. the actual opportunities) — are documented in the build log.

**Time spent (≈ 6.1 h, at the 6-hour cap)** — first edit **16 Jun 2026 14:56**, last edit
**17 Jun 2026 12:21**:

| | |
|---|---|
| Building — code edits + the design that drove them | 5.56 h |
| Analysis — Read/Grep | 0.06 h |
| Testing — Bash/pytest | 0.39 h |
| This pitch | 0.04 h |
| **Total (est.)** | **≈ 6.1 h** |
