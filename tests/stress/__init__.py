"""Stress / sustained-load tests — the 7th test type per STANDARDS_100PCT §1 row 11.

Created by DEC-STRESS-001 (PR closing the `tests/stress/` ROADMAP gap from
DEC-STANDARDS-AUDIT-001 entry #205, follow-up to DEC-CROSSWALK-001 +
DEC-PYLINT-DROP-001).

Scope
-----
Stress tests verify that the hot-path modules (audit chain, LMDB store,
rate limiter, bloom filter) hold their invariants under SUSTAINED burst
load — not just one-shot correctness.

Execution model (three tiers — DEC-SOAK-REAL-TIER-001, MIGRATION.md #1512)
-------------------------------------------------------------------------
* **PR / unit run** — the pure soak-drift LOGIC
  (:mod:`brutex.observability.soak_drift`) is unit-tested to 100% line+branch on
  synthetic RSS/fd/latency series; the wall-clock driver is NOT run.

* **CI nightly cron (light regression, ~5 min budget)** — runs in
  `.github/workflows/stress-nightly.yml` daily 03:00 UTC on ubuntu-24.04
  (``pytest tests/stress -m stress``). Event counts fit comfortably inside a
  60-minute cap and surface the regression classes (memory-leak floor,
  hash-chain drift, hot-path determinism, the SHORT soak driver
  ``test_soak_sustained.py::test_soak_short``). This tier is a REGRESSION GATE,
  not a soak — it CANNOT complete a soak-scale event count (the workflow's
  guard step refuses one).

* **Real multi-hour soak (self-hosted)** — the ONLY tier that runs a genuine
  hours-long soak: `.github/workflows/soak-selfhosted.yml` on the registered
  M4-Pro, ``pytest tests/stress -m 'stress or soak'`` for the full
  ``STRESS_DURATION_S`` seconds (the ``soak``-marked
  ``test_soak_sustained.py::test_soak_sustained``), with a job timeout DERIVED
  from the requested duration so it can never outrun its own clock.

Knobs (DYNAMIC)
---------------
* ``STRESS_EVENTS`` — event count for the count-bounded stress tests (the
  memory-leak / audit-chain harnesses). The nightly default is 5000; the ubuntu
  tier refuses > 200000.
* ``STRESS_DURATION_S`` — wall-clock seconds for the DURATION-bounded soak
  driver (``test_soak_sustained``). Live since DEC-SOAK-REAL-TIER-001 — the
  self-hosted soak sets it from ``soak_minutes``; a plain dev run defaults to a
  couple of seconds. (Before that decision this knob was documented here but NO
  test read it — a dead lever; the only working soak lever had been an
  un-completable ``STRESS_EVENTS`` count. Reconciled per Golden Rule #10.)

Marker discipline
-----------------
Every stress test carries ``@pytest.mark.stress``; the long soak ALSO carries
``@pytest.mark.soak`` so it is selected only on the self-hosted tier. Unit /
property / integration runs SKIP both markers. Bench tests are separate
(``tests/bench/``).
"""
