"""The MERGE GATE — crash/resume/re-pull STATE CONVERGENCE over the kernel render.

DEC-RUST-RUNID-STORES-001 — ADAPTED from the DEC-LAKE-RUST-LEDGERSTORE-CONVERT-001
(#1918) OFF-vs-ON battery per the coordinator ruling of 2026-07-18 (the #1918
render-plane arm is SUPERSEDED-IN-PART: the ``brutex_identity`` kernel render
is unconditionally the ONE production writer, so there is no flag arm to twin
against). The suite's SUBSTANCE — an EXECUTED proof that the REAL ledger
machinery (:class:`WindowLedger` + the store's ``save_window_ledger`` /
``append_ledger_delta`` / ``load_window_ledger``) converges through an
incremental-pull sequence with a mid-flight CRASH — is kept and re-targeted:

Per scenario the SAME deterministic fixture script runs TWICE on two
independent lakes (control vs subject — both through the ONE kernel-rendered
write path); the asserts are the spec's full set:

* the RELOADED-from-disk state after the crash is identical across runs
  (full ordered 16-field row equality — every column): the write → load →
  base+delta merge-fold chain is DETERMINISTIC, no hidden clock/randomness;
* the FINAL state (in-memory AND re-loaded from disk) is identical across
  runs after the resume + final flush, and disk == memory per run;
* the settled (SUCCESS/SKIPPED) / PLANNED / FAILED / EMPTY window SETS are
  equal; the merge-fold over base+deltas is what the reload exercises;
* NO lost / duplicated / reordered windows: explicit COUNT + key-MULTISET +
  ORDER asserts — and (strengthened vs #1918) the resume COMPLETES the plan:
  zero PLANNED rows survive the resume in every crash scenario;
* ANTI-VACUOUS: the on-disk base is genuinely kernel-authored
  (``created_by`` "parquet-rs" — never a stray pyarrow arm).

Three crash scenarios (crash-after-base-flush · crash-mid-deltas ·
crash-after-partial-record) + the re-pull idempotence arm (a second pull
pass over the kernel-rendered lake replays deterministically).

Runs wherever the hard ``brutex_identity`` wheel + pyarrow are installed
(main CI included — no optional wheel, no flag, no skip arm beyond the plain
dependency importorskips).
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

pq = pytest.importorskip("pyarrow.parquet")

from brutex.data import window_ledger_store as wls  # noqa: E402
from brutex.data.fetch_coverage import WindowKey  # noqa: E402
from brutex.data.window_ledger import EMPTY, FAILED, PLANNED, SUCCESS, WindowLedger  # noqa: E402

if TYPE_CHECKING:
    pass

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


@pytest.fixture(autouse=True)
def _fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    # Hermetic: the RETIRED render flag (DEC-LAKE-RUST-LEDGERSTORE-CONVERT-001,
    # superseded-in-part) would trip the store's loud
    # require_ledger_render_env_unset refusal (its own suite pins that law).
    monkeypatch.delenv(wls.LEDGER_RENDER_ENV, raising=False)


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
    resumed_keys = sorted(int(r.security_id[3:]) for r in resumed.records() if r.status == PLANNED)
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
    """The spec's full convergence assert set — control run vs subject run."""
    # The reloaded-from-disk state the resume starts from is identical —
    # every column of every row, in order (the merge-fold over base+deltas).
    assert control["reloaded_rows"] == subject["reloaded_rows"]
    # Both runs judged the SAME windows still-planned.
    assert control["resumed_keys"] == subject["resumed_keys"]
    # Final in-memory and final on-disk states are identical, and disk == memory.
    assert control["final_rows"] == subject["final_rows"]
    assert control["disk_rows"] == subject["disk_rows"]
    assert control["final_rows"] == control["disk_rows"]
    # Status-family SETS equal (settled / planned / failed / empty) — and the
    # resume genuinely COMPLETED the plan (strengthened vs the #1918 battery).
    assert _status_sets(control["disk_rows"]) == _status_sets(subject["disk_rows"])
    assert _status_sets(control["disk_rows"])["planned"] == set()
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
def test_crash_resume_repull_converges_identically(tmp_path: Path, scenario: str) -> None:
    """The MERGE GATE: the same deterministic crash script on two independent
    lakes converges bit-identically through the kernel-rendered disk state."""
    control_lake = tmp_path / "control"
    subject_lake = tmp_path / "subject"
    control_lake.mkdir()
    subject_lake.mkdir()

    control = _drive_pull_with_crash(control_lake, scenario)
    subject = _drive_pull_with_crash(subject_lake, scenario)

    # ANTI-VACUOUS: the disk state driven through really is the kernel's
    # canonical render (a stray pyarrow arm would falsify the subject).
    for lake in (control_lake, subject_lake):
        created_by = pq.read_metadata(wls.ledger_path(lake)).created_by
        assert created_by.startswith("parquet-rs")

    _assert_converged(control, subject)


def test_repull_idempotence_over_kernel_rendered_file(tmp_path: Path) -> None:
    """A SECOND pull pass over the kernel-rendered lake replays deterministically.

    Pass 1 settles every window (crash-free); pass 2 re-plans the same
    windows (all no-op upserts against settled rows), re-records ONLY the
    retryable FAILED ones with the same deterministic outcomes, and
    full-saves. The resulting rows are identical across two independent
    lakes — the re-pull is idempotent over the canonical parquet-rs bytes
    exactly as it was over the retired pyarrow writer's.
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
    control_rows = _two_pass(control_lake)
    subject_rows = _two_pass(subject_lake)
    assert pq.read_metadata(wls.ledger_path(control_lake)).created_by.startswith("parquet-rs")
    assert control_rows == subject_rows
    assert len(control_rows) == len(_OUTCOMES)
    # Anti-vacuous: pass 2 genuinely retried something — the FAILED set is
    # non-empty by fixture construction and the deterministic outcome table
    # re-records those windows FAILED again (attempts bumped, state
    # converged), while the settled rows stayed settled and NO row is left
    # PLANNED (every re-plan was a no-op upsert against a settled row).
    statuses = {row[5] for row in control_rows}
    assert FAILED in statuses
    assert PLANNED not in statuses
