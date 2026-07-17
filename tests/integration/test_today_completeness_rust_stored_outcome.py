"""OFF-vs-ON STORED-OUTCOME parity for the today-completeness routing (SLICE T24 PR-2).

DEC-DATA-RUST-INTEGRITY-DETECTORS-001. The seam suite pins LAW-LEVEL parity
(verdict structures over the 15-arm corpus); this suite widens the gate to
the STORED OUTCOME the law actually reaches in production — the
DEC-PULL-TODAY-COMPLETENESS-GATE-001 write guard on BOTH ingest spines:

* the SPOT tail (``ingest_spot`` → ``observe_today_completeness`` →
  ``WindowLedger.record(degraded=…)``) over (a) an INDEX count-short day
  (today's bars WITHHELD from the real sink + FAILED/``TODAY_INCOMPLETE``),
  (b) a genuinely complete INDEX day (SUCCESS — every bar lands) and (c) a
  CASH all-zero-volume day (withheld + demoted);
* the F&O tail (``ingest_option_contract``) over (d) a FUT day whose today
  rows carry the #837 still-settling OI-null shape (withheld + demoted).

Driven TWICE — flag OFF then ``BRUTEX_LAKE_RUST_TODAY_COMPLETENESS=1`` in
the post-burn-in steady state (``remaining=0`` — the Rust verdict genuinely
SHIPS) — and the STORED parquet bytes + the LEDGER records (status +
demotion reason) are asserted IDENTICAL, with the withholds/demotions proven
to have FIRED in BOTH arms (anti-vacuous) and a poisoned-crate teeth test
proving the parity gate genuinely bites on the stored outcome.
"""

from __future__ import annotations

import dataclasses
import hashlib
import sys
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import brutex.data.detectors_rust as dr
import pytest
from brutex.adapters.groww.client import GrowwClient
from brutex.adapters.groww.contracts import DerivativeContract
from brutex.adapters.groww.ingest import ingest_spot
from brutex.adapters.groww.option_ingest import ingest_option_contract
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink
from brutex.data.today_completeness import TODAY_INCOMPLETE
from brutex.data.window_ledger import FAILED, SUCCESS, WindowLedger
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from structlog.testing import capture_logs

from tests.fakes.detectors_crate import (
    fake_classify_today_completeness_verdict,
    fake_detectors_crate,
)

pl = pytest.importorskip("polars")

_IST = ZoneInfo("Asia/Kolkata")

# ---------------------------------------------------------------------------
# Fixtures — the EXACT DEC-PULL-TODAY-COMPLETENESS-GATE-001 catching shapes
# (the proven gate-suite grids: spot TODAY 2021-03-05 / F&O TODAY 2024-01-02,
# both regular trading days, both clocks post-close 17:00 IST so
# clamp_window_end mints ``end == today + 1`` — the covers-today window).
# ---------------------------------------------------------------------------

_GATE_TODAY = date(2021, 3, 5)
_GATE_EVENING = datetime(2021, 3, 5, 17, 0, tzinfo=_IST)
_FNO_GATE_NOW = datetime(2024, 1, 2, 17, 0, tzinfo=_IST)


def _gate_now() -> datetime:
    return _GATE_EVENING


def _fno_gate_now() -> datetime:
    return _FNO_GATE_NOW


def _bar(ts: datetime, *, volume: int = 1) -> CandleBar:
    return CandleBar(
        timestamp=ts,
        open=Decimal("100.00"),
        high=Decimal("110.00"),
        low=Decimal("90.00"),
        close=Decimal("105.00"),
        volume=volume,
        open_interest=None,
    )


def _day_minute(day: date, minute_index: int, *, volume: int = 1) -> CandleBar:
    """The day's 1-minute session bar at 09:15 + *minute_index* IST, stored UTC."""
    open_dt = datetime(day.year, day.month, day.day, 9, 15, tzinfo=_IST)
    return _bar((open_dt + timedelta(minutes=minute_index)).astimezone(UTC), volume=volume)


def _inst(symbol: str, security_id: int, *, index: bool) -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=security_id,
        symbol_name=symbol,
        display_name=symbol,
        instrument=InstrumentType.IDX if index else InstrumentType.EQ,
        segment=Segment.CASH,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=0 if index else 1,
        tick_size=Decimal("0") if index else Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
    )


