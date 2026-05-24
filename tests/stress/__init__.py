"""Stress / sustained-load tests — the 7th test type per STANDARDS_100PCT §1 row 11.

Created by DEC-STRESS-001 (PR closing the `tests/stress/` ROADMAP gap from
DEC-STANDARDS-AUDIT-001 entry #205, follow-up to DEC-CROSSWALK-001 +
DEC-PYLINT-DROP-001).

Scope
-----
Stress tests verify that the hot-path modules (audit chain, LMDB store,
rate limiter, bloom filter) hold their invariants under SUSTAINED burst
load — not just one-shot correctness.

Execution model
---------------
Two budgets are wired:

* **CI nightly cron (light stress, ~10 min budget)** — runs in
  `.github/workflows/stress-nightly.yml` daily 03:00 UTC. Iteration
  count is sized to fit comfortably inside the budget but is large
  enough to surface most regression classes (memory leak, hash-chain
  drift, lock contention).

* **Manual 24-hour soak (heavy stress, on-demand)** — same tests,
  scaled up via ``STRESS_DURATION_S=86400`` env var on
  ``workflow_dispatch``. NOT run automatically — would burn GHA hours.
  Per Golden Rule #10 honest envelope.

Marker discipline
-----------------
All stress tests carry ``@pytest.mark.stress`` (registered in
``pyproject.toml``). Unit / property / integration test runs SKIP
this marker. Bench tests are separate (``tests/bench/``).
"""
