# Architecture — Magic Spyglass

Thin Streamlit presentation over a tested, pure engine. The feed is treated as untrusted; every
number passes a chain of trust gates before it can reach Jasper.

## 1. System data flow

```mermaid
flowchart TD
    FEED["Mock price feed<br/>health · latest · bulk<br/>slow · fails · rate-limits · dirty"]:::ext

    subgraph ING["feed.py — ingestion (untrusted boundary)"]
        GET["_get<br/>retry + backoff · fail-silent"]
        STREAM["_parse_stream<br/>bounded NDJSON<br/>record / byte / line caps"]
        VAL["validate — PURE chokepoint<br/>type-coerce · dedupe on id · UTC<br/>price/volume bounds · strip ctrl chars · cap length"]
        CACHE[("parquet cache<br/>doubles as last-good")]
    end

    subgraph ANA["analytics.py — pure, grouped per instrument"]
        GUARD["guard<br/>cross-source circuit breaker<br/>drop fat-fingers over 50%"]
        CALC["VWAP · dislocations · freshness<br/>forward_curve · price_change"]
    end

    subgraph APP["app.py — Streamlit (presentation only)"]
        BOARD["Trade Opportunities · Live Price Board<br/>Forward Curve · Inbox · Validation mode"]
        COP["Copilot (sidebar)"]
    end

    LLM["llm.py<br/>Ollama default · Anthropic · OpenAI"]:::ext

    FEED --> GET --> STREAM --> VAL --> CACHE
    GET -.->|fetch fails| CACHE
    CACHE --> GUARD --> CALC --> BOARD
    CALC --> COP
    COP -->|grounded narration| LLM
    LLM -.->|fail-silent fallback| COP

    classDef ext fill:#1f2937,stroke:#64748b,color:#e2e8f0;
```

## 2. Trust gates — what every number survives before display

Defense in depth: if one gate is bypassed, the next still holds.

```mermaid
flowchart LR
    RAW["raw tick<br/>untrusted"] --> G1["validate<br/>types · bounds · sanitize"]
    G1 --> G2["guard<br/>fat-finger over 50% off peers"]
    G2 --> G3["analytics<br/>MAD outlier filter"]
    G3 --> G4["freshness<br/>stale lines flagged"]
    G4 --> G5["render<br/>html-escape"]
    G5 --> SCREEN["Jasper's screen"]
    G1 -.->|impossible or dirty| DROP["dropped + logged"]
    G2 -.->|catastrophic| DROP
```

## 3. Copilot answer pipeline — deterministic first, LLM second

Numbers are computed, then optionally narrated; the narration is number-grounded and word-banned, so
a decision never rests on a hallucination.

```mermaid
flowchart LR
    Q["question<br/>plain language"] --> ROUTE["route to intent<br/>prices · dislocations · history<br/>data-quality · overview · vwap · curve"]
    ROUTE --> FACTS["compute tight facts<br/>pure analytics"]
    FACTS --> DEC{"single instrument?"}
    DEC -->|no, or data-quality / inventory| DET["deterministic render"]
    DEC -->|yes| NARR["LLM narrates"]
    NARR --> CHK{"grounded?<br/>no foreign asset?<br/>no banned words?"}
    CHK -->|fail| DET
    CHK -->|pass| OUT["answer + facts receipt"]
    DET --> OUT
```

See `README.md` to run it, `PITCH.md` for the pitch/cut/truth, and `PHASES.md` for the build log.
