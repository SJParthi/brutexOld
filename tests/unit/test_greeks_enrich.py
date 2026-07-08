"""Unit tests for :mod:`brutex.data.greeks_enrich` (DEC-GREEKS-ENGINE-PR2B-001).

The generation-time Greeks store of `DEC-GREEKS-GENERATION-TIME-001`
(plan §15 / ADR-0015 §2b): provenance packing, the locked ``T`` / expiry
conventions, the pure per-bar solve core with EVERY stamped-NULL family of
plan §15.8, and the pass driver with per-target quarantine. Lake I/O is
injected here (the real readers/writer are covered by the integration
round-trip suite).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from structlog.testing import capture_logs

pytest.importorskip("trading_core", reason="greeks enrichment needs the trading_core wheel")

from brutex.data.fno_symbol import parse_fno_symbol
from brutex.data.greeks_enrich import (
    GREEKS_FORMULA_VERSION,
    Q_BIAS_ZERO,
    RATE_SOURCE_ID_CSV,
    RATE_SOURCE_ID_FIXED,
    RATE_SOURCE_ID_NONE,
    SECONDS_PER_YEAR_ACT365,
    STATUS_AT_EXPIRY,
    STATUS_BAD_INPUT,
    STATUS_BELOW_INTRINSIC,
    STATUS_NO_RATE,
    STATUS_NO_SOLUTION,
    STATUS_NO_SPOT,
    STATUS_OK,
    STYLE_AMERICAN_APPROX,
    STYLE_EUROPEAN_EXACT,
    STYLE_UNKNOWN,
    TTE_ACT365_EXPIRY_1530_IST,
    GreeksEnrichTarget,
    _SpotJoinMemo,
    _target_sort_key,
    current_inputs_pack,
    enrich_greeks_for_targets,
    enrich_option_bars,
    expiry_instant_utc,
    pack_greeks_provenance,
    rate_source_id_for,
    resolve_risk_free_source,
    stamp_is_current_inputs,
    t_years_act365,
    unpack_greeks_provenance,
)
from brutex.data.models import GREEKS_OVERLAY_FIELDS, FnoGreeksBar
from brutex.data.spot_join import SpotJoin
from brutex.options.greeks import compute_greeks
from brutex.options.risk_free import (
    CsvRateSource,
    FixedRateSource,
    RiskFreeError,
    RiskFreeRate,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from brutex.data.fno_symbol import FnoSymbolParts

_CE_SYMBOL = "NSE-NIFTY-25Jan24-21500-CE"
_PE_SYMBOL = "NSE-NIFTY-25Jan24-21500-PE"
_FUT_SYMBOL = "NSE-NIFTY-25Jan24-FUT"
_EXPIRY = date(2024, 1, 25)
#: 2024-01-15 09:15 IST — ten days before expiry, a comfortably-solvable T.
_BAR_TS = datetime(2024, 1, 15, 3, 45, tzinfo=UTC)
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
    close: str = "200",
    volume: int = 10,
    oi: int | None = 100,
    overlay: dict[str, object] | None = None,
) -> dict[str, object]:
    """One lake row shaped like the schema-tolerant option reader's output."""
    row: dict[str, object] = {
        "timestamp": ts,
        "open": float(close),
        "high": float(close) + 5.0,
        "low": float(close) - 5.0,
        "close": float(close),
        "volume": volume,
        "open_interest": oi,
    }
    for name in GREEKS_OVERLAY_FIELDS:
        row[name] = None
    if overlay:
        row.update(overlay)
    return row


def _enrich_one(
    row: dict[str, object],
    *,
    symbol: str = _CE_SYMBOL,
    spot: Decimal | None = _SPOT,
    rate_source: FixedRateSource | CsvRateSource = _RATE,
    style_flag: int = STYLE_EUROPEAN_EXACT,
) -> tuple[FnoGreeksBar, int]:
    """Run the pure core over ONE row; return the bar + its packed status."""
    ts = row["timestamp"]
    assert isinstance(ts, datetime)
    spot_by_ts = {} if spot is None else {ts.astimezone(UTC): spot}
    bars, _stats = enrich_option_bars(
        [row],
        parts=_parts(symbol),
        spot_by_ts=spot_by_ts,
        rate_source=rate_source,
        style_flag=style_flag,
    )
    (bar,) = bars
    pid = bar.greeks_provenance_id
    assert pid is not None
    return bar, unpack_greeks_provenance(pid).status


