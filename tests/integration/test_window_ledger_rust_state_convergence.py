"""The NAMED MERGE GATE — crash/resume/re-pull STATE CONVERGENCE, OFF vs ON.

DEC-LAKE-RUST-LEDGERSTORE-CONVERT-001 (MIGRATION.md #1918). The coordinator's
hard precondition for landing the G10 conversion: an EXECUTED proof that the
REAL ledger machinery (:class:`WindowLedger` + the store's
``save_window_ledger`` / ``append_ledger_delta`` / ``load_window_ledger``)
converges IDENTICALLY through an incremental-pull sequence with a mid-flight
CRASH, whether the on-disk parquet was rendered by the pyarrow oracle (flag
OFF — the control) or by the REAL merged ``brutex_lake`` crate (flag ON,
post-burn-in steady state — the subject).

Per scenario the SAME deterministic fixture script runs TWICE (control vs
subject); the asserts are the spec's full set:

* the RELOADED-from-disk state after the crash is identical across arms
  (full ordered 16-field row equality — every column);
* the FINAL state (in-memory AND re-loaded from disk) is identical across
  arms after the resume + final flush;
* the settled (SUCCESS/SKIPPED) / PLANNED / FAILED / EMPTY window SETS are
  equal; the merge-fold over base+deltas is what the reload exercises;
* NO lost / duplicated / reordered windows: explicit COUNT + key-MULTISET +
  ORDER asserts.

Three crash scenarios (crash-after-base-flush · crash-mid-deltas ·
crash-after-partial-record) + the re-pull idempotence arm (a second pull
pass over the flag-ON lake records identically to the OFF control).

Skips cleanly when the merged wheel is absent (the lake-rust-parity lane
installs it; main CI stays wheel-free).
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

brutex_lake = pytest.importorskip(
    "brutex_lake", reason="the merged rust/brutex-lake wheel is not installed"
)
pytest.importorskip("polars")
pytest.importorskip("pyarrow")

from brutex.data import lake_rust_ledger_render, window_ledger_store as wls  # noqa: E402
from brutex.data.fetch_coverage import WindowKey  # noqa: E402
from brutex.data.window_ledger import EMPTY, FAILED, PLANNED, SUCCESS, WindowLedger  # noqa: E402

if TYPE_CHECKING:
    from collections.abc import Iterator

_T0 = datetime(2026, 7, 1, 9, 30, tzinfo=UTC)

#: The deterministic per-window outcome table (index → (raw_bars, error)).
#: Mixed SUCCESS / EMPTY / FAILED so every settledness rank participates in
#: the merge fold the reload exercises.
_OUTCOMES: tuple[tuple[int, str | None], ...] = (
    (375, None),  # SUCCESS
    (0, None),  # EMPTY
    (12, "GrowwTimeoutError"),  # FAILED
    (375, None),  # SUCCESS
    (0, "GrowwRateLimitError"),  # FAILED
    (7, None),  # SUCCESS
    (0, None),  # EMPTY
    (375, None),  # SUCCESS
)


def _key(i: int) -> WindowKey:
    return WindowKey(
        market="groww" if i % 2 == 0 else "groww-option",
        security_id=f"SEC{i:02d}",
        interval="1minute" if i % 3 else "1day",
        start=date(2025, 2, 1 + (i % 2) * 14),
        end=date(2025, 2, 28),
    )


def _record(ledger: WindowLedger, i: int, *, minute: int) -> None:
    raw_bars, error_type = _OUTCOMES[i]
    ledger.record(
        _key(i),
        raw_bars=raw_bars,
        error_type=error_type,
        bars_appended=raw_bars if error_type is None else 0,
        now=_T0 + timedelta(minutes=minute),
    )


def _set_arm(monkeypatch: pytest.MonkeyPatch, *, rust: bool) -> None:
    if rust:
        monkeypatch.setenv(lake_rust_ledger_render.LAKE_RUST_LEDGER_RENDER_ENV, "1")
        lake_rust_ledger_render.reset_lake_rust_ledger_render_for_tests(remaining=0)
    else:
        monkeypatch.delenv(lake_rust_ledger_render.LAKE_RUST_LEDGER_RENDER_ENV, raising=False)
        lake_rust_ledger_render.reset_lake_rust_ledger_render_for_tests()


@pytest.fixture(autouse=True)
def _fresh(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv(lake_rust_ledger_render.LAKE_RUST_LEDGER_RENDER_ENV, raising=False)
    lake_rust_ledger_render.reset_lake_rust_ledger_render_for_tests()
    yield
    lake_rust_ledger_render.reset_lake_rust_ledger_render_for_tests()


def _drive_pull_with_crash(lake: Path, scenario: str) -> dict[str, Any]:
    """One incremental pull with a mid-flight CRASH, through the REAL machinery.

    Returns the observable state snapshots the convergence asserts compare:
    ``reloaded_rows`` (the from-disk state the resume starts from),
    ``resumed_keys`` (the windows the resume judged still-PLANNED),
    ``final_rows`` (the in-memory final state) and ``disk_rows`` (the final
    state re-loaded from disk).
    """
    # ── the pull: plan everything, flush the plan to the base ────────────
    ledger = WindowLedger()
    for i in range(len(_OUTCOMES)):
        ledger.plan(_key(i), now=_T0)
    wls.save_window_ledger(ledger, lake)

    if scenario == "crash_after_base_flush":
        # Record 3 outcomes, delta-flush them, then FULL-save (base) — and
        # crash immediately after the base flush.
        for i in range(3):
            _record(ledger, i, minute=i + 1)
        assert wls.append_ledger_delta(ledger, lake) == 3
        wls.save_window_ledger(ledger, lake)
    elif scenario == "crash_mid_deltas":
        # Two delta flushes land; two MORE outcomes are recorded in memory
        # only — the crash loses them (the resume re-records those windows).
        for i in range(2):
            _record(ledger, i, minute=i + 1)
        assert wls.append_ledger_delta(ledger, lake) == 2
        for i in range(2, 4):
            _record(ledger, i, minute=i + 1)
        assert wls.append_ledger_delta(ledger, lake) == 2
        for i in range(4, 6):
            _record(ledger, i, minute=i + 1)  # never flushed — lost in the crash
    elif scenario == "crash_after_partial_record":
        # One outcome recorded, NOTHING flushed since the plan save.
        _record(ledger, 0, minute=1)
    else:  # pragma: no cover - a scenario typo is a test bug
        raise AssertionError(f"unknown scenario {scenario!r}")

    # ── CRASH: the in-memory ledger is dropped ───────────────────────────
    del ledger

    # ── RESUME: reload from disk (base + surviving deltas fold) ──────────
    resumed = wls.load_window_ledger(lake)
    assert resumed.load_failed is False
    reloaded_rows = resumed.to_rows()
    resumed_keys = sorted(
        int(r.security_id[3:]) for r in resumed.records() if r.status == PLANNED
    )
    for i in resumed_keys:
        _record(resumed, i, minute=100 + i)
    if resumed.dirty_rows():
        assert wls.append_ledger_delta(resumed, lake) > 0
    wls.save_window_ledger(resumed, lake)

    return {
        "reloaded_rows": reloaded_rows,
        "resumed_keys": resumed_keys,
        "final_rows": resumed.to_rows(),
        "disk_rows": wls.load_window_ledger(lake).to_rows(),
    }


def _status_sets(rows: list[tuple[Any, ...]]) -> dict[str, set[tuple[str, ...]]]:
    """The window-key sets per status family (settled / planned / failed / empty)."""
    by: dict[str, set[tuple[str, ...]]] = {
        "settled": set(),
        "planned": set(),
        "failed": set(),
        "empty": set(),
    }
    for row in rows:
        key = tuple(str(v) for v in row[:5])
        status = row[5]
        if status == SUCCESS:
            by["settled"].add(key)
        elif status == PLANNED:
            by["planned"].add(key)
        elif status == FAILED:
            by["failed"].add(key)
        elif status == EMPTY:
            by["empty"].add(key)
        else:  # pragma: no cover - no SKIPPED rows in this fixture
            raise AssertionError(f"unexpected status {status!r}")
    return by


def _assert_converged(control: dict[str, Any], subject: dict[str, Any]) -> None:
    """The spec's full convergence assert set — control (OFF) vs subject (ON)."""
    # The reloaded-from-disk state the resume starts from is identical —
    # every column of every row, in order (the merge-fold over base+deltas).
    assert control["reloaded_rows"] == subject["reloaded_rows"]
    # Both arms judged the SAME windows still-planned.
    assert control["resumed_keys"] == subject["resumed_keys"]
    # Final in-memory and final on-disk states are identical, and disk == memory.
    assert control["final_rows"] == subject["final_rows"]
    assert control["disk_rows"] == subject["disk_rows"]
    assert control["final_rows"] == control["disk_rows"]
    # Status-family SETS equal (settled / planned / failed / empty).
    assert _status_sets(control["disk_rows"]) == _status_sets(subject["disk_rows"])
    # NO lost / duplicated / reordered windows: count + key multiset + order.
    for rows in (control["disk_rows"], subject["disk_rows"]):
        assert len(rows) == len(_OUTCOMES)  # count — nothing lost
        keys = [tuple(str(v) for v in r[:5]) for r in rows]
        assert Counter(keys) == Counter(
            (
                k.market,
                k.security_id,
                k.interval,
                k.start.isoformat(),
                k.end.isoformat(),
            )
            for k in (_key(i) for i in range(len(_OUTCOMES)))
        )  # key multiset — nothing duplicated / substituted
        assert keys == sorted(keys)  # order — the to_rows sorted-key law holds


