"""Regressions for the DEC-GREEKS-CORRECTNESS-AUDIT-001 fix round.

Each class pins one confirmed finding family of the 7-lens adversarial audit
of the Greeks enrichment value chain (``brutex/data/greeks_enrich.py`` +
``brutex/options/risk_free.py``): every test FAILED against the pre-fix code
(captured fails-before) and passes after the fix — the permanent net against
the audited defect re-appearing.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from structlog.testing import capture_logs

pytest.importorskip("trading_core", reason="greeks enrichment needs the trading_core wheel")

from brutex.data.fno_symbol import parse_fno_symbol
from brutex.data.greeks_enrich import (
    RATE_SOURCE_ID_CSV,
    RATE_SOURCE_ID_FIXED,
    STATUS_BAD_INPUT,
    STATUS_BELOW_INTRINSIC,
    STATUS_NO_RATE,
    STATUS_NO_SPOT,
    STATUS_OK,
    STYLE_AMERICAN_APPROX,
    STYLE_EUROPEAN_EXACT,
    EnrichStats,
    GreeksEnrichTarget,
    enrich_greeks_for_targets,
    enrich_option_bars,
    pack_greeks_provenance,
    unpack_greeks_provenance,
)
from brutex.data.models import GREEKS_OVERLAY_FIELDS, FnoGreeksBar
from brutex.data.spot_join import SpotJoin
from brutex.options.risk_free import CsvRateSource, FixedRateSource, RiskFreeRate

if TYPE_CHECKING:
    from brutex.data.fno_symbol import FnoSymbolParts

_CE_SYMBOL = "NSE-NIFTY-25Jan24-21500-CE"
_BAR_TS = datetime(2024, 1, 15, 3, 45, tzinfo=UTC)  # 09:15 IST, ten days out
_WINDOW = (date(2024, 1, 1), date(2024, 1, 20))
_SPOT = Decimal("21500")
_RATE = FixedRateSource()


def _parts(symbol: str) -> FnoSymbolParts:
    parts = parse_fno_symbol(symbol)
    assert parts is not None
    return parts


def _row(
    ts: datetime = _BAR_TS,
    *,
    close: float = 200.0,
    overlay: dict[str, object] | None = None,
) -> dict[str, object]:
    """One lake row shaped like the schema-tolerant option reader's output."""
    row: dict[str, object] = {
        "timestamp": ts,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": 10,
        "open_interest": 100,
    }
    for name in GREEKS_OVERLAY_FIELDS:
        row[name] = None
    if overlay:
        row.update(overlay)
    return row


def _enrich_rows(
    rows: list[dict[str, object]],
    *,
    symbol: str = _CE_SYMBOL,
    spot_by_ts: dict[datetime, Decimal] | None = None,
    rate_source: object = _RATE,
    style_flag: int = STYLE_EUROPEAN_EXACT,
) -> tuple[list[FnoGreeksBar], EnrichStats]:
    return enrich_option_bars(
        rows,
        parts=_parts(symbol),
        spot_by_ts={_BAR_TS: _SPOT} if spot_by_ts is None else spot_by_ts,
        rate_source=rate_source,  # type: ignore[arg-type]
        style_flag=style_flag,
    )


def _status_of(bar: FnoGreeksBar) -> int:
    pid = bar.greeks_provenance_id
    assert pid is not None
    return unpack_greeks_provenance(pid).status


class TestPackBandValidation:
    """PROV-2 (FIX-9): out-of-band provenance fields raise instead of colliding.

    ``pack_greeks_provenance`` silently ``&``-masked every field, so a future
    ``RISK_FREE_SOURCE_VERSION = 17`` stamp was bit-identical to today's v1 —
    the staleness detector would reuse stale Greeks against it forever. Every
    field is now band-validated with a loud ``ValueError`` naming the field.
    """

    def test_rate_version_17_raises_instead_of_colliding_with_1(self) -> None:
        with pytest.raises(ValueError, match=r"rate_version=17 outside its 4-bit band"):
            pack_greeks_provenance(
                rate_source_id=RATE_SOURCE_ID_FIXED,
                rate_version=17,
                style_flag=STYLE_EUROPEAN_EXACT,
                status=STATUS_OK,
            )

    def test_negative_field_raises(self) -> None:
        with pytest.raises(ValueError, match=r"status=-1 outside its 4-bit band"):
            pack_greeks_provenance(
                rate_source_id=RATE_SOURCE_ID_FIXED,
                style_flag=STYLE_EUROPEAN_EXACT,
                status=-1,
            )

    def test_out_of_band_formula_and_style_raise(self) -> None:
        with pytest.raises(ValueError, match=r"formula_version=256 outside its 8-bit band"):
            pack_greeks_provenance(
                rate_source_id=RATE_SOURCE_ID_FIXED,
                style_flag=STYLE_EUROPEAN_EXACT,
                status=STATUS_OK,
                formula_version=256,
            )
        with pytest.raises(ValueError, match=r"style_flag=8 outside its 3-bit band"):
            pack_greeks_provenance(
                rate_source_id=RATE_SOURCE_ID_FIXED, style_flag=8, status=STATUS_OK
            )

    def test_in_band_golden_pid_byte_identical(self) -> None:
        # The audited v1-era golden stamp: explicit in-band fields still pack
        # to the exact historical integer (no packing-layout drift).
        pid = pack_greeks_provenance(
            rate_source_id=RATE_SOURCE_ID_FIXED,
            rate_version=1,
            style_flag=STYLE_EUROPEAN_EXACT,
            status=STATUS_OK,
            formula_version=1,
            q_bias_id=1,
            tte_convention_id=1,
        )
        assert pid == 1380609


def _seed_lake_pair(
    lake: Path,
    *,
    spot_symbol: str,
    spot_segment: str,
    option_symbol: str,
    spot_close: str,
    option_close: str,
) -> None:
    """Seed one spot bar + one option bar at ``_BAR_TS`` through the real sink."""
    from brutex.data.models import CandleBar
    from brutex.data.parquet_writer import ParquetWriter
    from brutex.data.sink import ParquetBarSink

    sink = ParquetBarSink(writer=ParquetWriter(lake_root=lake))

    def _bar(close: str, *, oi: int | None) -> CandleBar:
        d = Decimal(close)
        return CandleBar(
            timestamp=_BAR_TS, open=d, high=d, low=d, close=d, volume=10, open_interest=oi
        )

    sink.append(
        exchange="NSE",
        segment=spot_segment,
        symbol=spot_symbol,
        interval="1minute",
        bars=[_bar(spot_close, oi=None)],
    )
    sink.append(
        exchange="NSE",
        segment="FNO",
        symbol=option_symbol,
        interval="1minute",
        bars=[_bar(option_close, oi=100)],
    )