class TestProvenancePacking:
    """The Int32 provenance stamp — pack/unpack is a lossless inverse."""

    def test_round_trip_all_fields(self) -> None:
        pid = pack_greeks_provenance(
            rate_source_id=RATE_SOURCE_ID_CSV,
            rate_version=3,
            style_flag=STYLE_AMERICAN_APPROX,
            status=STATUS_BELOW_INTRINSIC,
            formula_version=7,
            q_bias_id=2,
            tte_convention_id=1,
        )
        fields = unpack_greeks_provenance(pid)
        assert fields.formula_version == 7
        assert fields.rate_source_id == RATE_SOURCE_ID_CSV
        assert fields.rate_version == 3
        assert fields.q_bias_id == 2
        assert fields.tte_convention_id == 1
        assert fields.style_flag == STYLE_AMERICAN_APPROX
        assert fields.status == STATUS_BELOW_INTRINSIC

    def test_defaults_pack_the_locked_conventions(self) -> None:
        fields = unpack_greeks_provenance(
            pack_greeks_provenance(
                rate_source_id=RATE_SOURCE_ID_FIXED,
                style_flag=STYLE_EUROPEAN_EXACT,
                status=STATUS_OK,
            )
        )
        assert fields.formula_version == GREEKS_FORMULA_VERSION
        assert fields.q_bias_id == Q_BIAS_ZERO
        assert fields.tte_convention_id == TTE_ACT365_EXPIRY_1530_IST
        assert fields.rate_version == 1

    def test_always_int32_safe(self) -> None:
        pid = pack_greeks_provenance(
            rate_source_id=15, rate_version=15, style_flag=7, status=15, formula_version=255
        )
        assert 0 <= pid < 2**31

    def test_rate_source_id_for(self) -> None:
        assert rate_source_id_for(FixedRateSource()) == RATE_SOURCE_ID_FIXED
        csv = CsvRateSource(dates=(date(2024, 1, 1),), rates=(0.06,), source="csv:x.csv")
        assert rate_source_id_for(csv) == RATE_SOURCE_ID_CSV

        class _Other:
            def rate_on(self, on_date: date) -> RiskFreeRate | None:
                del on_date
                return None

        assert rate_source_id_for(_Other()) == RATE_SOURCE_ID_NONE


class TestStampPredicate:
    """``stamp_is_current_inputs`` / ``current_inputs_pack`` — the exported
    staleness predicate the bulk-backfill scanner keys on
    (DEC-GREEKS-BULK-REENRICH-001)."""

    #: A stamp packed EXACTLY as the current pass packs an OK bar (v2 / fixed rate).
    _CURRENT_PID = pack_greeks_provenance(
        rate_source_id=RATE_SOURCE_ID_FIXED, style_flag=STYLE_EUROPEAN_EXACT, status=STATUS_OK
    )
    #: The same inputs stamped under formula v1 — the #1188 frozen-expired class.
    _V1_PID = pack_greeks_provenance(
        rate_source_id=RATE_SOURCE_ID_FIXED,
        style_flag=STYLE_EUROPEAN_EXACT,
        status=STATUS_OK,
        formula_version=1,
    )
    #: Current formula but a drifted rate VERSION — the inputs-drift class.
    _DRIFT_PID = pack_greeks_provenance(
        rate_source_id=RATE_SOURCE_ID_FIXED,
        style_flag=STYLE_EUROPEAN_EXACT,
        status=STATUS_OK,
        rate_version=2,
    )

    def test_none_is_never_current(self) -> None:
        assert stamp_is_current_inputs(None, rate_source=_RATE) is False

    def test_current_v2_stamp_is_current(self) -> None:
        assert stamp_is_current_inputs(self._CURRENT_PID, rate_source=_RATE) is True

    def test_v1_stamp_is_stale(self) -> None:
        assert stamp_is_current_inputs(self._V1_PID, rate_source=_RATE) is False

    def test_rate_version_drift_is_stale(self) -> None:
        assert stamp_is_current_inputs(self._DRIFT_PID, rate_source=_RATE) is False

    def test_rate_source_kind_drift_is_stale(self) -> None:
        csv_stamped = pack_greeks_provenance(
            rate_source_id=RATE_SOURCE_ID_CSV, style_flag=0, status=0
        )
        assert stamp_is_current_inputs(csv_stamped, rate_source=_RATE) is False

    def test_style_and_status_bits_do_not_affect_the_inputs_verdict(self) -> None:
        """Style + status are per-bar DESCRIPTIONS, not inputs (bits 20-26)."""
        demoted = pack_greeks_provenance(
            rate_source_id=RATE_SOURCE_ID_FIXED, style_flag=0, status=STATUS_NO_SPOT
        )
        assert stamp_is_current_inputs(demoted, rate_source=_RATE) is True

    def test_pack_matches_the_driver_gate_bytes(self) -> None:
        """The predicate's pack IS the gate's pack — ONE source of truth."""
        expected = pack_greeks_provenance(
            rate_source_id=RATE_SOURCE_ID_FIXED, style_flag=0, status=0
        ) & ((1 << 20) - 1)
        assert current_inputs_pack(_RATE) == expected

    def test_frozen_current_pack_golden(self) -> None:
        """Golden pin: the v2/fixed-rate input bits are exactly this integer."""
        # formula=2 | rate_source=1<<8 | rate_version=1<<12 | q_bias=1<<16 | tte=1<<18
        assert current_inputs_pack(_RATE) == 2 + (1 << 8) + (1 << 12) + (1 << 16) + (1 << 18)


