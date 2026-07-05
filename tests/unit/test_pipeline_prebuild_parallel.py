"""Unit tests for the PARALLEL pipeline prebuild orchestration (DEC-HARDWARE-ENVELOPE-001).

Covers :func:`brutex.dashboard.pipeline_prebuild.run_pipeline_prebuild_parallel`
+ the guard dispatch — the SPOT/FUTURES/OPTIONS discovery, the thread-pool fan
out, the parent-side tally fold, and the two merge-blocking mitigations:

* **M1** — the daily forming-session cutoff is resolved ONCE per run and threaded
  identically into every worker (byte-identity across midnight IST).
* **M4** — work items are deduped by ``(exchange, segment, symbol)`` so a #911
  drift pair never materialises the same partition twice.

Offline — ``timeframe_materialize.materialize_lake_timeframes`` is monkeypatched
to a symbol-keyed fake (visible in the worker threads), so no real lake/polars
is touched. The shared discovery / report / outcome code is the SAME COMMON path
the route uses.
"""

from __future__ import annotations

import threading
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.calendars.nse import IST
from brutex.dashboard.fno_cascade import FnoCascadeIndex, build_fno_cascade
from brutex.dashboard.pipeline_prebuild import (
    PrebuildTarget,
    run_pipeline_prebuild_guarded,
    run_pipeline_prebuild_parallel,
)
from brutex.data.errors import LakeWriteHalt
from brutex.data.timeframe_materialize import MaterializeResult, PrebuildKind
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from structlog.testing import capture_logs


def _spot(name: str) -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=abs(hash(name)) & 0xFFFF,
        symbol_name=name,
        display_name=name,
        instrument=InstrumentType.IDX,
        segment=Segment.INDEX,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=0,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
    )


def _dataset(symbol: str, itype: str, *, strike: str | None = None) -> dict[str, object]:
    return {
        "symbol": symbol,
        "exchange": "NSE",
        "underlying": "NIFTY",
        "expiry": "2025-01-30",
        "strike": strike,
        "option_type": itype if itype in ("CE", "PE") else None,
        "instrument_type": itype,
        "interval": "1minute",
    }


def _cascade() -> FnoCascadeIndex:
    return build_fno_cascade(
        [
            _dataset("NSE-NIFTY-30Jan25-FUT", "FUT"),
            _dataset("NSE-NIFTY-30Jan25-20000-CE", "CE", strike="20000"),
            _dataset("NSE-NIFTY-30Jan25-20000-PE", "PE", strike="20000"),
        ]
    )


class _FakeMaterialize:
    """A thread-safe symbol-keyed fake for ``materialize_lake_timeframes``.

    Records every call's kwargs; raises per an injected failure map; returns a
    :class:`MaterializeResult` carrying per-symbol ``unchanged`` /
    ``opens_substituted`` when configured.
    """

    def __init__(
        self,
        *,
        fail: dict[str, Exception] | None = None,
        unchanged: set[str] | None = None,
        opens: dict[str, int] | None = None,
    ) -> None:
        self.fail = fail or {}
        self.unchanged = unchanged or set()
        self.opens = opens or {}
        self.calls: list[dict[str, object]] = []
        self._lock = threading.Lock()

    def __call__(self, **kwargs: object) -> MaterializeResult:
        with self._lock:
            self.calls.append(dict(kwargs))
        symbol = str(kwargs["symbol"])
        exc = self.fail.get(symbol)
        if exc is not None:
            raise exc
        return MaterializeResult(
            written={"5m": 3, "1day": 1},
            skipped=(),
            unchanged=symbol in self.unchanged,
            opens_substituted=self.opens.get(symbol, 0),
        )

    def by_symbol(self, symbol: str) -> list[dict[str, object]]:
        return [c for c in self.calls if c["symbol"] == symbol]


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch) -> _FakeMaterialize:
    fake = _FakeMaterialize()
    monkeypatch.setattr("brutex.data.timeframe_materialize.materialize_lake_timeframes", fake)
    return fake


def _install(monkeypatch: pytest.MonkeyPatch, fake: _FakeMaterialize) -> None:
    monkeypatch.setattr("brutex.data.timeframe_materialize.materialize_lake_timeframes", fake)


# ── happy path: threaded fan-out over SPOT + FUTURES + OPTIONS ──────────────


@pytest.mark.parametrize("workers", [1, 3])
def test_parallel_covers_all_phases(
    patched: _FakeMaterialize, tmp_path: Path, workers: int
) -> None:
    outcome = run_pipeline_prebuild_parallel(
        instruments=[_spot("NSE-NIFTY")],
        cascade=_cascade(),
        lake_root=tmp_path,
        workers=workers,
        daily_before=date(2025, 2, 1),
    )
    # 1 spot + 1 future + 2 options, each 4 bars (3 intraday + 1 daily).
    assert outcome.symbols_built == 4
    assert outcome.total_bars == 16
    assert outcome.failed_count == 0
    assert outcome.ok is True
    kinds = {str(c["symbol"]): c["kind"] for c in patched.calls}
    assert kinds["NSE-NIFTY"] is PrebuildKind.SPOT
    assert kinds["NSE-NIFTY-30Jan25-FUT"] is PrebuildKind.FUTURES
    assert kinds["NSE-NIFTY-30Jan25-20000-CE"] is PrebuildKind.OPTIONS