def _seed_master(lake: Path, *symbols: str) -> None:
    """Write a REAL DuckDB instrument master at ``<lake>/instruments.duckdb``
    holding one minimal NSE EQ row per symbol (the drift-map VOCABULARY —
    DEC-GREEKS-SPOTJOIN-STOCK-001)."""
    from brutex.data.instrument_store import Instrument, InstrumentStore
    from brutex.models import Exchange, InstrumentType, Segment

    store = InstrumentStore.open(lake / "instruments.duckdb")
    try:
        store.upsert_many(
            [
                Instrument(
                    exch_id=Exchange.NSE,
                    security_id=abs(hash(symbol)) % 100_000,
                    symbol_name=symbol,
                    display_name=symbol,
                    instrument=InstrumentType.EQ,
                    segment=Segment.CASH,
                    series="EQ",
                    isin=None,
                    underlying_symbol=None,
                    underlying_security_id=None,
                    lot_size=1,
                    tick_size=Decimal("0.05"),
                    sm_freeze_qty=None,
                    sm_expiry_date=None,
                    strike_price=None,
                    asm_gsm_flag=False,
                )
                for symbol in symbols
            ],
            snapshot_date=date(2024, 1, 1),
            run_id="test-stock-drift",
            csv_source_sha256="0" * 64,
        )
    finally:
        store.close()


