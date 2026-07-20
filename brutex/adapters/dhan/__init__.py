"""Dhan historical-data adapter — the STANDALONE SPOT feed track.

DEC-DHAN-SPOT-FEED-001 (ADR-0022). A read-only, Groww-independent SPOT feed:
NIFTY / BANKNIFTY / SENSEX 1-minute (the brute-force ENGINE surface) + INDIA VIX
(REFERENCE_ONLY), pulled from the DhanHQ v2 ``/charts/intraday`` endpoint
(``IDX_I`` / ``INDEX``) and written to its OWN lake namespace ``feeds/dhan/`` (the
feed-root nesting law — Groww bytes untouched). NO options, NO expired futures.

Auth is strictly READ-ONLY: BruteX READS ``{client_id, access_token}`` from AWS SSM
and constructs ``DhanContext(client_id, access_token)`` (no network at construction);
it NEVER mints / renews / ``PutParameter``s (DEC-DHAN-READONLY-AUTH-001 — the minter
Lambda / TickVault owns the token lifecycle).

Every Dhan fact is traceable to the verified DhanHQ-v2 research (the official
``dhanhq`` SDK read verbatim + official-doc snippets); UNVERIFIED live-only facts
carry the ``_UNVERIFIED`` honesty suffix in :mod:`brutex.adapters.dhan.constants` and
are never read by a decision branch (Golden Rule #1).
"""

from __future__ import annotations