class TestResolveRiskFreeSource:
    """The production ``r`` resolution — fixed default, CSV override, loud failure."""

    def test_unset_env_is_the_fixed_default(self) -> None:
        source = resolve_risk_free_source({})
        assert isinstance(source, FixedRateSource)
        assert source.rate == pytest.approx(0.065)

    def test_blank_env_is_the_fixed_default(self) -> None:
        assert isinstance(resolve_risk_free_source({"BRUTEX_RISK_FREE_CSV": "  "}), FixedRateSource)

    def test_csv_env_loads_the_step_source(self, tmp_path: Path) -> None:
        csv = tmp_path / "r.csv"
        csv.write_text("date,yield,tenor_days\n2024-01-01,0.068,91\n", encoding="utf-8")
        source = resolve_risk_free_source({"BRUTEX_RISK_FREE_CSV": str(csv)})
        assert isinstance(source, CsvRateSource)

    def test_broken_csv_raises_loudly(self, tmp_path: Path) -> None:
        with pytest.raises(RiskFreeError):
            resolve_risk_free_source({"BRUTEX_RISK_FREE_CSV": str(tmp_path / "missing.csv")})

    def test_default_env_reads_os_environ(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BRUTEX_RISK_FREE_CSV", raising=False)
        assert isinstance(resolve_risk_free_source(), FixedRateSource)


class TestTimeConventions:
    """The locked expiry-instant + Act/365 ``T`` convention."""

    def test_expiry_instant_is_1530_ist(self) -> None:
        instant = expiry_instant_utc(_EXPIRY)
        # 15:30 IST == 10:00 UTC of the same calendar day.
        assert instant == datetime(2024, 1, 25, 10, 0, tzinfo=UTC)

    def test_t_years_act365(self) -> None:
        expiry_at = expiry_instant_utc(_EXPIRY)
        t = t_years_act365(_BAR_TS, expiry_at)
        expected = (expiry_at - _BAR_TS).total_seconds() / SECONDS_PER_YEAR_ACT365
        assert t == expected
        assert t > 0

    def test_t_years_negative_after_expiry(self) -> None:
        after = expiry_instant_utc(_EXPIRY) + timedelta(minutes=1)
        assert t_years_act365(after, expiry_instant_utc(_EXPIRY)) < 0

    def test_naive_timestamp_read_as_utc(self) -> None:
        naive = _BAR_TS.replace(tzinfo=None)
        assert t_years_act365(naive, expiry_instant_utc(_EXPIRY)) == t_years_act365(
            _BAR_TS, expiry_instant_utc(_EXPIRY)
        )


class TestComputeBarStatuses:
    """Every plan-§15.8 stamped outcome — an honest NULL, never a fabrication."""

    def test_atm_call_solves(self) -> None:
        bar, status = _enrich_one(_row(close="200"))
        assert status == STATUS_OK
        assert bar.iv is not None and 0.001 <= bar.iv <= 5.0
        assert bar.delta is not None and 0.0 <= bar.delta <= 1.0
        assert bar.gamma is not None and bar.gamma >= 0.0
        assert bar.vega is not None and bar.vega >= 0.0
        assert bar.spot_at_bar == _SPOT
        assert bar.t_years_used == t_years_act365(_BAR_TS, expiry_instant_utc(_EXPIRY))
        assert bar.rate_used == pytest.approx(0.065)

    def test_atm_put_solves_with_negative_delta(self) -> None:
        bar, status = _enrich_one(_row(close="150"), symbol=_PE_SYMBOL)
        assert status == STATUS_OK
        assert bar.delta is not None and -1.0 <= bar.delta <= 0.0

    def test_solved_greeks_match_direct_kernel_call(self) -> None:
        """The stored tuple is EXACTLY the shared kernel's output (one source of truth)."""
        import math

        bar, status = _enrich_one(_row(close="200"))
        assert status == STATUS_OK
        t = t_years_act365(_BAR_TS, expiry_instant_utc(_EXPIRY))
        rate = _RATE.rate_on(date(2024, 1, 15))
        direct = compute_greeks(
            price=200.0,
            forward=float(_SPOT) * math.exp(rate.rate * t),
            strike=21500.0,
            t_years=t,
            rate=rate,
            is_call=True,
        )
        assert direct is not None
        assert (bar.iv, bar.delta, bar.gamma, bar.theta, bar.vega, bar.rho) == (
            direct.iv,
            direct.delta,
            direct.gamma,
            direct.theta,
            direct.vega,
            direct.rho,
        )

    def test_zero_volume_bar_still_solves(self) -> None:
        _bar, status = _enrich_one(_row(close="200", volume=0, oi=None))
        assert status == STATUS_OK

    def test_no_spot_minute_is_null(self) -> None:
        bar, status = _enrich_one(_row(), spot=None)
        assert status == STATUS_NO_SPOT
        assert bar.iv is None and bar.delta is None and bar.spot_at_bar is None
        assert bar.t_years_used is None and bar.rate_used is None

    def test_expiry_minute_t_floor_is_at_expiry(self) -> None:
        # 15:29 IST on expiry day — 60 s to the 15:30 close, inside the 600 s floor.
        ts = expiry_instant_utc(_EXPIRY) - timedelta(minutes=1)
        bar, status = _enrich_one(_row(ts))
        assert status == STATUS_AT_EXPIRY
        assert bar.iv is None
        # The spot is still materialized — the row never claims less than known.
        assert bar.spot_at_bar == _SPOT

    def test_bar_after_expiry_instant_is_at_expiry(self) -> None:
        ts = expiry_instant_utc(_EXPIRY) + timedelta(minutes=5)
        _bar, status = _enrich_one(_row(ts))
        assert status == STATUS_AT_EXPIRY

    def test_below_intrinsic_never_fed_to_solver(self) -> None:
        # Deep ITM: spot 21500, strike 20000 → intrinsic 1500; close 1400 < parity.
        bar, status = _enrich_one(_row(close="1400"), symbol="NSE-NIFTY-25Jan24-20000-CE")
        assert status == STATUS_BELOW_INTRINSIC
        assert bar.iv is None
        assert bar.spot_at_bar == _SPOT

    def test_below_discounted_parity_is_below_intrinsic(self) -> None:
        # Above the naive SPOT intrinsic (500) but below the DISCOUNTED no-arb
        # floor S − K·e^(−rT) ≈ 537 — the pre-screen refuses it before the solver.
        _bar, status = _enrich_one(_row(close="510"), symbol="NSE-NIFTY-25Jan24-21000-CE")
        assert status == STATUS_BELOW_INTRINSIC

    def test_deep_itm_put_above_its_discounted_floor_solves(self) -> None:
        # A deep-ITM PUT legitimately trades BELOW the naive K − S = 1500 (its
        # true floor is K·e^(−rT) − S ≈ 1459) — the discounted pre-screen must
        # NOT false-flag it (the stored-no-arb property regression).
        bar, status = _enrich_one(_row(close="1480"), symbol="NSE-NIFTY-25Jan24-23000-PE")
        assert status == STATUS_OK
        assert bar.delta is not None and bar.delta < -0.9

    def test_premium_above_upper_bound_is_no_solution(self) -> None:
        # A call premium above the discounted-forward upper bound (the spot) has
        # no IV — the kernel refuses it with an honest None.
        _bar, status = _enrich_one(_row(close="22000"))
        assert status == STATUS_NO_SOLUTION

    def test_deep_itm_above_parity_solves(self) -> None:
        # Discounted parity S − K·e^(−rT) ≈ 1535.6 here — 1560 clears it.
        bar, status = _enrich_one(_row(close="1560"), symbol="NSE-NIFTY-25Jan24-20000-CE")
        assert status == STATUS_OK
        assert bar.delta is not None and bar.delta > 0.9

    def test_deep_otm_penny_option_solves(self) -> None:
        bar, status = _enrich_one(_row(close="0.05"), symbol="NSE-NIFTY-25Jan24-24000-CE")
        assert status == STATUS_OK
        assert bar.iv is not None
        assert bar.delta is not None and bar.delta < 0.05

    def test_zero_price_bar_is_bad_input(self) -> None:
        # A zero close is a BAD INPUT stamped by the explicit non-positive
        # pre-check (before the parity floor screen), regardless of moneyness
        # — the ITM twin is pinned in test_greeks_enrich_audit_regressions
        # (DEC-GREEKS-CORRECTNESS-AUDIT-001 IVX-3).
        row = _row(close="0")
        row["open"] = 0.0
        row["high"] = 0.0
        row["low"] = 0.0
        _bar, status = _enrich_one(row, symbol="NSE-NIFTY-25Jan24-24000-CE")
        assert status == STATUS_BAD_INPUT

    def test_no_rate_for_date_is_null(self) -> None:
        late_csv = CsvRateSource(dates=(date(2025, 1, 1),), rates=(0.06,), source="csv:late.csv")
        bar, status = _enrich_one(_row(), rate_source=late_csv)
        assert status == STATUS_NO_RATE
        assert bar.iv is None and bar.rate_used is None

    def test_style_flag_stamped(self) -> None:
        for style in (STYLE_EUROPEAN_EXACT, STYLE_AMERICAN_APPROX, STYLE_UNKNOWN):
            bar, _status = _enrich_one(_row(), style_flag=style)
            pid = bar.greeks_provenance_id
            assert pid is not None
            assert unpack_greeks_provenance(pid).style_flag == style

    def test_pe_below_intrinsic(self) -> None:
        # PE intrinsic = strike − spot: strike 23000, spot 21500 → 1500; close 1400.
        _bar, status = _enrich_one(_row(close="1400"), symbol="NSE-NIFTY-25Jan24-23000-PE")
        assert status == STATUS_BELOW_INTRINSIC


class TestEnrichOptionBars:
    """The pure core's reuse / change bookkeeping (the idempotence machinery)."""

    def test_futures_parts_refused(self) -> None:
        with pytest.raises(ValueError, match="non-option"):
            enrich_option_bars(
                [_row()],
                parts=_parts(_FUT_SYMBOL),
                spot_by_ts={},
                rate_source=_RATE,
                style_flag=STYLE_UNKNOWN,
            )

    def test_fresh_rows_all_changed(self) -> None:
        rows = [_row(_BAR_TS), _row(_BAR_TS + timedelta(minutes=1))]
        spot_by_ts = {cast("datetime", r["timestamp"]): _SPOT for r in rows}
        bars, stats = enrich_option_bars(
            rows,
            parts=_parts(_CE_SYMBOL),
            spot_by_ts=spot_by_ts,
            rate_source=_RATE,
            style_flag=STYLE_EUROPEAN_EXACT,
        )
        assert len(bars) == 2
        assert stats.solved == 2 and stats.nulls == 0
        assert stats.reused == 0 and stats.changed == 2

    def test_current_ok_rows_are_reused_verbatim(self) -> None:
        # CHANGED PIN (DEC-GREEKS-CORRECTNESS-AUDIT-001): the reuse gate now
        # VALUE-checks the current rate + spot join, so the old "empty join
        # proves no recompute" trick would honestly demote to no_spot instead
        # of reusing. The no-recompute proof is a planted sentinel iv: a
        # recompute would replace it, a verbatim restate keeps it.
        first, _ = _enrich_one(_row(close="200"))
        assert first.iv is not None
        sentinel = first.iv + 0.01
        stored = _row(
            close="200",
            overlay={
                "iv": sentinel,
                "delta": first.delta,
                "gamma": first.gamma,
                "theta": first.theta,
                "vega": first.vega,
                "rho": first.rho,
                "spot_at_bar": float(_SPOT),
                "t_years_used": first.t_years_used,
                "rate_used": first.rate_used,
                "greeks_provenance_id": first.greeks_provenance_id,
            },
        )
        bars, stats = enrich_option_bars(
            [stored],
            parts=_parts(_CE_SYMBOL),
            spot_by_ts={_BAR_TS: _SPOT},
            rate_source=_RATE,
            style_flag=STYLE_EUROPEAN_EXACT,
        )
        assert stats.reused == 1 and stats.changed == 0
        assert stats.solved == 0 and stats.nulls == 0
        assert bars[0].iv == sentinel  # restated verbatim — NOT re-solved
        assert bars[0].greeks_provenance_id == first.greeks_provenance_id

    def test_stale_input_stamp_is_recomputed(self) -> None:
        stale_pid = pack_greeks_provenance(
            rate_source_id=RATE_SOURCE_ID_FIXED,
            style_flag=STYLE_EUROPEAN_EXACT,
            status=STATUS_OK,
            formula_version=GREEKS_FORMULA_VERSION + 1,  # a future formula bump
        )
        stored = _row(close="200", overlay={"greeks_provenance_id": stale_pid, "iv": 0.5})
        bars, stats = enrich_option_bars(
            [stored],
            parts=_parts(_CE_SYMBOL),
            spot_by_ts={_BAR_TS: _SPOT},
            rate_source=_RATE,
            style_flag=STYLE_EUROPEAN_EXACT,
        )
        assert stats.reused == 0 and stats.solved == 1 and stats.changed == 1
        assert bars[0].iv != 0.5

    def test_non_ok_status_is_reattempted_and_heals(self) -> None:
        # A bar stamped no_spot on a prior pass heals once the spot exists.
        no_spot_pid = pack_greeks_provenance(
            rate_source_id=RATE_SOURCE_ID_FIXED,
            style_flag=STYLE_EUROPEAN_EXACT,
            status=STATUS_NO_SPOT,
        )
        stored = _row(close="200", overlay={"greeks_provenance_id": no_spot_pid})
        bars, stats = enrich_option_bars(
            [stored],
            parts=_parts(_CE_SYMBOL),
            spot_by_ts={_BAR_TS: _SPOT},
            rate_source=_RATE,
            style_flag=STYLE_EUROPEAN_EXACT,
        )
        assert stats.solved == 1 and stats.changed == 1
        assert bars[0].iv is not None

    def test_identical_null_restate_is_unchanged(self) -> None:
        # An all-NA bar restated under the SAME inputs → changed == 0 (no write).
        first, status = _enrich_one(_row(), spot=None)
        assert status == STATUS_NO_SPOT
        stored = _row(overlay={"greeks_provenance_id": first.greeks_provenance_id})
        _bars, stats = enrich_option_bars(
            [stored],
            parts=_parts(_CE_SYMBOL),
            spot_by_ts={},
            rate_source=_RATE,
            style_flag=STYLE_EUROPEAN_EXACT,
        )
        assert stats.changed == 0 and stats.nulls == 1


class TestEnrichDriver:
    """The pass driver — tallies, quarantine, and the no-write fast path."""

    def _targets(self, *symbols: str) -> list[GreeksEnrichTarget]:
        return [
            GreeksEnrichTarget(exchange="NSE", symbol=s, start=_WINDOW[0], end=_WINDOW[1])
            for s in symbols
        ]

    def test_futures_and_undecodable_skipped(self, tmp_path: Path) -> None:
        with capture_logs() as logs:
            report = enrich_greeks_for_targets(
                targets=self._targets(_FUT_SYMBOL, "not-a-contract"),
                lake_root=tmp_path,
                rate_source=_RATE,
                read_bars=lambda _t: [_row()],
                read_spot=lambda _t, _p: SpotJoin({}, None),
                write_bars=lambda _t, _b: 0,
            )
        assert report.futures_skipped == 1
        assert report.undecodable == 1
        assert report.enriched == 0
        assert report.targets == 2
        assert any(e["event"] == "greeks_enrich.target_undecodable" for e in logs)

    def test_pivot_junk_expiry_counts_undecodable_and_warns(self, tmp_path: Path) -> None:
        # DEC-FNO-EXPIRY-YEAR-PIVOT-001 regression: "30Jun75" used to pivot to a
        # fabricated 1975 expiry and be ENRICHED as a real contract. The year-window
        # reject makes parse_fno_symbol return None, so the existing undecodable
        # counter + structlog WARNING fire (the honest reject surface).
        with capture_logs() as logs:
            report = enrich_greeks_for_targets(
                targets=self._targets("NSE-NIFTY-30Jun75-24000-CE"),
                lake_root=tmp_path,
                rate_source=_RATE,
                read_bars=lambda _t: [_row()],
                read_spot=lambda _t, _p: SpotJoin({}, None),
                write_bars=lambda _t, _b: 0,
            )
        assert report.undecodable == 1
        assert report.enriched == 0
        assert any(
            e["event"] == "greeks_enrich.target_undecodable"
            and e["symbol"] == "NSE-NIFTY-30Jun75-24000-CE"
            for e in logs
        )

    def test_zero_targets_is_a_zero_report(self, tmp_path: Path) -> None:
        report = enrich_greeks_for_targets(targets=[], lake_root=tmp_path, rate_source=_RATE)
        assert report.targets == 0 and report.bars_written == 0
        assert report.spot_reads == 0 and report.spot_hits == 0

    def test_no_bars_is_an_honest_no_op(self, tmp_path: Path) -> None:
        report = enrich_greeks_for_targets(
            targets=self._targets(_CE_SYMBOL),
            lake_root=tmp_path,
            rate_source=_RATE,
            read_bars=lambda _t: [],
            read_spot=lambda _t, _p: SpotJoin({}, None),
            write_bars=lambda _t, _b: 0,
        )
        assert report.no_bars == 1 and report.enriched == 0

    def test_enriched_target_writes_and_tallies(self, tmp_path: Path) -> None:
        written: list[list[FnoGreeksBar]] = []

        def _write(_t: GreeksEnrichTarget, bars: object) -> int:
            written.append(list(bars))  # type: ignore[call-overload]
            return len(written[-1])

        report = enrich_greeks_for_targets(
            targets=self._targets(_CE_SYMBOL),
            lake_root=tmp_path,
            rate_source=_RATE,
            read_bars=lambda _t: [_row(close="200")],
            read_spot=lambda _t, _p: SpotJoin({_BAR_TS: _SPOT}, "INDEX"),
            write_bars=_write,
        )
        assert report.enriched == 1
        assert report.bars_written == 1 and report.bars_solved == 1
        assert written[0][0].iv is not None
        pid = written[0][0].greeks_provenance_id
        assert pid is not None
        assert unpack_greeks_provenance(pid).style_flag == STYLE_EUROPEAN_EXACT

    def test_cash_segment_stamps_american_approx(self, tmp_path: Path) -> None:
        written: list[FnoGreeksBar] = []

        def _write(_t: GreeksEnrichTarget, bars: object) -> int:
            written.extend(bars)  # type: ignore[call-overload]
            return len(written)

        enrich_greeks_for_targets(
            targets=self._targets("NSE-RELIANCE-25Jan24-2800-CE"),
            lake_root=tmp_path,
            rate_source=_RATE,
            read_bars=lambda _t: [_row(close="55")],
            read_spot=lambda _t, _p: SpotJoin({_BAR_TS: Decimal("2800")}, "CASH"),
            write_bars=_write,
        )
        pid = written[0].greeks_provenance_id
        assert pid is not None
        assert unpack_greeks_provenance(pid).style_flag == STYLE_AMERICAN_APPROX

    def test_all_na_chain_is_written_with_null_stamps(self, tmp_path: Path) -> None:
        """A whole chain with NO spot anywhere → every bar an honest stamped NULL."""
        written: dict[str, list[FnoGreeksBar]] = {}

        def _write(t: GreeksEnrichTarget, bars: object) -> int:
            written[t.symbol] = list(bars)  # type: ignore[call-overload]
            return len(written[t.symbol])

        report = enrich_greeks_for_targets(
            targets=self._targets(_CE_SYMBOL, _PE_SYMBOL),
            lake_root=tmp_path,
            rate_source=_RATE,
            read_bars=lambda _t: [_row()],
            read_spot=lambda _t, _p: SpotJoin({}, None),
            write_bars=_write,
        )
        assert report.enriched == 2 and report.bars_null == 2 and report.bars_solved == 0
        for bars in written.values():
            assert bars[0].iv is None
            pid = bars[0].greeks_provenance_id
            assert pid is not None
            fields = unpack_greeks_provenance(pid)
            assert fields.status == STATUS_NO_SPOT
            assert fields.style_flag == STYLE_UNKNOWN

    def test_per_target_error_is_quarantined(self, tmp_path: Path) -> None:
        def _read(t: GreeksEnrichTarget) -> list[dict[str, object]]:
            if t.symbol == _CE_SYMBOL:
                raise OSError("corrupt partition")
            return [_row(close="150")]

        with capture_logs() as logs:
            report = enrich_greeks_for_targets(
                targets=self._targets(_CE_SYMBOL, _PE_SYMBOL),
                lake_root=tmp_path,
                rate_source=_RATE,
                read_bars=_read,
                read_spot=lambda _t, _p: SpotJoin({_BAR_TS: _SPOT}, "INDEX"),
                write_bars=lambda _t, b: len(list(b)),  # type: ignore[arg-type]
            )
        assert report.errored == 1
        assert report.enriched == 1  # the healthy sibling still landed
        assert any(e["event"] == "greeks_enrich.target_errored" for e in logs)

    def test_already_current_target_never_writes(self, tmp_path: Path) -> None:
        first, _ = _enrich_one(_row(close="200"))
        stored = _row(
            close="200",
            overlay={
                "iv": first.iv,
                "delta": first.delta,
                "gamma": first.gamma,
                "theta": first.theta,
                "vega": first.vega,
                "rho": first.rho,
                "spot_at_bar": float(_SPOT),
                "t_years_used": first.t_years_used,
                "rate_used": first.rate_used,
                "greeks_provenance_id": first.greeks_provenance_id,
            },
        )

        def _write(_t: GreeksEnrichTarget, _b: object) -> int:
            raise AssertionError("an already-current target must NOT be written")

        report = enrich_greeks_for_targets(
            targets=self._targets(_CE_SYMBOL),
            lake_root=tmp_path,
            rate_source=_RATE,
            read_bars=lambda _t: [stored],
            read_spot=lambda _t, _p: SpotJoin({_BAR_TS: _SPOT}, "INDEX"),
            write_bars=_write,
        )
        assert report.already_current == 1 and report.enriched == 0


# ── DEC-GREEKS-SPOT-MEMO-001: sorted targets + capacity-2 spot-read LRU ───────

_NIFTY_21400_CE = "NSE-NIFTY-25Jan24-21400-CE"
_NIFTY_21600_CE = "NSE-NIFTY-25Jan24-21600-CE"
_BANKNIFTY_CE = "NSE-BANKNIFTY-25Jan24-46000-CE"
_RELIANCE_CE = "NSE-RELIANCE-25Jan24-2800-CE"
_SPOT_BY_UNDERLYING = {
    "NIFTY": _SPOT,
    "BANKNIFTY": Decimal("46000"),
    "RELIANCE": Decimal("2800"),
}


def _target(
    symbol: str, *, window: tuple[date, date] = _WINDOW, exchange: str = "NSE"
) -> GreeksEnrichTarget:
    return GreeksEnrichTarget(exchange=exchange, symbol=symbol, start=window[0], end=window[1])


class TestTargetSortKey:
    """`_target_sort_key` — the (exchange, underlying, start, end, symbol) order."""

    def test_decodable_symbol_keys_on_the_parsed_underlying(self) -> None:
        assert _target_sort_key(_target(_CE_SYMBOL)) == (
            "NSE",
            "NIFTY",
            _WINDOW[0],
            _WINDOW[1],
            _CE_SYMBOL,
        )

    def test_undecodable_symbol_falls_back_to_the_raw_symbol(self) -> None:
        assert _target_sort_key(_target("not-a-contract")) == (
            "NSE",
            "not-a-contract",
            _WINDOW[0],
            _WINDOW[1],
            "not-a-contract",
        )


class TestSpotJoinMemo:
    """The capacity-2 LRU over the resolved spot reader — direct unit pins."""

    def _memo(self, capacity: int = 2) -> tuple[_SpotJoinMemo, list[str]]:
        calls: list[str] = []

        def _reader(_t: GreeksEnrichTarget, p: FnoSymbolParts) -> SpotJoin:
            calls.append(p.underlying)
            return SpotJoin({}, None)

        return _SpotJoinMemo(_reader, capacity=capacity), calls

    def _pair(self, symbol: str) -> tuple[GreeksEnrichTarget, FnoSymbolParts]:
        return _target(symbol), _parts(symbol)

    def test_shared_key_is_one_read_and_the_same_join_instance(self) -> None:
        memo, calls = self._memo()
        first = memo.get(*self._pair(_CE_SYMBOL))
        second = memo.get(*self._pair(_NIFTY_21600_CE))  # same (underlying, window) key
        assert second is first  # the cached SpotJoin is aliased, never re-read
        assert calls == ["NIFTY"]
        assert memo.reads == 1 and memo.hits == 1

    def test_capacity_two_survives_an_a_b_a_pattern(self) -> None:
        memo, _calls = self._memo(capacity=2)
        for symbol in (_CE_SYMBOL, _BANKNIFTY_CE, _CE_SYMBOL):
            memo.get(*self._pair(symbol))
        assert memo.reads == 2 and memo.hits == 1
        # The SAME pattern at capacity 1 re-reads A — kills the 2→1 mutant.
        memo1, _calls1 = self._memo(capacity=1)
        for symbol in (_CE_SYMBOL, _BANKNIFTY_CE, _CE_SYMBOL):
            memo1.get(*self._pair(symbol))
        assert memo1.reads == 3 and memo1.hits == 0

    def test_third_key_evicts_the_least_recently_used(self) -> None:
        # A,B,A,C,B: the A hit refreshes A's recency, so C evicts B (the LRU),
        # and the final B is a MISS. Without move_to_end C would evict A and
        # the final B would HIT (reads 3 / hits 2) — the recency mutant dies.
        memo, calls = self._memo()
        for symbol in (_CE_SYMBOL, _BANKNIFTY_CE, _CE_SYMBOL, _RELIANCE_CE, _BANKNIFTY_CE):
            memo.get(*self._pair(symbol))
        assert memo.reads == 4 and memo.hits == 1
        assert calls == ["NIFTY", "BANKNIFTY", "RELIANCE", "BANKNIFTY"]

    def test_eviction_bounds_the_cache_to_capacity(self) -> None:
        memo, _calls = self._memo()
        for symbol in (_CE_SYMBOL, _BANKNIFTY_CE, _RELIANCE_CE, _CE_SYMBOL):
            memo.get(*self._pair(symbol))  # the 3rd key evicted A → A re-reads
        assert memo.reads == 4 and memo.hits == 0

    def test_raising_read_counts_the_attempt_and_caches_nothing(self) -> None:
        def _boom(_t: GreeksEnrichTarget, _p: FnoSymbolParts) -> SpotJoin:
            raise OSError("corrupt spot partition")

        memo = _SpotJoinMemo(_boom)
        with pytest.raises(OSError, match="corrupt spot partition"):
            memo.get(*self._pair(_CE_SYMBOL))
        with pytest.raises(OSError, match="corrupt spot partition"):
            memo.get(*self._pair(_CE_SYMBOL))  # nothing was cached → re-attempted
        assert memo.reads == 2 and memo.hits == 0


class TestDriverSpotMemo:
    """The driver end of DEC-GREEKS-SPOT-MEMO-001 — sort + memo + counters."""

    def _counting_read_spot(
        self,
    ) -> tuple[list[str], Callable[[GreeksEnrichTarget, FnoSymbolParts], SpotJoin]]:
        calls: list[str] = []

        def _read(_t: GreeksEnrichTarget, p: FnoSymbolParts) -> SpotJoin:
            calls.append(p.underlying)
            return SpotJoin({_BAR_TS: _SPOT_BY_UNDERLYING[p.underlying]}, "INDEX")

        return calls, _read

    def test_chain_sharing_one_window_reads_spot_once(self, tmp_path: Path) -> None:
        calls, read_spot = self._counting_read_spot()
        written: dict[str, FnoGreeksBar] = {}

        def _write(t: GreeksEnrichTarget, bars: object) -> int:
            (written[t.symbol],) = list(bars)  # type: ignore[call-overload]
            return 1

        report = enrich_greeks_for_targets(
            targets=[
                _target(_NIFTY_21400_CE),
                _target(_CE_SYMBOL),
                _target(_NIFTY_21600_CE),
            ],
            lake_root=tmp_path,
            rate_source=_RATE,
            read_bars=lambda _t: [_row(close="200")],
            read_spot=read_spot,
            write_bars=_write,
        )
        assert calls == ["NIFTY"]  # ONE performed read for the whole chain
        assert report.spot_reads == 1 and report.spot_hits == 2
        assert report.enriched == 3 and report.bars_solved == 3
        for bar in written.values():  # per-target results identical to unmemoised
            assert bar.iv is not None and bar.spot_at_bar == _SPOT

    def test_distinct_keys_each_read_once(self, tmp_path: Path) -> None:
        calls, read_spot = self._counting_read_spot()
        report = enrich_greeks_for_targets(
            targets=[
                _target(_CE_SYMBOL),
                _target(_BANKNIFTY_CE),
                _target(_RELIANCE_CE),
            ],
            lake_root=tmp_path,
            rate_source=_RATE,
            read_bars=lambda _t: [_row(close="200")],
            read_spot=read_spot,
            write_bars=lambda _t, b: len(list(b)),  # type: ignore[arg-type]
        )
        assert sorted(calls) == ["BANKNIFTY", "NIFTY", "RELIANCE"]
        assert report.spot_reads == 3 and report.spot_hits == 0

    def test_interleaved_targets_are_sorted_for_the_memo(self, tmp_path: Path) -> None:
        # 3 distinct keys INTERLEAVED around a shared-key pair: without the
        # sorted() grouping the capacity-2 LRU evicts NIFTY before its second
        # target arrives (4 reads / 0 hits) — this pins the sort's teeth.
        calls, read_spot = self._counting_read_spot()
        report = enrich_greeks_for_targets(
            targets=[
                _target(_NIFTY_21400_CE),
                _target(_BANKNIFTY_CE),
                _target(_RELIANCE_CE),
                _target(_NIFTY_21600_CE),
            ],
            lake_root=tmp_path,
            rate_source=_RATE,
            read_bars=lambda _t: [_row(close="200")],
            read_spot=read_spot,
            write_bars=lambda _t, b: len(list(b)),  # type: ignore[arg-type]
        )
        assert report.spot_reads == 3 and report.spot_hits == 1
        assert calls == ["BANKNIFTY", "NIFTY", "RELIANCE"]

    def test_processing_order_is_deterministic(self, tmp_path: Path) -> None:
        late_window = (date(2024, 1, 5), _WINDOW[1])
        shuffled = [
            _target(_RELIANCE_CE),
            _target(_NIFTY_21600_CE, window=late_window),
            _target(_CE_SYMBOL),
            _target(_BANKNIFTY_CE),
        ]
        with capture_logs() as logs:
            enrich_greeks_for_targets(
                targets=shuffled,
                lake_root=tmp_path,
                rate_source=_RATE,
                read_bars=lambda _t: [_row(close="200")],
                read_spot=self._counting_read_spot()[1],
                write_bars=lambda _t, b: len(list(b)),  # type: ignore[arg-type]
            )
        order = [e["symbol"] for e in logs if e["event"] == "greeks_enrich.target_enriched"]
        # (exchange, underlying, start, end, symbol): BANKNIFTY < NIFTY < RELIANCE,
        # and within NIFTY the earlier window start sorts first.
        assert order == [_BANKNIFTY_CE, _CE_SYMBOL, _NIFTY_21600_CE, _RELIANCE_CE]

    def test_pre_read_skips_never_touch_the_counters(self, tmp_path: Path) -> None:
        report = enrich_greeks_for_targets(
            targets=[
                _target(_FUT_SYMBOL),
                _target("not-a-contract"),
                _target(_CE_SYMBOL),  # no stored bars → skipped before the read
            ],
            lake_root=tmp_path,
            rate_source=_RATE,
            read_bars=lambda _t: [],
            read_spot=lambda _t, _p: SpotJoin({}, None),
            write_bars=lambda _t, _b: 0,
        )
        assert report.futures_skipped == 1 and report.undecodable == 1 and report.no_bars == 1
        assert report.spot_reads == 0 and report.spot_hits == 0

    def test_raising_spot_reader_quarantines_and_counts_attempts(self, tmp_path: Path) -> None:
        def _boom(_t: GreeksEnrichTarget, _p: FnoSymbolParts) -> SpotJoin:
            raise OSError("corrupt spot partition")

        with capture_logs() as logs:
            report = enrich_greeks_for_targets(
                targets=[_target(_CE_SYMBOL), _target(_NIFTY_21600_CE)],  # same memo key
                lake_root=tmp_path,
                rate_source=_RATE,
                read_bars=lambda _t: [_row(close="200")],
                read_spot=_boom,
                write_bars=lambda _t, _b: 0,
            )
        # A raising read caches nothing, so the shared-key sibling re-attempts:
        # both targets quarantined, both attempts counted, zero hits.
        assert report.errored == 2
        assert report.spot_reads == 2 and report.spot_hits == 0
        assert sum(1 for e in logs if e["event"] == "greeks_enrich.target_errored") == 2
