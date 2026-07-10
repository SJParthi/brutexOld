"""Scenario test: boot-time auto-resume drives off a REAL WindowLedger.

DEC-AUTO-RESUME-ON-BOOT-001. The unit tests
(:mod:`tests.unit.test_orchestrator_auto_resume`) prove the detector + the
guarded helper against ledger doubles; this end-to-end scenario proves they
read a REAL :class:`~brutex.data.window_ledger.WindowLedger` correctly through
its authoritative :meth:`~brutex.data.window_ledger.WindowLedger.progress_counts`
snapshot — the exact contract the production wiring depends on:

* a pull mid-flight (a PLANNED window that has not settled) is detected as an
  incomplete pull and, with the flag ON, TRIGGERS a resume; and
* once every window is settled (SUCCESS / SKIPPED), the SAME ledger reads clean
  and the boot resume is a NOTHING_TO_RESUME no-op.

So the "detect ⇒ resume; complete ⇒ no-op" invariant holds against the real
ledger, not just a hand-built count snapshot.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from brutex.data.fetch_coverage import WindowKey
from brutex.data.window_ledger import WindowLedger
from brutex.orchestrator.auto_resume import (
    IncompletePull,
    ResumeVerdict,
    detect_incomplete_pull,
    maybe_resume_on_boot,
)


def _window(day: int) -> WindowKey:
    """A distinct spot window key for the given start-day offset in Jan 2020."""
    return WindowKey(
        market="groww",
        security_id="NSE-NIFTY",
        interval="1minute",
        start=date(2020, 1, day),
        end=date(2020, 1, day + 1),
    )


def test_detect_and_resume_then_clean_on_real_ledger() -> None:
    """A mid-flight ledger triggers a resume; a fully-settled one is a no-op."""
    now = datetime(2020, 1, 2, tzinfo=UTC)
    ledger = WindowLedger()

    # Plan two windows (the pull's tiling) — both PLANNED, nothing fetched yet.
    ledger.plan(_window(1), now=now)
    ledger.plan(_window(3), now=now)

    # Settle the FIRST window SUCCESS; the second stays PLANNED (pull interrupted).
    ledger.record(
        _window(1),
        raw_bars=375,
        error_type=None,
        bars_appended=375,
        now=now,
    )

    # The detector reads the REAL ledger snapshot: one window is still outstanding.
    incomplete = detect_incomplete_pull(ledger)
    assert incomplete is not None
    assert isinstance(incomplete, IncompletePull)
    assert incomplete.total == 2
    assert incomplete.settled == 1
    assert incomplete.planned == 1
    assert incomplete.failed == 0
    assert incomplete.outstanding == 1

    # With the flag ON, the boot helper fires the injected resume action.
    resumed: list[IncompletePull] = []
    verdict = maybe_resume_on_boot(
        load_ledger=lambda: ledger,
        resume=resumed.append,
        enabled=True,
    )
    assert verdict is ResumeVerdict.RESUMED
    assert len(resumed) == 1
    assert resumed[0].outstanding == 1

    # Finish the pull: settle the last window. Now the SAME ledger reads clean.
    ledger.record(
        _window(3),
        raw_bars=375,
        error_type=None,
        bars_appended=375,
        now=now,
    )
    assert detect_incomplete_pull(ledger) is None

    resumed_after: list[IncompletePull] = []
    verdict_after = maybe_resume_on_boot(
        load_ledger=lambda: ledger,
        resume=resumed_after.append,
        enabled=True,
    )
    assert verdict_after is ResumeVerdict.NOTHING_TO_RESUME
    assert resumed_after == []


def test_failed_window_on_real_ledger_triggers_resume() -> None:
    """A transient FAILED window (a 429-storm leftover) is detected + resumed."""
    now = datetime(2020, 1, 2, tzinfo=UTC)
    ledger = WindowLedger()
    ledger.plan(_window(5), now=now)
    # A retryable transport error → FAILED (below any settle threshold).
    ledger.record(
        _window(5),
        raw_bars=0,
        error_type="RATE_LIMIT",
        bars_appended=0,
        now=now,
    )

    incomplete = detect_incomplete_pull(ledger)
    assert incomplete is not None
    assert incomplete.failed == 1
    assert incomplete.outstanding == 1

    resumed: list[IncompletePull] = []
    verdict = maybe_resume_on_boot(
        load_ledger=lambda: ledger,
        resume=resumed.append,
        enabled=True,
    )
    assert verdict is ResumeVerdict.RESUMED
    assert len(resumed) == 1


def test_incident_shape_inprocess_resume_with_held_store_and_durable_ledger(
    tmp_path: Path,
) -> None:
    """DEC-AUTO-RESUME-INPROCESS-001 — the 2026-07-09T02:47:24Z incident shape.

    A REAL durable window ledger on disk with a FAILED window, BOTH dashboard
    DuckDB handles HELD OPEN — the boot-window L3 sweeper's ``catalog.duckdb``
    (the incident traceback's actual first-raising site) and the lifetime rw
    ``instruments.duckdb`` — and the opt-in flag ON: the boot resume must fire
    the IN-PROCESS launch (through the intent machinery —
    :func:`~brutex.dashboard.pull_intent.run_legacy_resume`) — exactly where
    the deleted subprocess child died in ~0.3 s on DuckDB's exclusive
    cross-process file locks, stranding the failed windows silently.
    """
    import pytest

    pytest.importorskip("duckdb", reason="the incident shape needs the real DuckDB lock")
    pytest.importorskip("pyarrow", reason="the durable ledger store needs pyarrow")
    from brutex.dashboard.pull_intent import (
        PullIntent,
        PullIntentStore,
        ResumeAction,
        run_legacy_resume,
    )
    from brutex.data.catalog import Catalog
    from brutex.data.instrument_store import InstrumentStore
    from brutex.data.window_ledger_store import load_window_ledger, save_window_ledger

    lake_root = tmp_path
    now = datetime(2026, 7, 9, tzinfo=UTC)

    # The incident's ledger: a real durable ledger with a FAILED (re-pullable) window.
    ledger = WindowLedger()
    ledger.plan(_window(5), now=now)
    ledger.record(_window(5), raw_bars=0, error_type="RATE_LIMIT", bars_appended=0, now=now)
    save_window_ledger(ledger, lake_root)

    # BOTH dashboard handles the old child died on — HELD across the resume: the
    # boot-window L3 sweeper's catalog (the incident's first-raising site) + the
    # lifetime rw store.
    with (
        Catalog.open(lake_root / "catalog.duckdb"),
        InstrumentStore.open(lake_root / "instruments.duckdb"),
    ):
        reloaded = load_window_ledger(lake_root)
        incomplete = detect_incomplete_pull(reloaded)
        assert incomplete is not None
        assert incomplete.failed == 1

        launched: list[PullIntent] = []

        def _resume(_incomplete: IncompletePull) -> None:
            """Discard the honest ``ResumeAction`` — the ``resume`` seam is ``-> None``.

            Mirrors the production binding (``app.py``'s ``_resume_legacy_pull``),
            which likewise drops :func:`run_legacy_resume`'s return value; the
            action's outcome is asserted separately via the direct call below.
            """
            run_legacy_resume(
                PullIntentStore(lake_root),
                launch=launched.append,
                probe_singleflight=lambda: True,
            )

        verdict = maybe_resume_on_boot(
            load_ledger=lambda: load_window_ledger(lake_root),
            resume=_resume,
            enabled=True,
        )
        assert verdict is ResumeVerdict.RESUMED
        assert len(launched) == 1, "the in-process launch fires despite the held store"
        assert launched[0].symbols is None, "the legacy resume re-fires the whole universe"
        assert launched[0].restarts == 1, "the crash-loop token was burnt before launch"
        # And the underlying action reports the honest FIRED outcome when driven directly.
        direct = run_legacy_resume(
            PullIntentStore(lake_root),
            launch=launched.append,
            probe_singleflight=lambda: True,
        )
        assert direct is ResumeAction.FIRED