class _Adapter:
    """Fake MarketAdapter returning a fixed bar list per fetch (the ingest-suite idiom)."""

    def __init__(self, bars: Sequence[CandleBar]) -> None:
        self._bars = list(bars)

    def fetch_ohlcv(
        self, *, symbol: str, exchange: object, interval: str, start: date, end: date
    ) -> list[CandleBar]:
        del symbol, exchange, interval, start, end
        return list(self._bars)


class _FakeSDK:
    """A minimal ``GrowwSDK``-shaped fake serving one fixed candle payload.

    The two contract-resolution methods are structural ``GrowwSDK`` protocol
    stubs only — the candle ingest under test never resolves expiries.
    """

    def __init__(self, payload: Mapping[str, object]) -> None:
        self._payload = payload

    def get_historical_candles(self, **kw: object) -> Mapping[str, object]:
        del kw
        return self._payload

    def get_expiries(self, **kw: object) -> Mapping[str, object]:
        raise NotImplementedError  # structural protocol stub — never called

    def get_contracts(self, **kw: object) -> Mapping[str, object]:
        raise NotImplementedError  # structural protocol stub — never called


def _gate_fut() -> DerivativeContract:
    return DerivativeContract(
        underlying_symbol="NIFTY",
        expiry=date(2024, 1, 25),
        symbol="NSE-NIFTY-25Jan24-FUT",
        contract_type="FUT",
        strike=None,
    )


def _fno_rows(day: str, count: int, *, volume: object, oi: object) -> list[list[object]]:
    """*count* consecutive 1-minute vendor rows on *day* from 09:15 IST."""
    base = datetime(2000, 1, 1, 9, 15)  # pure wall-clock arithmetic
    rows: list[list[object]] = []
    for i in range(count):
        t = base + timedelta(minutes=i)
        rows.append([f"{day}T{t:%H:%M}:00", 100.0, 110.0, 90.0, 105.0, volume, oi])
    return rows


#: ALL FOUR detector-law flags — the OFF arm must be genuinely all-OFF even
#: under the lake-rust-parity lane's armed environment (the spot tail's
#: observe-only partial-window classify would otherwise route + log engaged
#: mid-"OFF"), exactly the PR-1 stored-outcome template's isolation.
_ALL_FLAG_ENVS: tuple[str, ...] = (
    dr.LAKE_RUST_RAW_INTEGRITY_ENV,
    dr.LAKE_RUST_INTERIOR_GAP_ENV,
    dr.LAKE_RUST_PARTIAL_WINDOW_ENV,
    dr.LAKE_RUST_TODAY_COMPLETENESS_ENV,
)