def test_dense_flag_set_for_index_spot_only(patched: _FakeMaterialize, tmp_path: Path) -> None:
    run_pipeline_prebuild_parallel(
        instruments=[_spot("NSE-NIFTY")],
        cascade=_cascade(),
        lake_root=tmp_path,
        workers=2,
        daily_before=date(2025, 2, 1),
    )
    spot = patched.by_symbol("NSE-NIFTY")[0]
    fut = patched.by_symbol("NSE-NIFTY-30Jan25-FUT")[0]
    assert spot["dense_symbol"] is True  # index spot
    assert fut["dense_symbol"] is False  # contract


# ── M1: daily cutoff resolved ONCE per run ─────────────────────────────────


def test_m1_daily_cutoff_resolved_once_and_identical(
    patched: _FakeMaterialize, tmp_path: Path
) -> None:
    # daily_before=None → the orchestrator resolves today-IST ONCE and threads it
    # into EVERY worker (byte-identity across midnight IST).
    today_ist = datetime.now(UTC).astimezone(IST).date()
    run_pipeline_prebuild_parallel(
        instruments=[_spot("NSE-NIFTY")],
        cascade=_cascade(),
        lake_root=tmp_path,
        workers=3,
    )
    cutoffs = {c["daily_before"] for c in patched.calls}
    assert cutoffs == {today_ist}


def test_m1_explicit_cutoff_threaded_unchanged(patched: _FakeMaterialize, tmp_path: Path) -> None:
    run_pipeline_prebuild_parallel(
        instruments=[_spot("NSE-NIFTY")],
        cascade=_cascade(),
        lake_root=tmp_path,
        workers=2,
        daily_before=date(2024, 12, 25),
    )
    assert {c["daily_before"] for c in patched.calls} == {date(2024, 12, 25)}


def test_m1_no_daily_keeps_none(patched: _FakeMaterialize, tmp_path: Path) -> None:
    run_pipeline_prebuild_parallel(
        instruments=[_spot("NSE-NIFTY")],
        cascade=_cascade(),
        lake_root=tmp_path,
        workers=2,
        include_daily=False,
    )
    assert {c["daily_before"] for c in patched.calls} == {None}
    assert {c["include_daily"] for c in patched.calls} == {False}


# ── M4: dedup drift pairs by partition coords ──────────────────────────────


def test_m4_drift_pair_materialises_contract_once(
    patched: _FakeMaterialize, tmp_path: Path
) -> None:
    # Two spot instruments (NSE-NIFTY + bare NIFTY) both match cascade underlying
    # NIFTY → both discover the SAME contract partitions. M4 dedups them so each
    # contract materialises exactly ONCE (no double-write / stamp race).
    outcome = run_pipeline_prebuild_parallel(
        instruments=[_spot("NSE-NIFTY"), _spot("NIFTY")],
        cascade=_cascade(),
        lake_root=tmp_path,
        workers=3,
        daily_before=date(2025, 2, 1),
    )
    # 2 distinct spot partitions + 1 future + 2 options = 5 unique partitions.
    assert len(patched.by_symbol("NSE-NIFTY-30Jan25-FUT")) == 1
    assert len(patched.by_symbol("NSE-NIFTY-30Jan25-20000-CE")) == 1
    assert outcome.symbols_built == 5


# ── per-symbol quarantine + tallies ────────────────────────────────────────


