"""Chaos / randomized-schedule test suite (DEC-CHAOS-RACE-001).

These run on every PR via the ``chaos-race`` CI job (NOT the default unit run).
They hammer safety-critical invariants under *randomized* schedules, crash/restart
boundaries, and concurrent readers — the worst-case interleavings ordered tests do
not exercise. Marked ``chaos`` so the default suite stays fast.
"""