class TestSpotKeyRecoveryLadder:
    """SPOTJOIN-2 (FIX-7): underlying-key drift no longer NO_SPOTs a whole chain.

    The default spot reader probed EXACTLY ``bars/<exch>/<seg>/<underlying>``,
    but spot partitions are keyed by the instrument ``trading_symbol``
    (``MIDCPNIFTY`` options vs a ``NIFTYMIDSELECT`` spot dir; the #911
    ``NSE-M&M`` drifted-dir class) — every chain bar stamped NO_SPOT +
    STYLE_UNKNOWN forever while real spot sat in the lake. The reader now
    walks a verified drift-recovery ladder on an empty primary join.
    """

    def _enrich_target(self, lake: Path, symbol: str) -> object:
        return enrich_greeks_for_targets(
            targets=[
                GreeksEnrichTarget(exchange="NSE", symbol=symbol, start=_WINDOW[0], end=_WINDOW[1])
            ],
            lake_root=lake,
            rate_source=_RATE,
        )

    def _stored_status_style(self, lake: Path, symbol: str) -> tuple[int, int, object]:
        import polars as pl

        part = lake / "bars" / "NSE" / "FNO" / symbol / "1minute" / "2024" / "01.parquet"
        frame = pl.read_parquet(part)
        pid = frame["greeks_provenance_id"].to_list()[0]
        fields = unpack_greeks_provenance(int(pid))
        return fields.status, fields.style_flag, frame["iv"].to_list()[0]

    def test_index_alias_reverse_recovers_whole_chain(self, tmp_path: Path) -> None:
        pytest.importorskip("polars")
        lake = tmp_path / "lake"
        lake.mkdir()
        symbol = "NSE-MIDCPNIFTY-25Jan24-10000-CE"
        _seed_lake_pair(
            lake,
            spot_symbol="NIFTYMIDSELECT",  # the Groww spot trading_symbol
            spot_segment="INDEX",
            option_symbol=symbol,  # the vendor F&O underlying spelling
            spot_close="10000",
            option_close="100",
        )
        with capture_logs() as logs:
            report = self._enrich_target(lake, symbol)
        assert report.enriched == 1 and report.bars_solved == 1  # type: ignore[attr-defined]
        status, style, iv = self._stored_status_style(lake, symbol)
        # Pre-fix: STATUS_NO_SPOT + STYLE_UNKNOWN, iv None, permanently.
        assert status == STATUS_OK and iv is not None
        assert style == STYLE_EUROPEAN_EXACT  # style from the WINNING probe's segment
        recovered = [e for e in logs if e["event"] == "greeks_enrich.spot_key_recovered"]
        assert len(recovered) == 1
        assert recovered[0]["underlying"] == "MIDCPNIFTY"
        assert recovered[0]["recovered_key"] == "NIFTYMIDSELECT"

    def test_911_drifted_dir_form_recovers_cash_chain(self, tmp_path: Path) -> None:
        pytest.importorskip("polars")
        lake = tmp_path / "lake"
        lake.mkdir()
        symbol = "NSE-M&M-25Jan24-1500-CE"
        _seed_lake_pair(
            lake,
            spot_symbol="NSE-M&M",  # the #911 exchange-prefixed drifted dir
            spot_segment="CASH",
            option_symbol=symbol,
            spot_close="1500",
            option_close="40",
        )
        report = self._enrich_target(lake, symbol)
        assert report.enriched == 1 and report.bars_solved == 1  # type: ignore[attr-defined]
        status, style, iv = self._stored_status_style(lake, symbol)
        assert status == STATUS_OK and iv is not None
        assert style == STYLE_AMERICAN_APPROX  # CASH segment won the probe

    def test_env_alias_extends_the_ladder(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pytest.importorskip("polars")
        monkeypatch.setenv("BRUTEX_FNO_INDEX_ALIASES", '{"BARSPOT": "NIFTY"}')
        lake = tmp_path / "lake"
        lake.mkdir()
        _seed_lake_pair(
            lake,
            spot_symbol="BARSPOT",
            spot_segment="INDEX",
            option_symbol=_CE_SYMBOL,
            spot_close="21500",
            option_close="200",
        )
        report = self._enrich_target(lake, _CE_SYMBOL)
        assert report.enriched == 1 and report.bars_solved == 1  # type: ignore[attr-defined]

    def test_primary_hit_emits_no_recovery_log(self, tmp_path: Path) -> None:
        pytest.importorskip("polars")
        lake = tmp_path / "lake"
        lake.mkdir()
        _seed_lake_pair(
            lake,
            spot_symbol="NIFTY",
            spot_segment="INDEX",
            option_symbol=_CE_SYMBOL,
            spot_close="21500",
            option_close="200",
        )
        with capture_logs() as logs:
            report = self._enrich_target(lake, _CE_SYMBOL)
        assert report.enriched == 1  # type: ignore[attr-defined]
        assert not [e for e in logs if e["event"] == "greeks_enrich.spot_key_recovered"]

    def test_exhausted_ladder_stays_honest_no_spot(self, tmp_path: Path) -> None:
        pytest.importorskip("polars")
        lake = tmp_path / "lake"
        lake.mkdir()
        # Spot under an unrelated key the ladder can NEVER derive — no invention.
        _seed_lake_pair(
            lake,
            spot_symbol="UNRELATED",
            spot_segment="INDEX",
            option_symbol=_CE_SYMBOL,
            spot_close="21500",
            option_close="200",
        )
        with capture_logs() as logs:
            report = self._enrich_target(lake, _CE_SYMBOL)
        assert report.enriched == 1 and report.bars_null == 1  # type: ignore[attr-defined]
        status, _style, iv = self._stored_status_style(lake, _CE_SYMBOL)
        assert status == STATUS_NO_SPOT and iv is None
        assert not [e for e in logs if e["event"] == "greeks_enrich.spot_key_recovered"]

    # ── the evidence-grounded STOCK arm (DEC-GREEKS-SPOTJOIN-STOCK-001) ──

    _BAJAJ_CE = "NSE-BAJAJAUTO-25Jan24-1500-CE"  # vendor F&O spelling, no hyphen

    def test_stock_hyphen_drift_recovers_cash_chain(self, tmp_path: Path) -> None:
        """The SPOTJOIN-2 stock follow-up CLOSED: a BAJAJAUTO chain over a
        BAJAJ-AUTO spot dir solves once the instrument master verifies the
        fold — pre-change the whole chain read NO_SPOT forever."""
        pytest.importorskip("polars")
        pytest.importorskip("duckdb")
        lake = tmp_path / "lake"
        lake.mkdir()
        _seed_lake_pair(
            lake,
            spot_symbol="BAJAJ-AUTO",  # the master trading_symbol keyed dir
            spot_segment="CASH",
            option_symbol=self._BAJAJ_CE,
            spot_close="1500",
            option_close="40",
        )
        _seed_master(lake, "BAJAJ-AUTO")
        with capture_logs() as logs:
            report = self._enrich_target(lake, self._BAJAJ_CE)
        assert report.enriched == 1 and report.bars_solved == 1  # type: ignore[attr-defined]
        status, style, iv = self._stored_status_style(lake, self._BAJAJ_CE)
        assert status == STATUS_OK and iv is not None
        assert style == STYLE_AMERICAN_APPROX  # CASH segment won the probe
        recovered = [e for e in logs if e["event"] == "greeks_enrich.spot_key_recovered"]
        assert len(recovered) == 1
        assert recovered[0]["underlying"] == "BAJAJAUTO"
        assert recovered[0]["recovered_key"] == "BAJAJ-AUTO"
        assert recovered[0]["rung"] == "stock_drift"

    def test_trailing_space_fragment_recovers(self, tmp_path: Path) -> None:
        """A legacy trailing-space spot dir (``"M&M "``) is recovered through
        the stock rung — the base ladder has no whitespace-ADDING form."""
        pytest.importorskip("polars")
        pytest.importorskip("duckdb")
        lake = tmp_path / "lake"
        lake.mkdir()
        symbol = "NSE-M&M-25Jan24-1500-CE"
        _seed_lake_pair(
            lake,
            spot_symbol="M&M ",  # the #911 trailing-space drift class
            spot_segment="CASH",
            option_symbol=symbol,
            spot_close="1500",
            option_close="40",
        )
        _seed_master(lake, "M&M")
        with capture_logs() as logs:
            report = self._enrich_target(lake, symbol)
        assert report.enriched == 1 and report.bars_solved == 1  # type: ignore[attr-defined]
        recovered = [e for e in logs if e["event"] == "greeks_enrich.spot_key_recovered"]
        assert len(recovered) == 1
        assert recovered[0]["recovered_key"] == "M&M "
        assert recovered[0]["rung"] == "stock_drift"

    def test_dataless_master_exact_dir_recovers_data_carrying_fragment(
        self, tmp_path: Path
    ) -> None:
        """The multi-dir gap-closure regression: a DATALESS master-exact dir
        never shadows a data-carrying drift fragment — every fragment is
        offered in order and the PROBE decides."""
        pytest.importorskip("polars")
        pytest.importorskip("duckdb")
        lake = tmp_path / "lake"
        lake.mkdir()
        _seed_lake_pair(
            lake,
            spot_symbol="NSE-BAJAJ-AUTO",  # the fragment CARRYING the bars
            spot_segment="CASH",
            option_symbol=self._BAJAJ_CE,
            spot_close="1500",
            option_close="40",
        )
        # The master-exact dir EXISTS but holds no data — offered first, probes
        # empty, and the ladder moves on to the fragment.
        (lake / "bars" / "NSE" / "CASH" / "BAJAJ-AUTO").mkdir(parents=True)
        _seed_master(lake, "BAJAJ-AUTO")
        with capture_logs() as logs:
            report = self._enrich_target(lake, self._BAJAJ_CE)
        assert report.enriched == 1 and report.bars_solved == 1  # type: ignore[attr-defined]
        recovered = [e for e in logs if e["event"] == "greeks_enrich.spot_key_recovered"]
        assert len(recovered) == 1
        assert recovered[0]["recovered_key"] == "NSE-BAJAJ-AUTO"
        assert recovered[0]["rung"] == "stock_drift"

    def test_no_master_stays_honest_no_spot(self, tmp_path: Path) -> None:
        """Without an instrument master there is NO stock arm — the drifted
        chain stays the honest NO_SPOT (the pre-change behaviour, preserved
        byte-identically when the map is None)."""
        pytest.importorskip("polars")
        lake = tmp_path / "lake"
        lake.mkdir()
        _seed_lake_pair(
            lake,
            spot_symbol="BAJAJ-AUTO",
            spot_segment="CASH",
            option_symbol=self._BAJAJ_CE,
            spot_close="1500",
            option_close="40",
        )
        with capture_logs() as logs:
            report = self._enrich_target(lake, self._BAJAJ_CE)
        assert report.enriched == 1 and report.bars_null == 1  # type: ignore[attr-defined]
        status, _style, iv = self._stored_status_style(lake, self._BAJAJ_CE)
        assert status == STATUS_NO_SPOT and iv is None
        assert not [e for e in logs if e["event"] == "greeks_enrich.spot_key_recovered"]
        assert [e for e in logs if e["event"] == "spot_drift.no_master"]

    def test_poisoned_fold_stays_no_spot_with_one_warn(self, tmp_path: Path) -> None:
        """≥2 master symbols sharing the fold POISON it — never guess between
        real instruments; the chain stays NO_SPOT and the operator gets ONE
        ``spot_drift.ambiguous_master`` WARN naming the evidence."""
        pytest.importorskip("polars")
        pytest.importorskip("duckdb")
        lake = tmp_path / "lake"
        lake.mkdir()
        _seed_lake_pair(
            lake,
            spot_symbol="BAJAJ-AUTO",
            spot_segment="CASH",
            option_symbol=self._BAJAJ_CE,
            spot_close="1500",
            option_close="40",
        )
        _seed_master(lake, "BAJAJ-AUTO", "BAJAJAUTO")  # genuine fold collision
        with capture_logs() as logs:
            report = self._enrich_target(lake, self._BAJAJ_CE)
        assert report.enriched == 1 and report.bars_null == 1  # type: ignore[attr-defined]
        status, _style, _iv = self._stored_status_style(lake, self._BAJAJ_CE)
        assert status == STATUS_NO_SPOT
        assert not [e for e in logs if e["event"] == "greeks_enrich.spot_key_recovered"]
        warns = [e for e in logs if e["event"] == "spot_drift.ambiguous_master"]
        assert len(warns) == 1 and warns[0]["fold"] == "BAJAJAUTO"

    def test_index_alias_precedence_with_master_present(self, tmp_path: Path) -> None:
        """Index behaviour UNCHANGED with a map present: the alias rungs run
        FIRST and win — attributed ``rung="alias_ladder"``."""
        pytest.importorskip("polars")
        pytest.importorskip("duckdb")
        lake = tmp_path / "lake"
        lake.mkdir()
        symbol = "NSE-MIDCPNIFTY-25Jan24-10000-CE"
        _seed_lake_pair(
            lake,
            spot_symbol="NIFTYMIDSELECT",
            spot_segment="INDEX",
            option_symbol=symbol,
            spot_close="10000",
            option_close="100",
        )
        _seed_master(lake, "NIFTYMIDSELECT")  # the map is truthy (identity entry)
        with capture_logs() as logs:
            report = self._enrich_target(lake, symbol)
        assert report.enriched == 1 and report.bars_solved == 1  # type: ignore[attr-defined]
        _status, style, _iv = self._stored_status_style(lake, symbol)
        assert style == STYLE_EUROPEAN_EXACT
        recovered = [e for e in logs if e["event"] == "greeks_enrich.spot_key_recovered"]
        assert len(recovered) == 1
        assert recovered[0]["recovered_key"] == "NIFTYMIDSELECT"
        assert recovered[0]["rung"] == "alias_ladder"

    def test_primary_hit_with_master_present_emits_no_recovery_log(self, tmp_path: Path) -> None:
        """The exact-probe-first contract survives the stock arm: a clean key
        hits the primary probe with ZERO recovery log even with a map built."""
        pytest.importorskip("polars")
        pytest.importorskip("duckdb")
        lake = tmp_path / "lake"
        lake.mkdir()
        symbol = "NSE-RELIANCE-25Jan24-1500-CE"
        _seed_lake_pair(
            lake,
            spot_symbol="RELIANCE",
            spot_segment="CASH",
            option_symbol=symbol,
            spot_close="1500",
            option_close="40",
        )
        _seed_master(lake, "RELIANCE")
        with capture_logs() as logs:
            report = self._enrich_target(lake, symbol)
        assert report.enriched == 1  # type: ignore[attr-defined]
        assert not [e for e in logs if e["event"] == "greeks_enrich.spot_key_recovered"]

    def test_second_pass_after_stock_recovery_is_idempotent(self, tmp_path: Path) -> None:
        """Golden Rule #7: the recovered chain's second pass is already_current
        with byte-identical stored bytes."""
        import hashlib

        pytest.importorskip("polars")
        pytest.importorskip("duckdb")
        lake = tmp_path / "lake"
        lake.mkdir()
        _seed_lake_pair(
            lake,
            spot_symbol="BAJAJ-AUTO",
            spot_segment="CASH",
            option_symbol=self._BAJAJ_CE,
            spot_close="1500",
            option_close="40",
        )
        _seed_master(lake, "BAJAJ-AUTO")
        report1 = self._enrich_target(lake, self._BAJAJ_CE)
        assert report1.enriched == 1  # type: ignore[attr-defined]
        part = lake / "bars" / "NSE" / "FNO" / self._BAJAJ_CE / "1minute" / "2024" / "01.parquet"
        digest1 = hashlib.sha256(part.read_bytes()).hexdigest()
        report2 = self._enrich_target(lake, self._BAJAJ_CE)
        assert report2.already_current == 1 and report2.enriched == 0  # type: ignore[attr-defined]
        assert hashlib.sha256(part.read_bytes()).hexdigest() == digest1


class TestFormulaV2Restamp:
    """FIX-3: the designed staleness lever — v1 stamps recompute under v2.

    ``GREEKS_FORMULA_VERSION`` 1 → 2 (plan §15.3): every v1-era stored bar —
    including IVX-1 degenerate-band artifacts and stale-style/rate bars —
    mismatches the current inputs and recomputes + restamps on the next
    enrich pass over its window. HONEST ENVELOPE: the re-enrich reaches only
    contracts still inside a live pull window; expired contracts keep v1
    stamps (bulk re-enrich surface = named follow-up in the ledger).
    """

    def test_formula_version_is_2(self) -> None:
        from brutex.data.greeks_enrich import GREEKS_FORMULA_VERSION

        assert GREEKS_FORMULA_VERSION == 2

    def test_v1_ok_stamp_recomputes_and_restamps_v2(self) -> None:
        v1_pid = pack_greeks_provenance(
            rate_source_id=RATE_SOURCE_ID_FIXED,
            style_flag=STYLE_EUROPEAN_EXACT,
            status=STATUS_OK,
            formula_version=1,
        )
        # A v1-era artifact row (garbage iv) with otherwise-matching values.
        stored = _row(
            close=200.0,
            overlay={
                "iv": 9.99,
                "spot_at_bar": float(_SPOT),
                "rate_used": 0.065,
                "greeks_provenance_id": v1_pid,
            },
        )
        bars, stats = _enrich_rows([stored])
        assert stats.reused == 0 and stats.solved == 1 and stats.changed == 1
        pid = bars[0].greeks_provenance_id
        assert pid is not None
        assert unpack_greeks_provenance(pid).formula_version == 2
        assert bars[0].iv is not None and bars[0].iv != 9.99


class TestNullReasonsTelemetry:
    """CTR-6 (FIX-11): the per-status NULL breakdown actually reaches the logs.

    ``GreeksEnrichReport`` documented the ``no_spot`` tally as "``bars_null``
    broken down in the logs", but no log event carried a per-reason
    breakdown. ``EnrichStats.null_reasons`` now tallies every non-OK stamp
    and the driver emits it on ``greeks_enrich.target_enriched`` — also the
    telemetry that surfaces a SPOTJOIN-2-class whole-chain ``no_spot``.
    """

    def test_stats_carry_sorted_nonzero_reason_pairs(self) -> None:
        no_spot_ts = _BAR_TS + timedelta(minutes=1)
        below_ts = _BAR_TS + timedelta(minutes=2)
        rows = [
            _row(close=1700.0),  # solves (floor ≈ 1536.5 at spot 21500)
            _row(no_spot_ts, close=200.0),  # no spot minute
            _row(below_ts, close=1400.0),  # below the parity floor
        ]
        _bars, stats = _enrich_rows(
            rows,
            symbol="NSE-NIFTY-25Jan24-20000-CE",
            spot_by_ts={_BAR_TS: _SPOT, below_ts: _SPOT},
        )
        assert stats.solved == 1 and stats.nulls == 2
        assert stats.null_reasons == (("below_intrinsic", 1), ("no_spot", 1))

    def test_all_solved_reads_empty_reasons(self) -> None:
        _bars, stats = _enrich_rows([_row(close=200.0)])
        assert stats.null_reasons == ()

    def test_driver_logs_the_breakdown_on_target_enriched(self, tmp_path: Path) -> None:
        no_spot_ts = _BAR_TS + timedelta(minutes=1)
        rows = [_row(close=200.0), _row(no_spot_ts, close=200.0)]
        with capture_logs() as logs:
            enrich_greeks_for_targets(
                targets=[
                    GreeksEnrichTarget(
                        exchange="NSE", symbol=_CE_SYMBOL, start=_WINDOW[0], end=_WINDOW[1]
                    )
                ],
                lake_root=tmp_path,
                rate_source=_RATE,
                read_bars=lambda _t: rows,
                read_spot=lambda _t, _p: SpotJoin({_BAR_TS: _SPOT}, "INDEX"),
                write_bars=lambda _t, b: len(list(b)),  # type: ignore[arg-type]
            )
        enriched_events = [e for e in logs if e["event"] == "greeks_enrich.target_enriched"]
        assert len(enriched_events) == 1
        assert enriched_events[0]["null_reasons"] == {"no_spot": 1}


class TestLakeWriteHaltReRaise:
    """CTR-2 (FIX-8): a pull-wide global stop is never per-target quarantined.

    ``LakeWriteHalt`` (disk full / OOM) fails EVERY remaining write
    identically; the per-target ``except Exception`` used to swallow it and
    keep reading + solving + write-hammering every remaining contract with
    ``errored=N`` indistinguishable from N corrupt contracts. It now
    re-raises immediately. Since DEC-GREEKS-PARALLEL-001 the
    ``_enrich_greeks_from_minute`` wrapper RE-RAISES this halt too (plus
    ``GreeksEnrichPoolBroken``) so it reaches the CRITICAL pull-halt alert —
    every OTHER enrich error stays fenced and can never fail the pull.
    """

    def test_halt_on_first_target_stops_the_pass_immediately(self, tmp_path: Path) -> None:
        from brutex.data.errors import LakeWriteHalt

        reads: list[str] = []
        symbols = [
            _CE_SYMBOL,
            "NSE-NIFTY-25Jan24-21600-CE",
            "NSE-NIFTY-25Jan24-21700-CE",
        ]

        def _read(t: GreeksEnrichTarget) -> list[dict[str, object]]:
            reads.append(t.symbol)
            return [_row(close=200.0)]

        def _halt_write(_t: GreeksEnrichTarget, _b: object) -> int:
            raise LakeWriteHalt("disk_full")

        with pytest.raises(LakeWriteHalt):
            enrich_greeks_for_targets(
                targets=[
                    GreeksEnrichTarget(exchange="NSE", symbol=s, start=_WINDOW[0], end=_WINDOW[1])
                    for s in symbols
                ],
                lake_root=tmp_path,
                rate_source=_RATE,
                read_bars=_read,
                read_spot=lambda _t, _p: SpotJoin({_BAR_TS: _SPOT}, "INDEX"),
                write_bars=_halt_write,
            )
        # Pre-fix: swallowed per-target — all 3 read + solved + write-hammered,
        # errored == 3. Post-fix: targets 2-3 are never even read.
        assert reads == [_CE_SYMBOL]

    def test_per_target_quarantine_still_holds_for_ordinary_errors(self, tmp_path: Path) -> None:
        def _read(t: GreeksEnrichTarget) -> list[dict[str, object]]:
            if t.symbol == _CE_SYMBOL:
                raise OSError("corrupt partition")
            return [_row(close=150.0)]

        report = enrich_greeks_for_targets(
            targets=[
                GreeksEnrichTarget(exchange="NSE", symbol=s, start=_WINDOW[0], end=_WINDOW[1])
                for s in (_CE_SYMBOL, "NSE-NIFTY-25Jan24-21400-PE")
            ],
            lake_root=tmp_path,
            rate_source=_RATE,
            read_bars=_read,
            read_spot=lambda _t, _p: SpotJoin({_BAR_TS: _SPOT}, "INDEX"),
            write_bars=lambda _t, b: len(list(b)),  # type: ignore[arg-type]
        )
        assert report.errored == 1 and report.enriched == 1


class TestSpotRecoveryCandidates:
    """The pure candidate builder behind the FIX-7 ladder (env parsed locally)."""

    def test_sourced_reverse_and_forward_directions(self) -> None:
        from brutex.data.greeks_enrich import _spot_recovery_candidates

        # Reverse: the chain names the F&O symbol, the spot dir the trading one.
        assert "NIFTYMIDSELECT" in _spot_recovery_candidates("MIDCPNIFTY", "NSE")
        assert "NIFTYJR" in _spot_recovery_candidates("NIFTYNXT50", "NSE")
        # Forward: the chain names the spot spelling, the dir the F&O one.
        assert "NIFTYNXT50" in _spot_recovery_candidates("NIFTYJR", "NSE")

    def test_911_form_and_lake_keys_arm(self) -> None:
        from brutex.data.greeks_enrich import _spot_recovery_candidates

        candidates = _spot_recovery_candidates("M&M", "NSE")
        assert "NSE-M&M" in candidates
        # A drifted-SHAPED underlying walks the lake_keys ladder back down.
        assert "M&M" in _spot_recovery_candidates("NSE-M&M", "NSE")

    def test_env_override_malformed_shapes_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from brutex.data.greeks_enrich import _spot_recovery_candidates

        for raw in ("not json", "[1, 2]", '{"A": 3, "": "X", "B": "  ", "OK": "NIFTY"}'):
            monkeypatch.setenv("BRUTEX_FNO_INDEX_ALIASES", raw)
            candidates = _spot_recovery_candidates("NIFTY", "NSE")
            if raw.startswith("{"):
                assert "OK" in candidates  # the one well-formed pair survives
            assert "A" not in candidates and "B" not in candidates

    def test_dedup_and_identity_exclusion(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from brutex.data.greeks_enrich import _spot_recovery_candidates

        # Reverse candidate collides with the #911 form → appears exactly once.
        monkeypatch.setenv("BRUTEX_FNO_INDEX_ALIASES", '{"NSE-FOO": "FOO", "BAZ": "BAZ"}')
        candidates = _spot_recovery_candidates("FOO", "NSE")
        assert candidates.count("NSE-FOO") == 1
        # A self-alias never yields the identity as a candidate.
        assert "BAZ" not in _spot_recovery_candidates("BAZ", "NSE")


class TestForeignMonthPoisonBounded:
    """G-RACE-1 (FIX-5): a foreign-month duplicate can never mutate canonical OHLCV.

    ``read_option_rows`` keep-FIRST dedups across the span's month files, so a
    #911-class poisoned EARLIER file carrying a canonical-month timestamp WON
    the dedup and the latest-wins enrich write then overwrote the authoritative
    keep-first 1-minute OHLCV — the mutation the writer's own law forbids.
    Each month file is now bounded to its OWN UTC (year, month) before concat.
    """

    def test_poisoned_earlier_month_file_cannot_overwrite_canonical_ohlcv(
        self, tmp_path: Path
    ) -> None:
        pl = pytest.importorskip("polars")
        from brutex.data.models import CandleBar
        from brutex.data.parquet_writer import ParquetWriter
        from brutex.data.sink import ParquetBarSink

        lake = tmp_path / "lake"
        lake.mkdir()
        sink = ParquetBarSink(writer=ParquetWriter(lake_root=lake))

        def _bar(ts: datetime, close: str, *, volume: int, oi: int | None) -> CandleBar:
            d = Decimal(close)
            return CandleBar(
                timestamp=ts, open=d, high=d, low=d, close=d, volume=volume, open_interest=oi
            )

        sink.append(
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            interval="1minute",
            bars=[_bar(_BAR_TS, "21500", volume=7, oi=None)],
        )
        # Canonical January row: close 200 / vol 10 / oi 100.
        sink.append(
            exchange="NSE",
            segment="FNO",
            symbol=_CE_SYMBOL,
            interval="1minute",
            bars=[_bar(_BAR_TS, "200", volume=10, oi=100)],
        )
        # A December month file poisoned with the SAME January timestamp but
        # divergent OHLCV (close 210 / vol 7 / oi 70) — today's writer cannot
        # create one; plant it directly (the audited pre-existing-dup state).
        dec_ts = datetime(2023, 12, 15, 4, 0, tzinfo=UTC)
        sink.append(
            exchange="NSE",
            segment="FNO",
            symbol=_CE_SYMBOL,
            interval="1minute",
            bars=[_bar(dec_ts, "210", volume=7, oi=70)],
        )
        base = lake / "bars" / "NSE" / "FNO" / _CE_SYMBOL / "1minute"
        dec_part = base / "2023" / "12.parquet"
        frame = pl.read_parquet(dec_part)
        frame = frame.with_columns(
            pl.Series("timestamp", [_BAR_TS], dtype=frame.schema["timestamp"])
        )
        frame.write_parquet(dec_part)

        report = enrich_greeks_for_targets(
            targets=[
                GreeksEnrichTarget(
                    exchange="NSE", symbol=_CE_SYMBOL, start=date(2023, 12, 1), end=_WINDOW[1]
                )
            ],
            lake_root=lake,
            rate_source=_RATE,
        )
        assert report.errored == 0
        stored = pl.read_parquet(base / "2024" / "01.parquet")
        # Pre-fix: the December poison won the keep-first read dedup and the
        # latest-wins write stamped close=210/vol=7/oi=70 into January.
        assert stored["close"].to_list() == [200.0]
        assert stored["volume"].to_list() == [10]
        assert stored["open_interest"].to_list() == [100]


class TestDegenerateInputHandling:
    """IVX-2 + IVX-3 (FIX-4): NaN/negative inputs degrade per-bar, never per-contract.

    A NaN close used to make the honest-NULL bar ITSELF raise
    (``decimal.InvalidOperation`` inside ``FnoGreeksBar`` validation),
    escaping into the per-TARGET quarantine — the whole contract errored
    every pass, good bars never enriched. A zero/negative close on an ITM
    strike mislabelled ``BELOW_INTRINSIC`` instead of ``BAD_INPUT``.
    """

    def test_nan_close_row_skipped_other_bars_enrich(self) -> None:
        good_ts = _BAR_TS + timedelta(minutes=1)
        rows = [_row(close=math.nan), _row(good_ts, close=200.0)]
        with capture_logs() as logs:
            bars, stats = _enrich_rows(rows, spot_by_ts={_BAR_TS: _SPOT, good_ts: _SPOT})
        assert len(bars) == 1  # the NaN row builds NO bar (stored raw row untouched)
        assert stats.skipped == 1
        assert stats.solved == 1
        assert _status_of(bars[0]) == STATUS_OK
        assert any(e["event"] == "greeks_enrich.bar_skipped_nonfinite" for e in logs)

    def test_inf_high_row_skipped(self) -> None:
        row = _row()
        row["high"] = math.inf
        _bars, stats = _enrich_rows([row])
        assert stats.skipped == 1 and stats.solved == 0

    def test_nan_close_target_not_quarantined_in_driver(self, tmp_path: Path) -> None:
        good_ts = _BAR_TS + timedelta(minutes=1)
        rows = [_row(close=math.nan), _row(good_ts, close=200.0)]
        report = enrich_greeks_for_targets(
            targets=[
                GreeksEnrichTarget(
                    exchange="NSE", symbol=_CE_SYMBOL, start=_WINDOW[0], end=_WINDOW[1]
                )
            ],
            lake_root=tmp_path,
            rate_source=_RATE,
            read_bars=lambda _t: rows,
            read_spot=lambda _t, _p: SpotJoin({_BAR_TS: _SPOT, good_ts: _SPOT}, "INDEX"),
            write_bars=lambda _t, b: len(list(b)),  # type: ignore[arg-type]
        )
        assert report.errored == 0  # the whole contract is NOT quarantined
        assert report.enriched == 1
        assert report.bars_skipped == 1
        assert report.bars_solved == 1

    def test_nan_spot_treated_as_no_spot(self) -> None:
        with capture_logs() as logs:
            bars, stats = _enrich_rows([_row()], spot_by_ts={_BAR_TS: Decimal("NaN")})
        assert stats.nulls == 1
        assert _status_of(bars[0]) == STATUS_NO_SPOT
        assert bars[0].spot_at_bar is None  # never a stored NaN spot
        assert any(e["event"] == "greeks_enrich.spot_invalid" for e in logs)

    def test_negative_spot_treated_as_no_spot(self) -> None:
        bars, _stats = _enrich_rows([_row()], spot_by_ts={_BAR_TS: Decimal("-5")})
        assert _status_of(bars[0]) == STATUS_NO_SPOT

    def test_itm_zero_close_is_bad_input_not_below_intrinsic(self) -> None:
        # Deep ITM (floor ≈ 6560 > 0): a zero close is a BAD INPUT, not a
        # price below parity — the two stamps used to split by moneyness.
        row = _row(close=0.0)
        bars, _stats = _enrich_rows([row], symbol="NSE-NIFTY-25Jan24-15000-CE")
        assert _status_of(bars[0]) == STATUS_BAD_INPUT

    def test_negative_close_is_bad_input_everywhere(self) -> None:
        for symbol in ("NSE-NIFTY-25Jan24-15000-CE", "NSE-NIFTY-25Jan24-24000-CE"):
            bars, _stats = _enrich_rows([_row(close=-5.0)], symbol=symbol)
            assert _status_of(bars[0]) == STATUS_BAD_INPUT

    def test_subnormal_close_still_reaches_kernel_reject(self) -> None:
        # A positive subnormal passes the zero/negative pre-check and the
        # floor screen (OTM floor 0), so the kernel's caller-contract reject
        # still stamps BAD_INPUT (the except GreeksInputError arm stays live).
        bars, _stats = _enrich_rows([_row(close=1e-310)], symbol="NSE-NIFTY-25Jan24-24000-CE")
        assert _status_of(bars[0]) == STATUS_BAD_INPUT


def _stored_row_from(bar: FnoGreeksBar, *, close: float = 200.0) -> dict[str, object]:
    """A lake row carrying *bar*'s solved overlay as its STORED columns."""
    spot_at_bar = bar.spot_at_bar
    return _row(
        close=close,
        overlay={
            "iv": bar.iv,
            "delta": bar.delta,
            "gamma": bar.gamma,
            "theta": bar.theta,
            "vega": bar.vega,
            "rho": bar.rho,
            "spot_at_bar": None if spot_at_bar is None else float(spot_at_bar),
            "t_years_used": bar.t_years_used,
            "rate_used": bar.rate_used,
            "greeks_provenance_id": bar.greeks_provenance_id,
        },
    )


class _CountingFixedRate:
    """A RiskFreeSource spy counting ``rate_on`` calls (RF-5's dead memo pin)."""

    def __init__(self) -> None:
        self.calls = 0

    def rate_on(self, on_date: date) -> RiskFreeRate:
        del on_date
        self.calls += 1
        return RiskFreeRate(rate=0.065, source="fixed", version=1)


class TestReuseGateValueHardening:
    """RF-1/PROV-1/CTR-1/B76-1/G-RACE-2 + SPOTJOIN-1 + PROV-3 (FIX-1).

    The reuse fast path compared ONLY the packed input stamp — never the rate
    VALUE, the current spot join, or the style flag — so a revised risk-free
    CSV / re-configured fixed rate / healed spot lake silently reused stale
    Greeks forever (``already_current``, zero writes, no log). The gate now
    value-checks ``rate_used`` and ``spot_at_bar`` and the style flag; any
    mismatch falls through to the honest recompute.
    """

    def _solved(self, **kwargs: object) -> FnoGreeksBar:
        bars, _stats = _enrich_rows([_row(close=200.0)], **kwargs)  # type: ignore[arg-type]
        assert _status_of(bars[0]) == STATUS_OK
        return bars[0]

    def test_rate_value_change_recomputes_and_heals(self) -> None:
        # Same source KIND (fixed) + same version, different VALUE — the
        # exact five-lens headline (stamp bits identical, rate invisible).
        stored = _stored_row_from(self._solved())
        bars, stats = _enrich_rows([stored], rate_source=FixedRateSource(rate=0.08))
        assert stats.reused == 0  # pre-fix: reused == 1, changed == 0
        assert stats.solved == 1 and stats.changed == 1
        assert bars[0].rate_used == 0.08
        old_iv = stored["iv"]
        assert isinstance(old_iv, float)
        assert bars[0].iv is not None and bars[0].iv != old_iv

    def test_coverage_shrunk_rate_demotes_to_no_rate(self) -> None:
        covering = CsvRateSource(dates=(date(2024, 1, 1),), rates=(0.065,), source="csv:a.csv")
        stored = _stored_row_from(self._solved(rate_source=covering))
        pid = stored["greeks_provenance_id"]
        assert isinstance(pid, int)
        assert unpack_greeks_provenance(pid).rate_source_id == RATE_SOURCE_ID_CSV
        # The replacement CSV no longer covers the bar date → honest DEMOTION.
        late = CsvRateSource(dates=(date(2025, 1, 1),), rates=(0.06,), source="csv:b.csv")
        bars, stats = _enrich_rows([stored], rate_source=late)
        assert stats.reused == 0  # pre-fix: reused verbatim despite rate None
        assert _status_of(bars[0]) == STATUS_NO_RATE
        assert bars[0].iv is None and stats.changed == 1

    def test_spot_value_change_recomputes(self) -> None:
        # Post DEC-LAKE-HEAL-001 the spot 1-minute lake is mutable — a healed
        # spot partition must invalidate the stored Greeks at that minute.
        stored = _stored_row_from(self._solved())
        bars, stats = _enrich_rows([stored], spot_by_ts={_BAR_TS: Decimal("21520")})
        assert stats.reused == 0  # pre-fix: stale 21500-spot Greeks restated
        assert stats.solved == 1 and stats.changed == 1
        assert bars[0].spot_at_bar == Decimal("21520")
        old_iv = stored["iv"]
        assert isinstance(old_iv, float)
        assert bars[0].iv is not None and bars[0].iv != old_iv

    def test_vanished_spot_demotes_to_no_spot(self) -> None:
        stored = _stored_row_from(self._solved())
        bars, stats = _enrich_rows([stored], spot_by_ts={})
        assert stats.reused == 0  # pre-fix: reused with a spot no longer in the lake
        assert _status_of(bars[0]) == STATUS_NO_SPOT
        assert bars[0].iv is None and stats.changed == 1

    def test_style_flip_recomputes_the_stamp(self) -> None:
        stored = _stored_row_from(self._solved(style_flag=STYLE_EUROPEAN_EXACT))
        bars, stats = _enrich_rows([stored], style_flag=STYLE_AMERICAN_APPROX)
        assert stats.reused == 0  # pre-fix: the OLD style flag was pinned forever
        assert stats.changed == 1
        pid = bars[0].greeks_provenance_id
        assert pid is not None
        assert unpack_greeks_provenance(pid).style_flag == STYLE_AMERICAN_APPROX
        # Identical model inputs → identical Greek VALUES; only the stamp moves.
        assert bars[0].iv == stored["iv"]

    def test_reuse_fires_only_when_all_values_match(self) -> None:
        # A planted sentinel iv PROVES the hit path restates verbatim without
        # recomputing (a recompute would replace the sentinel with the true iv).
        stored = _stored_row_from(self._solved())
        true_iv = stored["iv"]
        assert isinstance(true_iv, float)
        stored["iv"] = true_iv + 0.01
        bars, stats = _enrich_rows([stored])
        assert stats.reused == 1
        # ``changed`` compares against the SAME stored row, so the verbatim
        # restate of the sentinel is byte-equal → 0. A recompute would have
        # replaced the sentinel and read changed == 1.
        assert stats.changed == 0
        assert bars[0].iv == true_iv + 0.01

    def test_legacy_non_2dp_join_spot_still_reuses(self) -> None:
        # Review fold: the gate compares the SNAPPED join spot
        # (``round_money(spot)``) — value-identical to a recompute because the
        # snapped spot IS the solve input (SPOTJOIN-3), so a legacy non-2dp
        # spot partition keeps the O(reuse) fast path. Pre-fold the RAW join
        # Decimal never equalled its own 2dp stored ``spot_at_bar``, so such
        # partitions paid a full kernel re-solve on every pass forever.
        stored = _stored_row_from(self._solved())
        raw = Decimal("21500.0000000001")  # snaps to the stored 21500.00
        bars, stats = _enrich_rows([stored], spot_by_ts={_BAR_TS: raw})
        assert stats.reused == 1  # pre-fold: reused == 0 (re-solved each pass)
        assert stats.solved == 0 and stats.changed == 0
        assert bars[0].iv == stored["iv"]

    def test_rate_on_called_once_per_date(self) -> None:
        # RF-5: the setdefault memo evaluated rate_on eagerly per BAR; the
        # explicit two-step memo calls it once per DATE.
        spy = _CountingFixedRate()
        rows = [_row(_BAR_TS + timedelta(minutes=i), close=200.0) for i in range(3)]
        spots = {_BAR_TS + timedelta(minutes=i): _SPOT for i in range(3)}
        _enrich_rows(rows, spot_by_ts=spots, rate_source=spy)
        assert spy.calls == 1  # pre-fix: 3 (once per bar)


class TestSpotSnapConvergence:
    """SPOTJOIN-3 (FIX-6): a legacy non-2dp spot close converges, no rewrite loop.

    The fresh bar carried the RAW join Decimal while the writer snaps
    ``spot_at_bar`` to 2dp at the lake boundary, so a legacy non-2dp spot
    partition made every pass read ``fresh != stored`` — ``enriched=1`` and a
    spurious flock-guarded rewrite attempt FOREVER. The join spot is now
    snapped once at bar-build time (``round_money`` — spot_at_bar ONLY, never
    a Greek/iv/t/rate, DEC-MONEY-2DP-001), so the second pass reads
    ``already_current``.
    """

    def test_legacy_non_2dp_spot_converges_on_second_pass(self, tmp_path: Path) -> None:
        pl = pytest.importorskip("polars")
        from brutex.data.models import CandleBar
        from brutex.data.parquet_writer import ParquetWriter
        from brutex.data.sink import ParquetBarSink

        lake = tmp_path / "lake"
        lake.mkdir()
        sink = ParquetBarSink(writer=ParquetWriter(lake_root=lake))

        def _bar(close: str, *, oi: int | None) -> CandleBar:
            d = Decimal(close)
            return CandleBar(
                timestamp=_BAR_TS,
                open=d,
                high=d,
                low=d,
                close=d,
                volume=10,
                open_interest=oi,
            )

        sink.append(
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            interval="1minute",
            bars=[_bar("30000.11", oi=None)],
        )
        # Plant the legacy non-2dp close DIRECTLY (no supported write path can
        # produce one today — the writer snaps at the lake boundary).
        spot_part = lake / "bars" / "NSE" / "INDEX" / "NIFTY" / "1minute" / "2024" / "01.parquet"
        frame = pl.read_parquet(spot_part)
        frame = frame.with_columns(pl.lit(30000.1099999998).alias("close"))
        frame.write_parquet(spot_part)

        symbol = "NSE-NIFTY-25Jan24-30000-CE"
        sink.append(
            exchange="NSE",
            segment="FNO",
            symbol=symbol,
            interval="1minute",
            bars=[_bar("200", oi=100)],
        )
        targets = [
            GreeksEnrichTarget(exchange="NSE", symbol=symbol, start=_WINDOW[0], end=_WINDOW[1])
        ]
        r1 = enrich_greeks_for_targets(targets=targets, lake_root=lake, rate_source=_RATE)
        assert r1.enriched == 1 and r1.bars_solved == 1
        r2 = enrich_greeks_for_targets(targets=targets, lake_root=lake, rate_source=_RATE)
        # Pre-fix: enriched == 1 on EVERY pass (never-converging rewrite loop).
        assert r2.already_current == 1 and r2.enriched == 0
        option_part = lake / "bars" / "NSE" / "FNO" / symbol / "1minute" / "2024" / "01.parquet"
        assert pl.read_parquet(option_part)["spot_at_bar"].to_list() == [30000.11]


class TestDegenerateTimeValueScreen:
    """IVX-1 (FIX-2, subsumes IVX-4): at-floor premiums stamp BELOW_INTRINSIC.

    A close at (or within ``1e-9 · close`` of) the discounted no-arb floor has
    ~zero time value: the kernel's 1e-9-relative verification then "confirms"
    ANY sigma in a band as wide as [0.001, 0.29] and one arbitrary member was
    stored STATUS_OK. The pre-screen now refuses the whole degenerate band —
    mirroring the kernel's ``VERIFY_REL_TOL`` — before the solver ever runs.
    """

    def test_production_shaped_at_floor_artifact_stamps_below_intrinsic(self) -> None:
        # The audited production failing input: NSE-NIFTY-27Aug26-14000-CE,
        # bar 2026-06-30 10:14 IST, spot 23500.00, close 9644.40 — time value
        # 4.17e-6 rupees above the discounted floor. Pre-fix: STATUS_OK with
        # stored iv=0.23835784699791276 (vega 5.9e-6).
        ts = datetime(2026, 6, 30, 4, 44, tzinfo=UTC)
        row = _row(ts, close=9644.40)
        bars, stats = _enrich_rows(
            [row],
            symbol="NSE-NIFTY-27Aug26-14000-CE",
            spot_by_ts={ts: Decimal("23500.00")},
        )
        assert _status_of(bars[0]) == STATUS_BELOW_INTRINSIC
        assert bars[0].iv is None
        assert stats.nulls == 1

    def test_r0_call_at_intrinsic_is_below_intrinsic(self) -> None:
        # r = 0 makes the floor land EXACTLY on the 2dp grid (K − S for a
        # call): every deep-ITM at-intrinsic quote was a systematic artifact.
        bars, _stats = _enrich_rows(
            [_row(close=1500.0)],
            symbol="NSE-NIFTY-25Jan24-20000-CE",
            rate_source=FixedRateSource(rate=0.0),
        )
        assert _status_of(bars[0]) == STATUS_BELOW_INTRINSIC

    def test_r0_put_deep_itm_at_floor_is_below_intrinsic(self) -> None:
        # PE twin (the preflight-validated put branch of the predicate):
        # floor = K·e^(−rT) − S = 8500 exactly at r = 0.
        bars, _stats = _enrich_rows(
            [_row(close=8500.0)],
            symbol="NSE-NIFTY-25Jan24-30000-PE",
            rate_source=FixedRateSource(rate=0.0),
        )
        assert _status_of(bars[0]) == STATUS_BELOW_INTRINSIC

    def test_real_time_value_above_band_still_solves(self) -> None:
        # Anti-overscreen guard: a REAL half-rupee of time value on the same
        # deep-ITM geometries must still solve (CE floor ≈ 1536.51, PE floor
        # ≈ 8445.23 at r = 6.5%) — the band fires ONLY on the artifact family.
        ce_bars, _s1 = _enrich_rows([_row(close=1537.01)], symbol="NSE-NIFTY-25Jan24-20000-CE")
        assert _status_of(ce_bars[0]) == STATUS_OK
        assert ce_bars[0].iv is not None
        pe_bars, _s2 = _enrich_rows([_row(close=8445.73)], symbol="NSE-NIFTY-25Jan24-30000-PE")
        assert _status_of(pe_bars[0]) == STATUS_OK
        assert pe_bars[0].iv is not None
