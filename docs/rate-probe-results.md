# Marketplace rate probe results

Status: **complete** (live run).

Re-run:

```powershell
python scripts/rate_probe.py --write-docs
```

## Summary

| Field | Value |
|-------|-------|
| Probe date (UTC) | 2026-08-07 10:55:40 UTC |
| Endpoint | `GET /api/v2/marketplace/products?blueprint_id=...` |
| Expansion used | `1` (Game Night) |
| Blueprint used | `49480` |
| First 429 at (attempted issue-RPS) | 12.0 |
| First 429 body | `{"error":"Too many requests: max 10 requests per second"}` |
| Highest sustained non-429 issue-RPS | 8.0 |
| Sustained window (s) | 9.9 |
| **Production safe RPS** | **6.0** (operator-confirmed for Phase 1) |
| Implied wall-clock for 10k blueprint lookups | 1666.7s (~27.8 min) |
| Recommended generate-chunk size | 2000 |

## Ramp detail

| Target RPS | Issued | Issued RPS | OK | Completed RPS | p50 ms | Max in-flight | Status counts | Hit 429 |
|------------|--------|------------|----|---------------|--------|---------------|---------------|---------|
| 1 | 8 | 1.00 | 8 | 1.00 | 210 | 1 | `{'200': 8}` | False |
| 2 | 16 | 2.00 | 16 | 2.00 | 207 | 1 | `{'200': 16}` | False |
| 5 | 40 | 5.00 | 40 | 5.00 | 220 | 2 | `{'200': 40}` | False |
| 8 | 64 | 7.88 | 64 | 7.88 | 228 | 3 | `{'200': 64}` | False |
| 10 | 80 | 9.83 | 80 | 9.83 | 226 | 5 | `{'200': 80}` | False |
| 12 | 16 | 9.99 | 14 | 8.74 | 248 | 4 | `{'200': 14, '429': 2}` | True |

## Method

1. Authenticate with Bearer token.
2. Resolve one MTG `expansion_id`, sample marketplace, pick a `blueprint_id`.
3. Warm-up calls (3) on that blueprint.
4. Stepped ramp with **concurrent** request starts: 1, 2, 5, 8, 10, 12 issue-RPS (~8s each).
5. On first `429`, record payload and rate.
6. Sustained retest at highest non-429 issue rate (~45s).
7. Apply margin 0.75 to the **issue-rate** ceiling (not latency-bound completion RPS).

## Notes

- Using expansion_id=1 (Game Night), blueprint_id=49480 (concurrent issue-rate probe)
- Warm-up latency p50=269ms (serial throughput would be ~3.72 RPS)
- First 429 during ramp at 12 issue-RPS
- Sustained window hit 429 at 10 RPS; stepping down one ramp level for production safe RPS

Docs conflict on marketplace limits (prose “1/s” vs error “10/s”). This probe is the source of truth for Phase 1 rate limiting and chunk sizing.