def test_per_symbol_failure_quarantined(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = _FakeMaterialize(fail={"NSE-NIFTY-30Jan25-FUT": ValueError("bad partition")})
    _install(monkeypatch, fake)
    outcome = run_pipeline_prebuild_parallel(
        instruments=[_spot("NSE-NIFTY")],
        cascade=_cascade(),
        lake_root=tmp_path,
        workers=3,
        daily_before=date(2025, 2, 1),
    )
    assert outcome.ok is False
    assert outcome.failed_count == 1
    assert outcome.failed[0].startswith("NSE-NIFTY-30Jan25-FUT")
    assert outcome.symbols_built == 3  # the other 3 still built


def test_tallies_unchanged_and_opens(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = _FakeMaterialize(
        unchanged={"NSE-NIFTY-30Jan25-20000-CE"},
        opens={"NSE-NIFTY": 4},
    )
    _install(monkeypatch, fake)
    outcome = run_pipeline_prebuild_parallel(
        instruments=[_spot("NSE-NIFTY")],
        cascade=_cascade(),
        lake_root=tmp_path,
        workers=2,
        daily_before=date(2025, 2, 1),
    )
    assert outcome.unchanged_skips == 1
    assert outcome.opens_substituted == 4


# ── exclude_symbols ────────────────────────────────────────────────────────


def test_exclude_partial(patched: _FakeMaterialize, tmp_path: Path) -> None:
    outcome = run_pipeline_prebuild_parallel(
        instruments=[_spot("NSE-NIFTY"), _spot("NSE-RELIANCE")],
        cascade=_cascade(),
        lake_root=tmp_path,
        workers=2,
        daily_before=date(2025, 2, 1),
        exclude_symbols=frozenset({"NSE-RELIANCE"}),
    )
    built = {str(c["symbol"]) for c in patched.calls}
    assert "NSE-RELIANCE" not in built
    assert "NSE-NIFTY" in built
    assert outcome.ok is True


def test_exclude_all_degrades_honestly(patched: _FakeMaterialize, tmp_path: Path) -> None:
    with capture_logs() as logs:
        outcome = run_pipeline_prebuild_parallel(
            instruments=[_spot("NSE-NIFTY")],
            cascade=_cascade(),
            lake_root=tmp_path,
            workers=2,
            daily_before=date(2025, 2, 1),
            exclude_symbols=frozenset({"NSE-NIFTY"}),
        )
    assert outcome.symbols_built == 0
    assert outcome.ok is True
    assert any(log["event"] == "pipeline.prebuild.all_excluded" for log in logs)
    assert patched.calls == []


def test_empty_note_when_no_contracts(patched: _FakeMaterialize, tmp_path: Path) -> None:
    # A cascade with no matching underlying → futures/options phases empty + noted.
    empty_cascade = build_fno_cascade([])
    outcome = run_pipeline_prebuild_parallel(
        instruments=[_spot("NSE-NIFTY")],
        cascade=empty_cascade,
        lake_root=tmp_path,
        workers=2,
        daily_before=date(2025, 2, 1),
    )
    assert outcome.symbols_built == 1  # spot only
    assert outcome.ok is True


def test_started_log_names_parallel_workers(patched: _FakeMaterialize, tmp_path: Path) -> None:
    with capture_logs() as logs:
        run_pipeline_prebuild_parallel(
            instruments=[_spot("NSE-NIFTY")],
            cascade=_cascade(),
            lake_root=tmp_path,
            workers=4,
            daily_before=date(2025, 2, 1),
        )
    started = next(log for log in logs if log["event"] == "pipeline.prebuild.started")
    assert started["parallel_workers"] == 4


# ── guard dispatch (sequential vs parallel) ────────────────────────────────


def test_guard_routes_to_parallel_when_workers_gt_1(
    patched: _FakeMaterialize, tmp_path: Path
) -> None:
    sentinel_called = {"seq": False}

    def _seq_materialize(_target: PrebuildTarget) -> MaterializeResult:
        sentinel_called["seq"] = True
        return MaterializeResult(written={}, skipped=())

    outcome = run_pipeline_prebuild_guarded(
        instruments=[_spot("NSE-NIFTY")],
        cascade_or_error=_cascade,
        materialize=_seq_materialize,
        lake_root=tmp_path,
        workers=3,
        daily_before=date(2025, 2, 1),
    )
    assert outcome.symbols_built == 4  # parallel path ran
    assert sentinel_called["seq"] is False  # the injected sequential fake was NOT used


def test_guard_routes_to_sequential_when_workers_none(tmp_path: Path) -> None:
    seq_targets: list[PrebuildTarget] = []

    def _seq_materialize(target: PrebuildTarget) -> MaterializeResult:
        seq_targets.append(target)
        return MaterializeResult(written={"5m": 2}, skipped=())

    outcome = run_pipeline_prebuild_guarded(
        instruments=[_spot("NSE-NIFTY")],
        cascade_or_error=_cascade,
        materialize=_seq_materialize,
        lake_root=tmp_path,
        workers=None,
    )
    assert outcome.ok is True
    assert seq_targets  # the SEQUENTIAL injected materialiser WAS used


def test_guard_sequential_when_no_lake_root(tmp_path: Path) -> None:
    def _seq_materialize(_target: PrebuildTarget) -> MaterializeResult:
        return MaterializeResult(written={"5m": 2}, skipped=())

    # workers>1 but lake_root None → still sequential (parallel needs a lake_root).
    outcome = run_pipeline_prebuild_guarded(
        instruments=[_spot("NSE-NIFTY")],
        cascade_or_error=_cascade,
        materialize=_seq_materialize,
        lake_root=None,
        workers=4,
    )
    assert outcome.ok is True


def test_guard_parallel_global_stop_reraises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = _FakeMaterialize(fail={"NSE-NIFTY": LakeWriteHalt("disk_full")})
    _install(monkeypatch, fake)
    with pytest.raises(LakeWriteHalt):
        run_pipeline_prebuild_guarded(
            instruments=[_spot("NSE-NIFTY")],
            cascade_or_error=_cascade,
            materialize=lambda _t: MaterializeResult(written={}, skipped=()),
            lake_root=tmp_path,
            workers=3,
            daily_before=date(2025, 2, 1),
        )
