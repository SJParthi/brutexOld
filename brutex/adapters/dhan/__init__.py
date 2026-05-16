"""Dhan HTTP adapter — `/v2/charts/intraday` + `/v2/charts/historical`.

Phase 1 deliverable. Implements:
- HTTPX async client with 3 r/s effective token bucket (5 × 0.6 safety)
- Daily quota counter (100K/day cap with 5% safety margin)
- Auth: manual paste once + auto-renew via `/v2/RenewToken` loop
- 90-day chunking for 5y backfill (~63 chunks total)
- Exponential backoff: 1s → 4s → 16s → 64s, max 5 attempts → FROZEN

To be implemented when operator provides `DHAN_ACCESS_TOKEN`.
"""
