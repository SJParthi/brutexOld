"""End-to-end Greeks-enrich lake round-trips (DEC-GREEKS-ENGINE-PR2B-001).

The REAL window-bounded readers + the REAL flock-guarded ``latest-wins`` write
spine over a real temp lake: solve-and-store, FUT untouched, re-run
idempotence (byte-identical), vendor re-pull keep-first preservation, the
crash-mid-pass durability/resume story, the no-spot heal, and the
mixed-schema resilience of the downstream daily-derive / executor readers.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

pytest.importorskip("trading_core", reason="greeks enrichment needs the trading_core wheel")
pytest.importorskip("polars", reason="the lake round-trip needs polars")

import polars as pl
from brutex.data.fno_symbol import parse_fno_symbol
from brutex.data.greeks_enrich import (
    STATUS_NO_SPOT,
    STATUS_OK,
    GreeksEnrichTarget,
    _style_for_segment,
    enrich_greeks_for_targets,
    enrich_option_bars,
    read_option_rows,
    unpack_greeks_provenance,
    write_option_greeks,
)
from brutex.data.models import CandleBar, FnoGreeksBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink
from brutex.data.spot_join import spot_close_by_ts_window
from brutex.options.risk_free import FixedRateSource

if TYPE_CHECKING:
    from collections.abc import Sequence

_CE = "NSE-NIFTY-25Jan24-21500-CE"
_PE = "NSE-NIFTY-25Jan24-21500-PE"
_FUT = "NSE-NIFTY-25Jan24-FUT"
_TS = datetime(2024, 1, 15, 3, 45, tzinfo=UTC)  # 09:15 IST, ten days to expiry
_WINDOW = (date(2024, 1, 1), date(2024, 1, 20))
_RATE = FixedRateSource()


def _bar(ts: datetime, close: str, *, oi: int | None = 100) -> CandleBar:
    return CandleBar(
        timestamp=ts,
        open=Decimal(close),
        high=Decimal(close) + 5,
        low=Decimal(close) - 5,
        close=Decimal(close),
        volume=10,
        open_interest=oi,
    )


@pytest.fixture
def lake(tmp_path: Path) -> Path:
    root = tmp_path / "lake"
    root.mkdir()
    return root


def _sink(lake: Path) -> ParquetBarSink:
    return ParquetBarSink(writer=ParquetWriter(lake_root=lake))


def _seed_spot(lake: Path, minutes: int = 3) -> None:
    bars = [_bar(_TS + timedelta(minutes=i), "21500", oi=None) for i in range(minutes)]
    _sink(lake).append(
        exchange="NSE", segment="INDEX", symbol="NIFTY", interval="1minute", bars=bars
    )


def _seed_option(lake: Path, symbol: str = _CE, minutes: int = 3, close: str = "200") -> None:
    bars = [_bar(_TS + timedelta(minutes=i), close) for i in range(minutes)]
    _sink(lake).append(exchange="NSE", segment="FNO", symbol=symbol, interval="1minute", bars=bars)


def _targets(*symbols: str) -> list[GreeksEnrichTarget]:
    return [
        GreeksEnrichTarget(exchange="NSE", symbol=s, start=_WINDOW[0], end=_WINDOW[1])
        for s in symbols
    ]


def _option_partition(lake: Path, symbol: str = _CE) -> Path:
    return lake / "bars" / "NSE" / "FNO" / symbol / "1minute" / "2024" / "01.parquet"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TestEnrichRoundTrip:
    def test_solve_and_store_through_the_real_lake(self, lake: Path) -> None:
        _seed_spot(lake)
        _seed_option(lake)
        report = enrich_greeks_for_targets(targets=_targets(_CE), lake_root=lake, rate_source=_RATE)
        assert report.enriched == 1 and report.bars_solved == 3
        df = pl.read_parquet(_option_partition(lake))
        assert df["iv"].null_count() == 0
        assert df["spot_at_bar"].to_list() == [21500.0] * 3
        for pid in df["greeks_provenance_id"].to_list():
            assert unpack_greeks_provenance(pid).status == STATUS_OK

    def test_futures_partition_left_raw(self, lake: Path) -> None:
        _seed_spot(lake)
        _seed_option(lake, symbol=_FUT, close="21510")
        before = _sha(_option_partition(lake, _FUT))
        report = enrich_greeks_for_targets(
            targets=_targets(_FUT), lake_root=lake, rate_source=_RATE
        )
        assert report.futures_skipped == 1
        assert _sha(_option_partition(lake, _FUT)) == before
        assert pl.read_parquet(_option_partition(lake, _FUT)).width == 7

    def test_reenrich_twice_is_byte_identical(self, lake: Path) -> None:
        _seed_spot(lake)
        _seed_option(lake)
        enrich_greeks_for_targets(targets=_targets(_CE), lake_root=lake, rate_source=_RATE)
        h1 = _sha(_option_partition(lake))
        report2 = enrich_greeks_for_targets(
            targets=_targets(_CE), lake_root=lake, rate_source=_RATE
        )
        assert report2.already_current == 1 and report2.enriched == 0
        assert _sha(_option_partition(lake)) == h1

    def test_vendor_repull_after_enrich_keeps_greeks(self, lake: Path) -> None:
        """The keep-first re-pull of the SAME bars never strips the overlay."""
        _seed_spot(lake)
        _seed_option(lake)
        enrich_greeks_for_targets(targets=_targets(_CE), lake_root=lake, rate_source=_RATE)
        h1 = _sha(_option_partition(lake))
        _seed_option(lake)  # the idempotent vendor re-pull (keep-first)
        assert _sha(_option_partition(lake)) == h1

    def test_later_vendor_bars_enriched_incrementally(self, lake: Path) -> None:
        """A later pull adds a NEW minute → the next pass solves ONLY it (reuse skip)."""
        _seed_spot(lake, minutes=5)
        _seed_option(lake, minutes=3)
        enrich_greeks_for_targets(targets=_targets(_CE), lake_root=lake, rate_source=_RATE)
        # A later vendor pull lands one more minute.
        _sink(lake).append(
            exchange="NSE",
            segment="FNO",
            symbol=_CE,
            interval="1minute",
            bars=[_bar(_TS + timedelta(minutes=3), "201")],
        )
        report = enrich_greeks_for_targets(targets=_targets(_CE), lake_root=lake, rate_source=_RATE)
        assert report.enriched == 1
        assert report.bars_reused == 3  # the already-solved minutes were NOT re-solved
        assert report.bars_solved == 1
        df = pl.read_parquet(_option_partition(lake))
        assert df["iv"].null_count() == 0

    def test_no_spot_chain_heals_when_spot_arrives(self, lake: Path) -> None:
        _seed_option(lake)  # NO spot in the lake at all
        report = enrich_greeks_for_targets(targets=_targets(_CE), lake_root=lake, rate_source=_RATE)
        assert report.bars_null == 3 and report.bars_solved == 0
        df = pl.read_parquet(_option_partition(lake))
        assert df["iv"].null_count() == 3
        for pid in df["greeks_provenance_id"].to_list():
            assert unpack_greeks_provenance(pid).status == STATUS_NO_SPOT
        # The spot arrives on a later pull → the stamped-NULL bars HEAL.
        _seed_spot(lake)
        report2 = enrich_greeks_for_targets(
            targets=_targets(_CE), lake_root=lake, rate_source=_RATE
        )
        assert report2.bars_solved == 3
        assert pl.read_parquet(_option_partition(lake))["iv"].null_count() == 0

    def test_crash_mid_pass_is_durable_and_resumable(self, lake: Path) -> None:
        """A write failure on ONE target corrupts nothing and heals on re-run."""
        _seed_spot(lake)
        _seed_option(lake, symbol=_CE)
        _seed_option(lake, symbol=_PE, close="150")
        ce_before = _sha(_option_partition(lake, _CE))

        def _crashing_write(target: GreeksEnrichTarget, bars: Sequence[FnoGreeksBar]) -> int:
            if target.symbol == _CE:
                raise OSError("simulated crash mid-enrich")
            return write_option_greeks(lake, target, bars)

        report = enrich_greeks_for_targets(
            targets=_targets(_CE, _PE),
            lake_root=lake,
            rate_source=_RATE,
            write_bars=_crashing_write,
        )
        assert report.errored == 1 and report.enriched == 1
        # The crashed target's partition is UNTOUCHED (atomic spine — never torn).
        assert _sha(_option_partition(lake, _CE)) == ce_before
        assert pl.read_parquet(_option_partition(lake, _PE))["iv"].null_count() == 0
        # The next pass (the next pull) heals the crashed target incrementally.
        report2 = enrich_greeks_for_targets(
            targets=_targets(_CE, _PE), lake_root=lake, rate_source=_RATE
        )
        assert report2.enriched == 1 and report2.already_current == 1
        assert pl.read_parquet(_option_partition(lake, _CE))["iv"].null_count() == 0

    def test_zero_option_pull_is_a_no_op(self, lake: Path) -> None:
        _seed_spot(lake)
        report = enrich_greeks_for_targets(
            targets=_targets(_FUT), lake_root=lake, rate_source=_RATE
        )
        assert report.futures_skipped == 1 and report.bars_written == 0

    def test_window_bounds_the_read(self, lake: Path) -> None:
        """Bars outside the target window are neither read nor rewritten."""
        _seed_spot(lake)
        _seed_option(lake)
        # A bar in a FAR month, outside [start, end).
        far = datetime(2023, 11, 1, 4, 0, tzinfo=UTC)
        _sink(lake).append(
            exchange="NSE", segment="FNO", symbol=_CE, interval="1minute", bars=[_bar(far, "300")]
        )
        far_partition = lake / "bars" / "NSE" / "FNO" / _CE / "1minute" / "2023" / "11.parquet"
        before = _sha(far_partition)
        rows = read_option_rows(lake, _targets(_CE)[0])
        assert all(cast("datetime", r["timestamp"]) >= _TS for r in rows)
        enrich_greeks_for_targets(targets=_targets(_CE), lake_root=lake, rate_source=_RATE)
        assert _sha(far_partition) == before  # never rewritten

    def test_read_option_rows_empty_when_no_partition(self, lake: Path) -> None:
        assert read_option_rows(lake, _targets(_CE)[0]) == []


class TestMemoisationInvariance:
    """DEC-GREEKS-SPOT-MEMO-001 golden A/B: memoised driver ≡ per-target fresh reads.

    Lake A is enriched by the REAL driver (sorted targets + the capacity-2
    spot-read LRU); lake B — seeded byte-identically — is enriched by manually
    composing the PUBLIC unmemoised pipeline per target (a FRESH
    ``spot_close_by_ts_window`` for every contract, exactly what the driver
    did before the memo). Every stored option partition must come out
    byte-for-byte identical.
    """

    _JAN = (date(2024, 1, 1), date(2024, 1, 20))
    _FEB = (date(2024, 2, 1), date(2024, 2, 20))
    _FEB_TS = datetime(2024, 2, 5, 4, 0, tzinfo=UTC)

    def _seed(self, lake: Path) -> list[GreeksEnrichTarget]:
        """2 underlyings × (2 NIFTY windows + 1 RELIANCE) × strikes × CE/PE.

        3 distinct memo keys — (NSE, NIFTY, Jan), (NSE, NIFTY, Feb),
        (NSE, RELIANCE, Jan) — so the capacity-2 LRU is forced to EVICT, plus
        shared-key chains so it is forced to HIT.
        """
        sink = _sink(lake)
        for i in range(3):
            sink.append(
                exchange="NSE",
                segment="INDEX",
                symbol="NIFTY",
                interval="1minute",
                bars=[_bar(_TS + timedelta(minutes=i), "21500", oi=None)],
            )
            sink.append(
                exchange="NSE",
                segment="INDEX",
                symbol="NIFTY",
                interval="1minute",
                bars=[_bar(self._FEB_TS + timedelta(minutes=i), "21700", oi=None)],
            )
            sink.append(
                exchange="NSE",
                segment="CASH",
                symbol="RELIANCE",
                interval="1minute",
                bars=[_bar(_TS + timedelta(minutes=i), "2800", oi=None)],
            )
        targets: list[GreeksEnrichTarget] = []

        def _option(symbol: str, window: tuple[date, date], ts: datetime, close: str) -> None:
            sink.append(
                exchange="NSE",
                segment="FNO",
                symbol=symbol,
                interval="1minute",
                bars=[_bar(ts + timedelta(minutes=i), close) for i in range(3)],
            )
            targets.append(
                GreeksEnrichTarget(exchange="NSE", symbol=symbol, start=window[0], end=window[1])
            )

        for strike in (21400, 21500, 21600):
            for side in ("CE", "PE"):
                _option(f"NSE-NIFTY-25Jan24-{strike}-{side}", self._JAN, _TS, "200")
        for side in ("CE", "PE"):
            _option(f"NSE-NIFTY-29Feb24-21700-{side}", self._FEB, self._FEB_TS, "250")
        for strike in (2700, 2800, 2900):
            for side in ("CE", "PE"):
                _option(f"NSE-RELIANCE-25Jan24-{strike}-{side}", self._JAN, _TS, "110")
        return targets

    def _enrich_unmemoised(self, lake: Path, targets: list[GreeksEnrichTarget]) -> None:
        """The public pipeline per target with a FRESH spot read every time."""
        for target in targets:
            parts = parse_fno_symbol(target.symbol)
            assert parts is not None and parts.option_type is not None
            rows = read_option_rows(lake, target)
            join = spot_close_by_ts_window(
                lake,
                exchange=target.exchange,
                underlying=parts.underlying,
                interval="1minute",
                start=target.start,
                end=target.end,
            )
            bars, _stats = enrich_option_bars(
                rows,
                parts=parts,
                spot_by_ts=join.close_by_ts,
                rate_source=_RATE,
                style_flag=_style_for_segment(join.segment),
            )
            write_option_greeks(lake, target, bars)

    def test_memoised_output_byte_equals_fresh_per_target_reads(self, tmp_path: Path) -> None:
        lake_a = tmp_path / "lake_a"
        lake_a.mkdir()
        lake_b = tmp_path / "lake_b"
        lake_b.mkdir()
        targets_a = self._seed(lake_a)
        targets_b = self._seed(lake_b)

        report = enrich_greeks_for_targets(targets=targets_a, lake_root=lake_a, rate_source=_RATE)
        assert report.enriched == len(targets_a) == 14
        # 3 distinct (exchange, underlying, window) keys → 3 performed reads,
        # every other target a memo hit (the honest measured counters).
        assert report.spot_reads == 3
        assert report.spot_hits == len(targets_a) - 3

        self._enrich_unmemoised(lake_b, targets_b)

        rel_a = sorted(p.relative_to(lake_a) for p in lake_a.rglob("*.parquet"))
        rel_b = sorted(p.relative_to(lake_b) for p in lake_b.rglob("*.parquet"))
        assert rel_a == rel_b and len(rel_a) > 0
        for rel in rel_a:
            a_bytes = (lake_a / rel).read_bytes()
            b_bytes = (lake_b / rel).read_bytes()
            assert a_bytes == b_bytes, f"memoised vs fresh divergence at {rel}"


class TestMixedSchemaDownstreamReaders:
    """The downstream readers survive a mixed 7/17-column option tree."""

    def _mixed_tree(self, lake: Path) -> None:
        # Month 1 (2024-01): enriched 17-col; month 2 (2024-02): raw 7-col.
        _seed_spot(lake)
        _seed_option(lake)
        enrich_greeks_for_targets(targets=_targets(_CE), lake_root=lake, rate_source=_RATE)
        feb = datetime(2024, 2, 1, 4, 0, tzinfo=UTC)
        _sink(lake).append(
            exchange="NSE", segment="FNO", symbol=_CE, interval="1minute", bars=[_bar(feb, "210")]
        )
        assert pl.read_parquet(_option_partition(lake)).width == 17
        feb_partition = lake / "bars" / "NSE" / "FNO" / _CE / "1minute" / "2024" / "02.parquet"
        assert pl.read_parquet(feb_partition).width == 7

    def test_daily_derive_reader_reads_mixed_tree(self, lake: Path) -> None:
        from brutex.data.spot_daily_resample import make_lake_read_1m_window

        self._mixed_tree(lake)
        read = make_lake_read_1m_window(
            lake_root=lake,
            exchange="NSE",
            segment="FNO",
            symbol=_CE,
            start=date(2024, 1, 1),
            end=date(2024, 2, 10),
            include_oi=True,
        )
        frame = read()
        assert frame.height == 4  # 3 January + 1 February bars
        assert frame.columns == [
            "ts_utc",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "open_interest",
        ]

    def test_timeframe_materialize_reader_reads_mixed_tree(self, lake: Path) -> None:
        from brutex.data.timeframe_materialize import make_lake_read_1m

        self._mixed_tree(lake)
        frame = make_lake_read_1m(
            lake_root=lake,
            exchange="NSE",
            segment="FNO",
            symbol=_CE,
            with_open_interest=True,
        )()
        assert frame.height == 4

    def test_lake_option_source_reads_mixed_tree(self, lake: Path) -> None:
        from brutex.options.lake_source import LakeOptionBarSource
        from brutex.options.lot_size_history import OptionsUnderlying

        self._mixed_tree(lake)
        source = LakeOptionBarSource(lake_root=lake)
        pair = source.load_pair(
            instrument=OptionsUnderlying.NIFTY,
            expiry_date=date(2024, 1, 25),
            strike=21500,
            option_type="CE",
            entry_ts=_TS,
            exit_ts=datetime(2024, 2, 1, 4, 0, tzinfo=UTC),
        )
        assert pair is not None
        assert pair.entry_bar.close == Decimal("200.00")
        assert pair.exit_bar.close == Decimal("210.00")

    def test_enrich_reader_reads_mixed_tree(self, lake: Path) -> None:
        self._mixed_tree(lake)
        rows = read_option_rows(
            lake,
            GreeksEnrichTarget(
                exchange="NSE", symbol=_CE, start=date(2024, 1, 1), end=date(2024, 2, 10)
            ),
        )
        assert len(rows) == 4
        assert rows[0]["iv"] is not None  # January enriched
        assert rows[-1]["iv"] is None  # February raw reads NULL overlay


class TestStockDriftHeldOpenStore:
    """DEC-GREEKS-SPOTJOIN-STOCK-001 — the MANDATORY held-open-store pin.

    The drift-map loader opens ``<lake>/instruments.duckdb`` — the SAME file a
    long-lived dashboard ``InstrumentStore`` handle holds open in-process.
    Without this pin, a same-path open conflict would silently degrade the
    fail-soft loader to ``master_error`` → ``None`` on EVERY production pass —
    a permanently-dormant feature (the exact #935/#1014 dormant-feature trap;
    ``instrument_store.py`` documents that same-path config conflicts are
    real). The pin: the map MUST load and the recovery MUST fire while the
    dashboard-style handle is held.
    """

    _BAJAJ_CE = "NSE-BAJAJAUTO-25Jan24-1500-CE"

    def test_enrich_recovers_stock_spot_while_store_handle_is_held(self, lake: Path) -> None:
        pytest.importorskip("duckdb")
        from datetime import date as date_type
        from decimal import Decimal as Dec

        from brutex.data.instrument_store import Instrument, InstrumentStore
        from brutex.models import Exchange, InstrumentType, Segment
        from structlog.testing import capture_logs

        # A drifted stock chain: option underlying BAJAJAUTO, spot dir the
        # master trading_symbol BAJAJ-AUTO under CASH.
        spot = [_bar(_TS + timedelta(minutes=i), "1500", oi=None) for i in range(3)]
        _sink(lake).append(
            exchange="NSE", segment="CASH", symbol="BAJAJ-AUTO", interval="1minute", bars=spot
        )
        option = [_bar(_TS + timedelta(minutes=i), "40") for i in range(3)]
        _sink(lake).append(
            exchange="NSE", segment="FNO", symbol=self._BAJAJ_CE, interval="1minute", bars=option
        )
        # The dashboard-style handle: opened BEFORE the pass, held THROUGHOUT.
        with InstrumentStore.open(lake / "instruments.duckdb") as held:
            held.upsert_many(
                [
                    Instrument(
                        exch_id=Exchange.NSE,
                        security_id=42,
                        symbol_name="BAJAJ-AUTO",
                        display_name="BAJAJ-AUTO",
                        instrument=InstrumentType.EQ,
                        segment=Segment.CASH,
                        series="EQ",
                        isin=None,
                        underlying_symbol=None,
                        underlying_security_id=None,
                        lot_size=1,
                        tick_size=Dec("0.05"),
                        sm_freeze_qty=None,
                        sm_expiry_date=None,
                        strike_price=None,
                        asm_gsm_flag=False,
                    )
                ],
                snapshot_date=date_type(2024, 1, 1),
                run_id="held-open-pin",
                csv_source_sha256="0" * 64,
            )
            with capture_logs() as logs:
                report = enrich_greeks_for_targets(
                    targets=_targets(self._BAJAJ_CE), lake_root=lake, rate_source=_RATE
                )
        # The map LOADED (no fail-soft degrade masked the conflict) …
        assert not [e for e in logs if e["event"] == "spot_drift.master_error"]
        assert [e for e in logs if e["event"] == "spot_drift.map_built"]
        # … and the recovery actually FIRED through it.
        recovered = [e for e in logs if e["event"] == "greeks_enrich.spot_key_recovered"]
        assert len(recovered) == 1
        assert recovered[0]["recovered_key"] == "BAJAJ-AUTO"
        assert recovered[0]["rung"] == "stock_drift"
        assert report.enriched == 1 and report.bars_solved == 3
        part = lake / "bars" / "NSE" / "FNO" / self._BAJAJ_CE / "1minute" / "2024" / "01.parquet"
        df = pl.read_parquet(part)
        assert df["iv"].null_count() == 0
        for pid in df["greeks_provenance_id"].to_list():
            assert unpack_greeks_provenance(pid).status == STATUS_OK
