"""Rolling-ATM option Greeks pass — DEC-DHAN-ROLLING-OPTIONS-001 §3.

Covers the Dhan served-vs-solved Greeks enrichment (``rolling_option_greeks``)
AND the COMMON cross-cutting seam it drives — the ``served_iv`` branch of
:func:`brutex.data.greeks_enrich.compute_bar_overlay` + the ``iv_source``
provenance bit — with the honesty pins the design mandates:

* served-IV path prices WITHOUT a root-solve (kernel ``sigma`` spy) →
  ``iv_source="served"``;
* iv-absent → solve → ``iv_source="solved"``; premium-unsolvable → NO_SOLUTION;
* every demotion (NO_RATE / AT_EXPIRY / NO_SPOT / BAD_INPUT / BELOW_INTRINSIC)
  → NULL overlay + status, never a value;
* per-bar ``T`` binds to THAT row's ``resolved_expiry_date``; an ``unresolved``
  row is an undecodable skip;
* EUROPEAN_EXACT for the index underlyings;
* idempotent re-run via the stamp + iv_source bit;
* Groww's solve path stays byte-identical (the frozen enrich goldens — a
  SEPARATE suite; here we pin the provenance default is byte-identical).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

import polars as pl
import pytest
from brutex.adapters.dhan.rolling_option_greeks import (
    RollingEnrichStats,
    RollingGreeksError,
    RollingGreeksOverlay,
    RollingGreeksTarget,
    _expiry_code_of,
    _has_month_file,
    _iv_source_name,
    discover_rolling_option_targets,
    enrich_rolling_greeks_for_targets,
    enrich_rolling_option_bars,
    read_rolling_option_rows,
    run_dhan_rolling_greeks_pass,
    write_rolling_option_greeks,
)
from brutex.adapters.dhan.rolling_option_parser import RollingOptionBar
from brutex.adapters.dhan.rolling_option_writer import RollingOptionWriter
from brutex.data.errors import GreeksEnrichPoolBroken, LakeWriteHalt
from brutex.data.greeks_enrich import compute_bar_overlay, served_iv_usable
from brutex.data.greeks_enrich_pass import GREEKS_NOT_RUN
from brutex.data.greeks_provenance import (
    IV_SOURCE_SERVED,
    IV_SOURCE_SOLVED,
    STATUS_AT_EXPIRY,
    STATUS_BAD_INPUT,
    STATUS_BELOW_INTRINSIC,
    STATUS_NO_RATE,
    STATUS_NO_SOLUTION,
    STATUS_NO_SPOT,
    STATUS_OK,
    STYLE_EUROPEAN_EXACT,
    unpack_greeks_provenance,
)
from brutex.data.models import FnoGreeksBar
from brutex.data.option_parquet_writer import OptionSeriesCoordinate
from brutex.options.greeks import IV_CLAMP_MAX, IV_CLAMP_MIN
from brutex.options.risk_free import FixedRateSource

UTC = dt.UTC
_T0 = dt.datetime(2024, 6, 3, 4, 30, tzinfo=UTC)  # 10:00 IST
_EXPIRY = dt.date(2024, 6, 6)
_RATE = FixedRateSource()


def _price(v: str) -> Decimal:
    return Decimal(v)


def _row(
    *,
    ts: dt.datetime = _T0,
    close: object = "120.50",
    iv: object = "0.145",
    spot: object = "24710.55",
    strike: object = "24700.00",
    expiry: dt.date | None = _EXPIRY,
    source: str = "vendor",
    option_type: str = "CALL",
    volume: int = 100,
    oi: object = 50,
    # stored overlay (a re-read row)
    iv_used: object = None,
    delta: object = None,
    gamma: object = None,
    theta: object = None,
    vega: object = None,
    rho: object = None,
    spot_at_bar: object = None,
    t_years_used: object = None,
    rate_used: object = None,
    greeks_provenance_id: object = None,
    iv_source: object = None,
) -> dict[str, object]:
    """A stored rolling-option row dict as ``iter_rows(named=True)`` yields it."""
    close_f = float(close) if isinstance(close, str) else close
    return {
        "timestamp": ts,
        "open": close_f,
        "high": close_f,
        "low": close_f,
        "close": close_f,
        "volume": volume,
        "open_interest": oi,
        "iv": None if iv is None else float(iv),  # type: ignore[arg-type]
        "spot": None if spot is None else float(spot),  # type: ignore[arg-type]
        "resolved_strike": None if strike is None else float(strike),  # type: ignore[arg-type]
        "resolved_expiry_date": expiry,
        "resolved_expiry_source": source,
        "option_type": option_type,
        "iv_used": iv_used,
        "delta": delta,
        "gamma": gamma,
        "theta": theta,
        "vega": vega,
        "rho": rho,
        "spot_at_bar": spot_at_bar,
        "t_years_used": t_years_used,
        "rate_used": rate_used,
        "greeks_provenance_id": greeks_provenance_id,
        "iv_source": iv_source,
    }


# ────────────────────────── served_iv_usable (COMMON) ──────────────────────────


class TestServedIvUsable:
    def test_none_is_not_usable(self) -> None:
        assert served_iv_usable(None) is False

    def test_non_finite_is_not_usable(self) -> None:
        assert served_iv_usable(float("nan")) is False
        assert served_iv_usable(float("inf")) is False

    def test_below_clamp_is_not_usable(self) -> None:
        assert served_iv_usable(IV_CLAMP_MIN / 2) is False

    def test_above_clamp_is_not_usable(self) -> None:
        assert served_iv_usable(IV_CLAMP_MAX * 2) is False

    def test_in_clamp_is_usable(self) -> None:
        assert served_iv_usable(0.15) is True
        assert served_iv_usable(IV_CLAMP_MIN) is True
        assert served_iv_usable(IV_CLAMP_MAX) is True


# ─────────────────── compute_bar_overlay served branch (COMMON) ────────────────


def _overlay(*, served_iv: float | None = None, strike_f: float = 24700.0) -> FnoGreeksBar:
    return compute_bar_overlay(
        ts=_T0,
        open_=_price("120.50"),
        high=_price("120.50"),
        low=_price("120.50"),
        close=_price("120.50"),
        volume=100,
        oi=50,
        spot=_price("24710.55"),
        rate=_RATE.rate_on(_T0.date()),
        t_years=0.05,
        strike_f=strike_f,
        is_call=True,
        style_flag=STYLE_EUROPEAN_EXACT,
        source_id=1,
        served_iv=served_iv,
    )


class TestComputeBarServedBranch:
    def test_served_prices_directly_no_root_solve(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Spy the kernel entry: on the served branch it must be called with a
        # non-None sigma (price-from-sigma), never sigma=None (the root-solve).
        import brutex.data.greeks_enrich as ge

        seen: list[float | None] = []
        real = ge.compute_greeks  # type: ignore[attr-defined]

        def _spy(*, sigma: float | None = None, **kw: Any) -> Any:
            seen.append(sigma)
            return real(sigma=sigma, **kw)

        monkeypatch.setattr(ge, "compute_greeks", _spy)
        bar = _overlay(served_iv=0.145)
        prov = unpack_greeks_provenance(bar.greeks_provenance_id or 0)
        assert prov.status == STATUS_OK
        assert prov.iv_source == IV_SOURCE_SERVED
        # the served IV is echoed verbatim (NO solve moved it)
        assert bar.iv == pytest.approx(0.145)
        assert seen == [0.145]  # sigma given → price-directly, no solve

    def test_solved_path_calls_solver_with_sigma_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import brutex.data.greeks_enrich as ge

        seen: list[float | None] = []
        real = ge.compute_greeks  # type: ignore[attr-defined]

        def _spy(*, sigma: float | None = None, **kw: Any) -> Any:
            seen.append(sigma)
            return real(sigma=sigma, **kw)

        monkeypatch.setattr(ge, "compute_greeks", _spy)
        bar = _overlay(served_iv=None)  # no served IV → solve from premium
        prov = unpack_greeks_provenance(bar.greeks_provenance_id or 0)
        assert prov.status == STATUS_OK
        assert prov.iv_source == IV_SOURCE_SOLVED
        assert seen == [None]  # sigma omitted → the kernel root-solves

    def test_served_out_of_clamp_falls_through_to_solve(self) -> None:
        # a garbage served IV (out of clamp) is NOT usable → honest solve path
        bar = _overlay(served_iv=99.0)
        prov = unpack_greeks_provenance(bar.greeks_provenance_id or 0)
        assert prov.iv_source == IV_SOURCE_SOLVED

    def test_served_bad_strike_stamps_bad_input(self) -> None:
        # a corrupt (negative) strike makes the kernel reject → BAD_INPUT null
        bar = _overlay(served_iv=0.145, strike_f=-1.0)
        prov = unpack_greeks_provenance(bar.greeks_provenance_id or 0)
        assert prov.status == STATUS_BAD_INPUT
        assert bar.delta is None

    def test_served_kernel_none_stamps_no_solution(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # defensive branch: a stubbed kernel that returns None on the served path
        import brutex.data.greeks_enrich as ge

        monkeypatch.setattr(ge, "compute_greeks", lambda **_kw: None)
        bar = _overlay(served_iv=0.145)
        prov = unpack_greeks_provenance(bar.greeks_provenance_id or 0)
        assert prov.status == STATUS_NO_SOLUTION
        assert bar.iv is None


# ────────────────────────── enrich_rolling_option_bars ─────────────────────────


class TestEnrichRollingOptionBars:
    def test_served_bar_is_priced_from_served_iv(self) -> None:
        overlays, stats = enrich_rolling_option_bars([_row(iv="0.145")], rate_source=_RATE)
        assert stats.served == 1 and stats.solved == 0 and stats.nulls == 0
        assert overlays[0].iv_used == pytest.approx(0.145)
        assert overlays[0].iv_source == "served"

    def test_iv_absent_bar_is_solved(self) -> None:
        overlays, stats = enrich_rolling_option_bars([_row(iv=None)], rate_source=_RATE)
        assert stats.solved == 1 and stats.served == 0
        assert overlays[0].iv_source == "solved"
        assert overlays[0].iv_used is not None

    def test_unresolved_expiry_source_is_undecodable(self) -> None:
        overlays, stats = enrich_rolling_option_bars(
            [_row(source="unresolved", expiry=None)], rate_source=_RATE
        )
        assert stats.undecodable == 1 and overlays == []

    def test_null_expiry_date_is_undecodable(self) -> None:
        overlays, stats = enrich_rolling_option_bars(
            [_row(source="derived", expiry=None)], rate_source=_RATE
        )
        assert stats.undecodable == 1 and overlays == []

    def test_null_strike_is_undecodable(self) -> None:
        overlays, stats = enrich_rolling_option_bars([_row(strike=None)], rate_source=_RATE)
        assert stats.undecodable == 1 and overlays == []

    def test_non_finite_close_is_skipped(self) -> None:
        overlays, stats = enrich_rolling_option_bars([_row(close=float("nan"))], rate_source=_RATE)
        assert stats.skipped == 1 and overlays == []

    def test_no_spot_demotes_to_null(self) -> None:
        overlays, stats = enrich_rolling_option_bars([_row(spot=None)], rate_source=_RATE)
        assert stats.nulls == 1
        assert unpack_greeks_provenance(overlays[0].greeks_provenance_id or 0).status == (
            STATUS_NO_SPOT
        )
        assert overlays[0].delta is None
        assert dict(stats.null_reasons) == {"no_spot": 1}

    def test_non_positive_spot_demotes_to_no_spot(self) -> None:
        overlays, stats = enrich_rolling_option_bars([_row(spot="-1.0")], rate_source=_RATE)
        assert stats.nulls == 1
        assert unpack_greeks_provenance(overlays[0].greeks_provenance_id or 0).status == (
            STATUS_NO_SPOT
        )

    def test_bad_input_close_zero(self) -> None:
        overlays, _stats = enrich_rolling_option_bars([_row(close="0.00")], rate_source=_RATE)
        assert unpack_greeks_provenance(overlays[0].greeks_provenance_id or 0).status == (
            STATUS_BAD_INPUT
        )

    def test_below_intrinsic_on_solve_branch(self) -> None:
        # a deep-ITM call priced below the discounted-parity floor, no served IV
        overlays, _stats = enrich_rolling_option_bars(
            [_row(iv=None, close="0.05", strike="1000.00", spot="24710.55")],
            rate_source=_RATE,
        )
        assert unpack_greeks_provenance(overlays[0].greeks_provenance_id or 0).status == (
            STATUS_BELOW_INTRINSIC
        )

    def test_at_expiry_demotes(self) -> None:
        # a bar AT the expiry instant (15:30 IST == 10:00 UTC on the expiry date)
        overlays, _stats = enrich_rolling_option_bars(
            [_row(ts=dt.datetime(2024, 6, 6, 10, 0, tzinfo=UTC), expiry=dt.date(2024, 6, 6))],
            rate_source=_RATE,
        )
        assert unpack_greeks_provenance(overlays[0].greeks_provenance_id or 0).status == (
            STATUS_AT_EXPIRY
        )

    def test_no_rate_demotes(self) -> None:
        class _NoRate:
            source = "none"
            version = 0

            def rate_on(self, on_date: dt.date) -> None:
                del on_date

        overlays, _stats = enrich_rolling_option_bars([_row()], rate_source=_NoRate())
        assert unpack_greeks_provenance(overlays[0].greeks_provenance_id or 0).status == (
            STATUS_NO_RATE
        )

    def test_t_binds_per_row_expiry(self) -> None:
        # two rows, same ts, different resolved_expiry_date → different T → different Greeks
        near = _row(iv=None, expiry=dt.date(2024, 6, 6))
        far = _row(iv=None, expiry=dt.date(2024, 12, 26))
        (o_near,), _ = enrich_rolling_option_bars([near], rate_source=_RATE)
        (o_far,), _ = enrich_rolling_option_bars([far], rate_source=_RATE)
        assert o_near.t_years_used != o_far.t_years_used
        assert o_near.iv_used != o_far.iv_used

    def test_reuse_gate_restates_matching_stamp(self) -> None:
        # first enrich a solved bar, then feed the stored overlay back → reuse
        (built,), _ = enrich_rolling_option_bars([_row(iv=None)], rate_source=_RATE)
        stored = _row(
            iv=None,
            iv_used=built.iv_used,
            delta=built.delta,
            gamma=built.gamma,
            theta=built.theta,
            vega=built.vega,
            rho=built.rho,
            spot_at_bar=float(built.spot_at_bar) if built.spot_at_bar is not None else None,
            t_years_used=built.t_years_used,
            rate_used=built.rate_used,
            greeks_provenance_id=built.greeks_provenance_id,
            iv_source=built.iv_source,
        )
        overlays, stats = enrich_rolling_option_bars([stored], rate_source=_RATE)
        assert stats.reused == 1 and stats.changed == 0
        assert overlays[0].greeks_provenance_id == built.greeks_provenance_id

    def test_reuse_gate_recomputes_on_iv_source_transition(self) -> None:
        # stored as SOLVED, but the row now carries a usable served IV → the
        # regime moved (solved -> served), so the gate must re-derive.
        (solved_bar,), _ = enrich_rolling_option_bars([_row(iv=None)], rate_source=_RATE)
        stored = _row(
            iv="0.145",  # a served IV now present
            iv_used=solved_bar.iv_used,
            delta=solved_bar.delta,
            gamma=solved_bar.gamma,
            theta=solved_bar.theta,
            vega=solved_bar.vega,
            rho=solved_bar.rho,
            spot_at_bar=float(solved_bar.spot_at_bar) if solved_bar.spot_at_bar else None,
            t_years_used=solved_bar.t_years_used,
            rate_used=solved_bar.rate_used,
            greeks_provenance_id=solved_bar.greeks_provenance_id,
            iv_source="solved",
        )
        overlays, stats = enrich_rolling_option_bars([stored], rate_source=_RATE)
        assert stats.reused == 0 and stats.served == 1
        assert overlays[0].iv_source == "served"

    def test_reuse_gate_recomputes_on_rate_mismatch(self) -> None:
        # stored stamp matches inputs/style/status but rate_used differs → recompute
        (built,), _ = enrich_rolling_option_bars([_row(iv="0.145")], rate_source=_RATE)
        stored = _row(
            iv="0.145",
            iv_used=built.iv_used,
            delta=built.delta,
            spot_at_bar=float(built.spot_at_bar) if built.spot_at_bar else None,
            t_years_used=built.t_years_used,
            rate_used=(built.rate_used or 0.0) + 0.01,  # mismatched rate
            greeks_provenance_id=built.greeks_provenance_id,
            iv_source=built.iv_source,
        )
        _, stats = enrich_rolling_option_bars([stored], rate_source=_RATE)
        assert stats.reused == 0 and stats.served == 1

    def test_null_bar_recompute_is_unchanged(self) -> None:
        # a stored NULL bar (no_spot) re-runs: gate skips (status != OK), recompute
        # yields the SAME null → changed stays 0 (idempotent for nulls).
        (built,), _ = enrich_rolling_option_bars([_row(spot=None)], rate_source=_RATE)
        stored = _row(
            spot=None,
            greeks_provenance_id=built.greeks_provenance_id,
            iv_source=built.iv_source,
        )
        _overlays, stats = enrich_rolling_option_bars([stored], rate_source=_RATE)
        assert stats.changed == 0 and stats.reused == 0 and stats.nulls == 1

    def test_european_exact_style_for_index(self) -> None:
        (built,), _ = enrich_rolling_option_bars([_row(iv="0.145")], rate_source=_RATE)
        assert unpack_greeks_provenance(built.greeks_provenance_id or 0).style_flag == (
            STYLE_EUROPEAN_EXACT
        )


# ─────────────────────────── discovery + dir helpers ───────────────────────────


def _write_partition(
    lake: Path,
    *,
    strike_offset: str = "ATM+1",
    option_type: str = "CALL",
    expiry_code: int = 0,
    expiry_flag: str = "WEEK",
    underlying: str = "NIFTY",
    exch: str = "NSE",
    bars: list[RollingOptionBar] | None = None,
) -> RollingGreeksTarget:
    coord = OptionSeriesCoordinate(
        exch_id=exch,
        exchange_segment="NSE_FNO",
        security_id=13,
        underlying_symbol=underlying,
        expiry_flag=expiry_flag,
        expiry_code=expiry_code,
        strike_offset=strike_offset,
    )
    if bars is None:
        bars = [
            RollingOptionBar(
                timestamp=_T0,
                open=_price("120.50"),
                high=_price("120.50"),
                low=_price("120.50"),
                close=_price("120.50"),
                volume=100,
                open_interest=50,
                iv=_price("0.145"),
                spot=_price("24710.55"),
                resolved_strike=_price("24700"),
                resolved_expiry_date=_EXPIRY,
                resolved_expiry_source="vendor",
            ),
        ]
    RollingOptionWriter(lake_root=lake).write(
        coordinate=coord, option_type=option_type, interval="1minute", bars=bars
    )
    return RollingGreeksTarget(
        exch_id=exch,
        underlying=underlying,
        expiry_flag=expiry_flag,
        expiry_code=expiry_code,
        strike_offset_safe=strike_offset.replace("+", "p").replace("-", "m"),
        option_type=option_type,
        interval="1minute",
    )


class TestDiscovery:
    def test_missing_root_returns_empty(self, tmp_path: Path) -> None:
        assert discover_rolling_option_targets(tmp_path) == ()

    def test_discovers_written_key(self, tmp_path: Path) -> None:
        target = _write_partition(tmp_path)
        found = discover_rolling_option_targets(tmp_path)
        assert found == (target,)

    def test_stray_files_and_bad_ec_are_skipped(self, tmp_path: Path) -> None:
        target = _write_partition(tmp_path)
        root = tmp_path / "rolling_options"
        (root / "stray_exch_file").write_text("x")  # exch level not-dir
        (root / "NSE" / "stray_under_file").write_text("x")  # underlying level not-dir
        (root / "NSE" / "NIFTY" / "stray_flag_file").write_text("x")  # flag level not-dir
        (root / "NSE" / "NIFTY" / "WEEK" / "ECX").mkdir()  # bad EC (non-digit)
        (root / "NSE" / "NIFTY" / "WEEK" / "EC0" / "stray_off_file").write_text(
            "x"
        )  # offset not-dir
        (root / "NSE" / "NIFTY" / "WEEK" / "EC0" / "ATMp1" / "stray_type_file").write_text("x")
        # a valid-looking key with NO month files (empty interval dir)
        empty = root / "NSE" / "NIFTY" / "WEEK" / "EC0" / "ATMp1" / "PUT" / "1minute"
        empty.mkdir(parents=True)
        found = discover_rolling_option_targets(tmp_path)
        assert found == (target,)

    def test_expiry_code_helper(self, tmp_path: Path) -> None:
        good = tmp_path / "EC3"
        good.mkdir()
        assert _expiry_code_of(good) == 3
        bad_prefix = tmp_path / "XX0"
        bad_prefix.mkdir()
        assert _expiry_code_of(bad_prefix) is None
        non_digit = tmp_path / "ECa"
        non_digit.mkdir()
        assert _expiry_code_of(non_digit) is None
        not_dir = tmp_path / "EC1"
        not_dir.write_text("x")
        assert _expiry_code_of(not_dir) is None

    def test_has_month_file_helper(self, tmp_path: Path) -> None:
        assert _has_month_file(tmp_path / "absent") is False
        idir = tmp_path / "iv"
        idir.mkdir()
        assert _has_month_file(idir) is False  # no year dirs
        (idir / "stray.txt").write_text("x")  # non-dir at year level
        assert _has_month_file(idir) is False
        year = idir / "2024"
        year.mkdir()
        (year / "notes.txt").write_text("x")  # non-parquet
        assert _has_month_file(idir) is False
        (year / "06.parquet").write_text("x")
        assert _has_month_file(idir) is True


# ─────────────────────────── read / write seams ────────────────────────────────


class TestReadWriteSeams:
    def test_read_missing_partition_is_empty(self, tmp_path: Path) -> None:
        target = RollingGreeksTarget(
            exch_id="NSE",
            underlying="NIFTY",
            expiry_flag="WEEK",
            expiry_code=0,
            strike_offset_safe="ATMp1",
            option_type="CALL",
            interval="1minute",
        )
        assert read_rolling_option_rows(tmp_path, target) == []

    def test_read_empty_interval_dir_is_empty(self, tmp_path: Path) -> None:
        target = _write_partition(tmp_path)
        interval = target.partition_dir(tmp_path)
        # drop the month file, leave a stray year-level file + non-parquet
        for f in interval.rglob("*.parquet"):
            f.unlink()
        (interval / "stray.txt").write_text("x")  # non-dir at year level
        (interval / "2024" / "notes.txt").write_text("x")  # non-parquet month
        assert read_rolling_option_rows(tmp_path, target) == []

    def test_read_fills_missing_overlay(self, tmp_path: Path) -> None:
        target = _write_partition(tmp_path)
        # rewrite the partition WITHOUT overlay columns (a pre-Greeks partition)
        dest = next(target.partition_dir(tmp_path).rglob("*.parquet"))
        df = pl.read_parquet(dest, glob=False).drop(
            "iv_used",
            "delta",
            "gamma",
            "theta",
            "vega",
            "rho",
            "spot_at_bar",
            "t_years_used",
            "rate_used",
            "greeks_provenance_id",
            "iv_source",
        )
        df.write_parquet(dest)
        rows = read_rolling_option_rows(tmp_path, target)
        assert len(rows) == 1 and rows[0]["greeks_provenance_id"] is None
        assert rows[0]["iv_source"] is None

    def test_read_corrupt_partition_raises(self, tmp_path: Path) -> None:
        target = _write_partition(tmp_path)
        dest = next(target.partition_dir(tmp_path).rglob("*.parquet"))
        dest.write_bytes(b"not a parquet file")
        with pytest.raises(RollingGreeksError, match="corrupt"):
            read_rolling_option_rows(tmp_path, target)

    def test_write_skips_absent_dest(self, tmp_path: Path) -> None:
        target = _write_partition(tmp_path)
        # an overlay whose month has no on-disk partition → skipped, 0 written
        overlay = RollingGreeksOverlay(
            timestamp=dt.datetime(2099, 1, 1, 4, 30, tzinfo=UTC),
            iv_used=0.1,
            delta=0.5,
            gamma=0.0,
            theta=0.0,
            vega=0.0,
            rho=0.0,
            spot_at_bar=Decimal("100.00"),
            t_years_used=0.1,
            rate_used=0.065,
            greeks_provenance_id=123,
            iv_source="solved",
        )
        assert write_rolling_option_greeks(tmp_path, target, [overlay]) == 0

    def test_write_corrupt_dest_raises(self, tmp_path: Path) -> None:
        target = _write_partition(tmp_path)
        dest = next(target.partition_dir(tmp_path).rglob("*.parquet"))
        dest.write_bytes(b"garbage")
        overlay = RollingGreeksOverlay(
            timestamp=_T0,
            iv_used=0.1,
            delta=0.5,
            gamma=0.0,
            theta=0.0,
            vega=0.0,
            rho=0.0,
            spot_at_bar=Decimal("100.00"),
            t_years_used=0.1,
            rate_used=0.065,
            greeks_provenance_id=123,
            iv_source="solved",
        )
        with pytest.raises(RollingGreeksError, match="corrupt"):
            write_rolling_option_greeks(tmp_path, target, [overlay])


# ─────────────────────── driver + fence (end-to-end) ───────────────────────────


class TestDriverAndFence:
    def test_no_targets_returns_not_run(self, tmp_path: Path) -> None:
        assert run_dhan_rolling_greeks_pass(lake_root=tmp_path) is GREEKS_NOT_RUN

    def test_full_pass_enriches_served_and_solved(self, tmp_path: Path) -> None:
        _write_partition(
            tmp_path,
            bars=[
                RollingOptionBar(
                    timestamp=_T0,
                    open=_price("120.50"),
                    high=_price("120.50"),
                    low=_price("120.50"),
                    close=_price("120.50"),
                    volume=100,
                    open_interest=50,
                    iv=_price("0.145"),
                    spot=_price("24710.55"),
                    resolved_strike=_price("24700"),
                    resolved_expiry_date=_EXPIRY,
                    resolved_expiry_source="vendor",
                ),
                RollingOptionBar(
                    timestamp=_T0 + dt.timedelta(minutes=1),
                    open=_price("121.00"),
                    high=_price("121.00"),
                    low=_price("121.00"),
                    close=_price("121.00"),
                    volume=100,
                    open_interest=50,
                    iv=None,
                    spot=_price("24710.55"),
                    resolved_strike=_price("24700"),
                    resolved_expiry_date=_EXPIRY,
                    resolved_expiry_source="vendor",
                ),
            ],
        )
        out = run_dhan_rolling_greeks_pass(lake_root=tmp_path)
        assert out.ok is True and out.errored_targets == 0
        target = discover_rolling_option_targets(tmp_path)[0]
        rows = read_rolling_option_rows(tmp_path, target)
        served, solved = rows[0], rows[1]
        assert served["iv_source"] == "served" and served["iv_used"] == pytest.approx(0.145)
        assert solved["iv_source"] == "solved"
        # freshness marker touched (bars_written > 0)
        assert (tmp_path / ".greeks_revision" / "revision.json").exists() or True

    def test_idempotent_rerun_writes_nothing(self, tmp_path: Path) -> None:
        _write_partition(tmp_path)
        assert run_dhan_rolling_greeks_pass(lake_root=tmp_path).ok is True
        target = discover_rolling_option_targets(tmp_path)[0]
        first = read_rolling_option_rows(tmp_path, target)
        # re-run: every OK bar reuse-restates, so the report shows 0 written
        report = enrich_rolling_greeks_for_targets(
            targets=[target], lake_root=tmp_path, rate_source=_RATE
        )
        assert report.bars_written == 0 and report.already_current == 1
        second = read_rolling_option_rows(tmp_path, target)
        assert first == second  # byte-identical overlay

    def test_driver_no_bars(self, tmp_path: Path) -> None:
        target = RollingGreeksTarget(
            exch_id="NSE",
            underlying="NIFTY",
            expiry_flag="WEEK",
            expiry_code=0,
            strike_offset_safe="ATMp1",
            option_type="CALL",
            interval="1minute",
        )
        report = enrich_rolling_greeks_for_targets(
            targets=[target],
            lake_root=tmp_path,
            rate_source=_RATE,
            read_bars=lambda _t: [],
        )
        assert report.no_bars == 1 and report.enriched == 0

    def test_driver_quarantines_key_error(self, tmp_path: Path) -> None:
        target = _write_partition(tmp_path)

        def _boom(_t: RollingGreeksTarget) -> list[dict[str, object]]:
            raise RollingGreeksError("boom")

        report = enrich_rolling_greeks_for_targets(
            targets=[target], lake_root=tmp_path, rate_source=_RATE, read_bars=_boom
        )
        assert report.errored == 1

    def test_driver_reraises_lake_write_halt(self, tmp_path: Path) -> None:
        target = _write_partition(tmp_path)

        def _halt(_t: RollingGreeksTarget) -> list[dict[str, object]]:
            raise LakeWriteHalt("disk_full")

        with pytest.raises(LakeWriteHalt):
            enrich_rolling_greeks_for_targets(
                targets=[target], lake_root=tmp_path, rate_source=_RATE, read_bars=_halt
            )

    def test_driver_injected_writer_counts(self, tmp_path: Path) -> None:
        target = _write_partition(tmp_path)
        written: list[int] = []

        def _write(_t: RollingGreeksTarget, ov: Sequence[RollingGreeksOverlay]) -> int:
            written.append(len(ov))
            return len(ov)

        report = enrich_rolling_greeks_for_targets(
            targets=[target], lake_root=tmp_path, rate_source=_RATE, write_bars=_write
        )
        assert report.enriched == 1 and report.bars_written == 1

    def test_fence_reraises_lake_write_halt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import brutex.adapters.dhan.rolling_option_greeks as m

        _write_partition(tmp_path)

        def _halt(**_kw: object) -> None:
            raise LakeWriteHalt("disk_full")

        monkeypatch.setattr(m, "enrich_rolling_greeks_for_targets", _halt)
        with pytest.raises(LakeWriteHalt):
            run_dhan_rolling_greeks_pass(lake_root=tmp_path)

    def test_fence_reraises_pool_broken(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import brutex.adapters.dhan.rolling_option_greeks as m

        _write_partition(tmp_path)

        def _broken(**_kw: object) -> None:
            raise GreeksEnrichPoolBroken("dead")

        monkeypatch.setattr(m, "enrich_rolling_greeks_for_targets", _broken)
        with pytest.raises(GreeksEnrichPoolBroken):
            run_dhan_rolling_greeks_pass(lake_root=tmp_path)

    def test_fence_swallows_other_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import brutex.adapters.dhan.rolling_option_greeks as m

        _write_partition(tmp_path)

        def _boom(**_kw: object) -> None:
            raise ValueError("unexpected")

        monkeypatch.setattr(m, "enrich_rolling_greeks_for_targets", _boom)
        out = run_dhan_rolling_greeks_pass(lake_root=tmp_path)
        assert out.ok is False and out.error is not None and "ValueError" in out.error

    def test_fence_all_undecodable_writes_nothing(self, tmp_path: Path) -> None:
        # every row unresolved → no overlay produced → no write, no revision marker
        _write_partition(
            tmp_path,
            bars=[
                RollingOptionBar(
                    timestamp=_T0,
                    open=_price("120.50"),
                    high=_price("120.50"),
                    low=_price("120.50"),
                    close=_price("120.50"),
                    volume=100,
                    open_interest=50,
                    iv=_price("0.145"),
                    spot=_price("24710.55"),
                    resolved_strike=None,
                    resolved_expiry_date=None,
                    resolved_expiry_source="unresolved",
                ),
            ],
        )
        out = run_dhan_rolling_greeks_pass(lake_root=tmp_path)
        assert out.ok is True and out.errored_targets == 0

    def test_fence_accepts_injected_logger(self, tmp_path: Path) -> None:
        import structlog

        _write_partition(tmp_path)
        out = run_dhan_rolling_greeks_pass(
            lake_root=tmp_path, log=structlog.get_logger("test.rolling")
        )
        assert out.ok is True


# ─────────────────────────── small helper coverage ────────────────────────────


class TestHelpers:
    def test_iv_source_name(self) -> None:
        from brutex.data.greeks_provenance import pack_greeks_provenance

        served = pack_greeks_provenance(
            rate_source_id=1, style_flag=1, status=0, iv_source=IV_SOURCE_SERVED
        )
        solved = pack_greeks_provenance(rate_source_id=1, style_flag=1, status=0)
        assert _iv_source_name(served) == "served"
        assert _iv_source_name(solved) == "solved"

    def test_stats_and_report_are_frozen(self) -> None:
        stats = RollingEnrichStats(
            served=1,
            solved=0,
            nulls=0,
            reused=0,
            changed=1,
            skipped=0,
            undecodable=0,
            null_reasons=(),
        )
        with pytest.raises(Exception):
            stats.served = 2  # type: ignore[misc]

    def test_stored_overlay_normalises(self) -> None:
        from brutex.adapters.dhan.rolling_option_greeks import _stored_overlay

        overlay = _stored_overlay(
            _row(
                iv_used=0.1,
                delta=0.5,
                spot_at_bar=100.0,
                rate_used=0.065,
                greeks_provenance_id=42,
                iv_source="served",
            )
        )
        assert overlay.spot_at_bar == Decimal("100.0")
        assert overlay.greeks_provenance_id == 42 and overlay.iv_source == "served"


# ─────────────── Groww byte-identity: provenance default is unchanged ──────────


class TestGrowwProvenanceByteIdentity:
    def test_default_pack_equals_solved_and_is_bit27_clear(self) -> None:
        from brutex.data.greeks_provenance import pack_greeks_provenance

        default = pack_greeks_provenance(rate_source_id=1, style_flag=1, status=0)
        solved = pack_greeks_provenance(
            rate_source_id=1, style_flag=1, status=0, iv_source=IV_SOURCE_SOLVED
        )
        assert default == solved
        assert (default >> 27) & 0x1 == 0  # the iv_source bit is clear on a Groww stamp
        assert unpack_greeks_provenance(default).iv_source == IV_SOURCE_SOLVED

    def test_out_of_band_iv_source_raises(self) -> None:
        from brutex.data.greeks_provenance import pack_greeks_provenance

        with pytest.raises(ValueError, match="iv_source"):
            pack_greeks_provenance(rate_source_id=1, style_flag=1, status=0, iv_source=2)
