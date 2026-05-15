"""Integration tests — wire multiple components together end-to-end.

Slow-ish relative to unit tests (real LMDB stores, real audit files,
real-but-fake the broker client). Still fully offline — no network calls.
"""