@pytest.fixture(autouse=True)
def _isolated_facade(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for env in _ALL_FLAG_ENVS:
        monkeypatch.delenv(env, raising=False)
    dr.reset_detectors_rust_for_tests()
    yield
    dr.reset_detectors_rust_for_tests()


def _arm_flag_on(monkeypatch: pytest.MonkeyPatch, crate_source: str) -> dict[str, int] | None:
    """Arm the today flag in the post-burn-in steady state; return fake call counters.

    ``crate_source == "real"`` requires the built wheel carrying the PR-2
    symbol (skip otherwise — the lake-rust-parity lane installs it);
    ``"fake"`` installs the shared law-faithful fake crate wrapped with a
    CALL COUNTER so engagement is PROVEN, never assumed (a silently-degrading
    ON arm would otherwise make this an oracle-vs-oracle vacuous parity).
    """
    monkeypatch.setenv(dr.LAKE_RUST_TODAY_COMPLETENESS_ENV, "1")
    if crate_source == "real":
        crate = pytest.importorskip("brutex_lake")
        if not hasattr(crate, "classify_today_completeness_verdict"):
            pytest.skip("installed brutex_lake wheel predates the PR-2 today law")
        counters: dict[str, int] | None = None
    else:
        counters = {"today": 0}

        def _counting_today(*args: object) -> tuple[bool, int, int, int, int, bool]:
            assert counters is not None
            counters["today"] += 1
            return fake_classify_today_completeness_verdict(*args)  # type: ignore[arg-type]

        monkeypatch.setitem(
            sys.modules, "brutex_lake", fake_detectors_crate(classify_today=_counting_today)
        )
    # Steady state: the Rust verdict genuinely SHIPS (no burn-in oracle in
    # the serving path) — the strongest stored-outcome claim.
    dr.reset_detectors_rust_for_tests(remaining=0)
    return counters


# ---------------------------------------------------------------------------
# The production pull driver + stored-outcome projections.
# ---------------------------------------------------------------------------


def _run_production_pull(lake_root: Path) -> tuple[dict[str, str], list[dict[str, object]]]:
    """ONE deterministic real-writer pull over the four gate shapes.

    Drives the REAL production consumer chain the law feeds — both ingest
    tails with ``today_completeness_gate=True`` over a REAL
    :class:`ParquetBarSink` (real parquet bytes on disk) and a REAL
    :class:`WindowLedger` (the demotion authority), under the injected
    post-close clocks. One window per symbol/contract keeps the pull fully
    deterministic across arms.

    Returns ``(stored_files, ledger_rows)`` — the sha256 of every file under
    ``bars/`` keyed by relative path, and the ledger records normalised for
    cross-arm comparison (the two wall-clock stamp fields dropped; every
    OTHER field — status, ``last_error`` demotion reason, bars, counters —
    compared verbatim).
    """
    sink = ParquetBarSink(writer=ParquetWriter(lake_root=lake_root))
    ledger = WindowLedger()
    # (a) INDEX count-short: 374 of 375 today minutes — still settling.
    # (b) INDEX complete: the full 375-minute day — settles SUCCESS.
    # (c) CASH all-zero-volume today — the still-settling cash shape.
    spot_shapes: tuple[tuple[Instrument, list[CandleBar]], ...] = (
        (
            _inst("NIFTY", 13, index=True),
            [
                _bar(datetime(2021, 3, 4, 4, 5, tzinfo=UTC)),
                _bar(datetime(2021, 3, 4, 4, 6, tzinfo=UTC)),
                *(_day_minute(_GATE_TODAY, i) for i in range(374)),
            ],
        ),
        (
            _inst("BANKNIFTY", 25, index=True),
            [
                _bar(datetime(2021, 3, 4, 4, 5, tzinfo=UTC)),
                *(_day_minute(_GATE_TODAY, i) for i in range(375)),
            ],
        ),
        (
            _inst("RELIANCE", 2885, index=False),
            [
                _bar(datetime(2021, 3, 4, 4, 5, tzinfo=UTC)),
                *(_day_minute(_GATE_TODAY, i, volume=0) for i in range(10)),
            ],
        ),
    )
    for instrument, bars in spot_shapes:
        ingest_spot(
            _Adapter(bars),  # type: ignore[arg-type]
            sink,
            instrument=instrument,
            interval="1minute",
            start=date(2021, 3, 4),
            end=date(2021, 3, 6),
            ledger=ledger,
            now_ist=_gate_now,
            today_completeness_gate=True,
        )
    # (d) FUT OI-null: prior settled rows + today's forming OI-null rows.
    payload = {
        "candles": [
            *_fno_rows("2024-01-01", 5, volume=5, oi=7),
            *_fno_rows("2024-01-02", 5, volume=0, oi=None),
        ]
    }
    ingest_option_contract(
        GrowwClient(sdk=_FakeSDK(payload), sleep=lambda _s: None, max_attempts=1),
        sink,
        contract=_gate_fut(),
        interval="1minute",
        start=date(2024, 1, 1),
        end=date(2024, 1, 3),
        ledger=ledger,
        now_ist=_fno_gate_now,
        today_completeness_gate=True,
    )
    bars_root = lake_root / "bars"
    stored_files = {
        str(path.relative_to(lake_root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(bars_root.rglob("*"))
        if path.is_file()
    }
    rows: list[dict[str, object]] = []
    for rec in ledger.records():
        row = dataclasses.asdict(rec)
        # The ONLY wall-clock fields — everything else compares verbatim.
        row.pop("first_seen_at")
        row.pop("last_attempt_at")
        rows.append(row)
    rows.sort(
        key=lambda r: (
            str(r["market"]),
            str(r["security_id"]),
            str(r["window_start"]),
            str(r["interval"]),
        )
    )
    return stored_files, rows


def _today_demotions(rows: list[dict[str, object]]) -> int:
    """Count the FAILED/TODAY_INCOMPLETE demotions (the gate's stored verdicts)."""
    return sum(
        1 for row in rows if row["status"] == FAILED and row["last_error"] == TODAY_INCOMPLETE
    )


# ---------------------------------------------------------------------------
# The stored-outcome parity gates.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("crate_source", ["fake", "real"])
def test_today_gate_stored_outcome_identical_off_vs_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crate_source: str
) -> None:
    """Stored parquet bytes + ledger demotions are IDENTICAL OFF vs ON — and FIRED."""
    # --- OFF arm (the default: flag deleted by the autouse fixture). ---
    with capture_logs() as off_logs:
        off_files, off_rows = _run_production_pull(tmp_path / "off_lake")
    assert not any(e["event"] == "detectors_rust.engaged" for e in off_logs)
    # ANTI-VACUOUS: all three withhold classes genuinely fired on the OFF arm
    # (INDEX count_short + CASH volume_all_zero + FUT oi_null_settling) …
    assert _today_demotions(off_rows) == 3
    # … and the complete INDEX day genuinely SETTLED (the gate is per-class,
    # never a blanket withhold).
    assert any(row["status"] == SUCCESS and row["bars"] == 376 for row in off_rows)
    # The WITHHELD append: the count-short NIFTY month holds ONLY the two
    # settled prior-day bars — today's 374 forming minutes never landed.
    nifty_months = sorted((tmp_path / "off_lake" / "bars" / "NSE").rglob("NIFTY/**/*.parquet"))
    assert len(nifty_months) == 1
    assert pl.read_parquet(nifty_months[0]).height == 2

    # --- ON arm (the today flag, steady state, real-or-fake crate). ---
    counters = _arm_flag_on(monkeypatch, crate_source)
    with capture_logs() as on_logs:
        on_files, on_rows = _run_production_pull(tmp_path / "on_lake")
    # The crate genuinely ENGAGED (never a silently-degraded oracle-only run) …
    assert any(e["event"] == "detectors_rust.engaged" for e in on_logs)
    # … and nothing latched/autoreverted mid-run (parity held internally too).
    assert dr.rust_detectors_disabled() is False
    if counters is not None:
        assert counters["today"] >= 4  # one classify per gate shape, at least

    # (1) STORED BYTES: the bars tree is byte-identical (paths AND content).
    assert on_files == off_files
    assert off_files  # the pull genuinely stored parquet files
    # (2) LEDGER: every record identical — status + demotion reason included.
    assert on_rows == off_rows
    # (3) ANTI-VACUOUS ON: the demotions fired identically in the ON arm.
    assert _today_demotions(on_rows) == 3


def test_gate_teeth_poisoned_crate_flips_the_stored_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ANTI-VACUOUS teeth: a law-divergent crate visibly FLIPS the stored outcome.

    A steady-state (post-burn-in) crate that claims every day "complete" must
    make the still-settling bars LAND and the demotions vanish — proving the
    parity assertions above genuinely bite on the STORED outcome (they are
    not an oracle-vs-oracle tautology). Production carries the burn-in
    autorevert against exactly this; ``remaining=0`` bypasses it BY DESIGN
    here to demonstrate the gate's sensitivity.
    """
    with capture_logs():
        off_files, off_rows = _run_production_pull(tmp_path / "off_lake")
    assert _today_demotions(off_rows) == 3

    monkeypatch.setenv(dr.LAKE_RUST_TODAY_COMPLETENESS_ENV, "1")
    poisoned = fake_detectors_crate(
        classify_today=lambda *args: (False, 0, 375, 0, 0, False)  # always "complete"
    )
    monkeypatch.setitem(sys.modules, "brutex_lake", poisoned)
    dr.reset_detectors_rust_for_tests(remaining=0)  # bypass the burn-in shield
    with capture_logs():
        on_files, on_rows = _run_production_pull(tmp_path / "on_lake")

    # The poisoned law laundered the still-settling days — no demotion fired,
    # the withheld bars LANDED (the count-short NIFTY month now holds today's
    # forming minutes too) and the stored outcome DIVERGES on BOTH surfaces:
    # exactly the failure shape the parity test catches.
    assert _today_demotions(on_rows) == 0
    assert on_rows != off_rows
    assert on_files != off_files
    nifty_months = sorted((tmp_path / "on_lake" / "bars" / "NSE").rglob("NIFTY/**/*.parquet"))
    assert len(nifty_months) == 1
    assert pl.read_parquet(nifty_months[0]).height == 2 + 374  # the forming bars froze in