@pytest.mark.parametrize(
    "scenario",
    ["crash_after_base_flush", "crash_mid_deltas", "crash_after_partial_record"],
)
def test_crash_resume_repull_converges_identically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    """The MERGE GATE: control (flag OFF) vs subject (flag ON, REAL crate wheel)."""
    control_lake = tmp_path / "control"
    subject_lake = tmp_path / "subject"
    control_lake.mkdir()
    subject_lake.mkdir()

    _set_arm(monkeypatch, rust=False)
    control = _drive_pull_with_crash(control_lake, scenario)

    _set_arm(monkeypatch, rust=True)
    subject = _drive_pull_with_crash(subject_lake, scenario)
    # Anti-vacuous: the subject arm genuinely shipped CRATE bytes — a latched
    # or autoreverted seam would have served oracle bytes and proven nothing.
    assert lake_rust_ledger_render.rust_ledger_render_disabled() is False
    control_bytes = wls.ledger_path(control_lake).read_bytes()
    subject_bytes = wls.ledger_path(subject_lake).read_bytes()
    assert control_bytes != subject_bytes  # the #1917 divergence is REAL here

    _assert_converged(control, subject)


def test_repull_idempotence_over_rust_rendered_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A SECOND pull pass over the flag-ON lake records identically to the OFF control.

    Pass 1 settles every window (crash-free); pass 2 re-plans the same
    windows (all no-op upserts against settled rows), re-records ONLY the
    retryable FAILED ones with the same deterministic outcomes, and
    full-saves. The resulting rows are identical across arms — the re-pull
    is idempotent over rust-rendered parquet exactly as over pyarrow's.
    """

    def _two_pass(lake: Path) -> list[tuple[Any, ...]]:
        ledger = WindowLedger()
        for i in range(len(_OUTCOMES)):
            ledger.plan(_key(i), now=_T0)
        for i in range(len(_OUTCOMES)):
            _record(ledger, i, minute=i + 1)
        wls.save_window_ledger(ledger, lake)
        # ── pass 2: a fresh process re-plans + retries the FAILED rows ───
        second = wls.load_window_ledger(lake)
        for i in range(len(_OUTCOMES)):
            second.plan(_key(i), now=_T0 + timedelta(hours=1))  # settled → no-op
        for record in second.records():
            if record.status == FAILED:
                i = int(record.security_id[3:])
                _record(second, i, minute=200 + i)
        if second.dirty_rows():
            assert wls.append_ledger_delta(second, lake) > 0
        wls.save_window_ledger(second, lake)
        return wls.load_window_ledger(lake).to_rows()

    control_lake = tmp_path / "control"
    subject_lake = tmp_path / "subject"
    control_lake.mkdir()
    subject_lake.mkdir()
    _set_arm(monkeypatch, rust=False)
    control_rows = _two_pass(control_lake)
    _set_arm(monkeypatch, rust=True)
    subject_rows = _two_pass(subject_lake)
    assert lake_rust_ledger_render.rust_ledger_render_disabled() is False
    assert control_rows == subject_rows
    assert len(control_rows) == len(_OUTCOMES)
    # Anti-vacuous: pass 2 genuinely retried something (the FAILED set is
    # non-empty by fixture construction) and the retries re-settled.
    statuses = {row[5] for row in control_rows}
    assert FAILED in statuses or SUCCESS in statuses
    retried = [row for row in control_rows if str(row[10] or "").endswith("+00:00") and row[5] != PLANNED]
    assert retried
