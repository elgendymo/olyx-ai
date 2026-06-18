# Task: data-trustworthiness + responsiveness

Feedback (senior data engineer review):
1. Clean checkout loaded NO data at all.
2. A refresh can overwrite its own cached fallback.
3. Invalid records dropped with no visible record of what/why.
4. Make the app responsive (mobile — see screenshot).

## Plan
- [ ] feed.validate(df, with_report=True): mutually-exclusive rejection report (counts+reasons+samples)
- [ ] feed.seed(): bundled human-readable JSON sample, validated through the chokepoint (Issue 1)
- [ ] feed.bulk(): never overwrite a good last-good cache with a degraded fetch (Issue 2)
- [ ] persist + read rejection report sidecar (Issue 3)
- [ ] config: cache_replace_min_ratio
- [ ] app: 3-mode load (cache/live/seed) + clear sample-data banner (Issue 1)
- [ ] app: DATA QUALITY card surfacing the rejection report (Issue 3)
- [ ] app: responsive CSS media queries + wrap hero (Issue 4)
- [ ] tests: report, seed, cache-regression guard
- [ ] run full suite + headless smoke
