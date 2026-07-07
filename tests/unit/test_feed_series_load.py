"""Unit tests for :mod:`brutex.feed.series_load` — the ONE alias-aware slice loader.

DEC-NIFTY-FUTURES-SWEEP-LAUNCH-001: pins the spot passthrough (byte-identical
to the inline reconcile + ``ReplayFeed`` block it replaced), the exact IST-date
ownership cut (expiry-day bars stay on the EXPIRING contract; a pre-roll
overlap bar in the successor's partition is cut; the midnight-IST derived
``1day`` bar is owned by its session date across the UTC D−1 file drift — the
C-fatal regression), the per-``(contract, interval)`` zero-bar refusal in both
reconcile modes, the boundary monotonicity raise, the F7 poison-dir
never-read pin, the F12 snapshot-sensitivity pin (one changed bar → a new
run identity preimage), and the loader-cut/cursor per-bar agreement pin
(the loader's date-cut and ``Stage1AMatrixBuilder``'s roll cursor derive the
SAME owner for every bar — both from the ONE ``roll_schedule``).

This file is the module's Layer-B/Layer-C covering set — it alone must hold
``brutex/feed/series_load.py`` at 100% line + 100% branch.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.calendars.nse import IST
from brutex.data.catalog import Catalog
from brutex.data.catalog_reconcile import ReconcileResult, reconcile_catalog_from_lake
from brutex.data.futures_series import (
    FuturesSeriesError,
    FuturesSeriesResolution,
    FuturesSeriesSegment,
    resolve_futures_series,
)
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink
from brutex.feed import series_load
from brutex.feed.replay import ReplayFeed
from brutex.feed.series_load import load_sweep_slice
from structlog.testing import capture_logs

pytest.importorskip("polars")
pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")


# ────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────

#: The pinned series alias (import-identity asserted in test_futures_series /
#: test_universe; spelled literally here to keep the fixture self-describing).
_ALIAS = "NSE-NIFTY-FUT"

#: Real vendor-shaped 2025 contract dirs (calendar-verified expiries).
_JUN = "NSE-NIFTY-26Jun25-FUT"
_JUL = "NSE-NIFTY-31Jul25-FUT"
_AUG = "NSE-NIFTY-28Aug25-FUT"


def _ist(y: int, mo: int, d: int, h: int = 9, mi: int = 15) -> datetime:
    """A tz-aware IST instant (stored UTC by the lake writers)."""
    return datetime(y, mo, d, h, mi, tzinfo=IST)


def _bar(ts: datetime, close: float, *, volume: int = 10) -> CandleBar:
    """One volume-bearing bar with a distinctive close (UTC-normalised)."""
    px = Decimal(str(close))
    return CandleBar(
        timestamp=ts.astimezone(UTC),
        open=px - Decimal("1"),
        high=px + Decimal("1"),
        low=px - Decimal("2"),
        close=px,
        volume=volume,
        open_interest=100,
    )


def _seed(
    lake_root: Path,
    cat: Catalog,
    *,
    symbol: str,
    bars: list[CandleBar],
    interval: str = "1minute",
    segment: str = "FNO",
    exchange: str = "NSE",
) -> None:
    """Persist *bars* through the REAL production writers (sink + catalog)."""
    sink = ParquetBarSink(writer=ParquetWriter(lake_root=lake_root), catalog=cat)
    sink.append(exchange=exchange, segment=segment, symbol=symbol, interval=interval, bars=bars)


@pytest.fixture
def lake(tmp_path: Path) -> Path:
    """An empty lake root."""
    return tmp_path / "lake"


def _futures_lake(lake: Path, cat: Catalog) -> dict[str, list[CandleBar]]:
    """Seed a 2-contract June→July lake with a roll at 2025-06-26 (expiry day).

    The July dir deliberately carries a PRE-ROLL overlap bar dated on the
    June contract's expiry day (the 60-day pull tail overlaps ~30 days), and
    the June dir carries a post-expiry stray — BOTH must be cut.
    """
    jun_bars = [
        _bar(_ist(2025, 6, 25), 100.0),
        _bar(_ist(2025, 6, 26, 15, 29), 101.0),  # expiry-day last usable bar
        _bar(_ist(2025, 6, 27), 666.0),  # stray PAST its own expiry — cut
    ]
    jul_bars = [
        _bar(_ist(2025, 6, 26, 15, 29), 555.0),  # pre-roll overlap — cut
        _bar(_ist(2025, 6, 27, 9, 15), 200.0),  # first post-roll session
        _bar(_ist(2025, 7, 1, 0, 30), 201.0),  # IST date Jul-1 = UTC Jun-30
        _bar(_ist(2025, 7, 1, 19, 15), 202.0),  # muhurat-style evening bar
    ]
    _seed(lake, cat, symbol=_JUN, bars=jun_bars)
    _seed(lake, cat, symbol=_JUL, bars=jul_bars)
    return {"jun": jun_bars, "jul": jul_bars}


def _load_alias(
    cat: Catalog,
    lake: Path,
    *,
    interval: str = "1minute",
    start: datetime | None = None,
    end: datetime | None = None,
    **kwargs: object,
) -> list[CandleBar]:
    """`load_sweep_slice` over the alias with the fixture's default window."""
    return load_sweep_slice(
        cat=cat,
        lake_root=lake,
        exchange="NSE",
        segment="FNO",
        symbol=_ALIAS,
        interval=interval,
        start=start if start is not None else _ist(2025, 6, 25, 0, 0),
        end=end if end is not None else _ist(2025, 7, 2, 23, 59),
        **kwargs,  # type: ignore[arg-type]
    )


# ────────────────────────────────────────────────────────────────────────
# Spot / verbatim-dir passthrough (the moved inline block)
# ────────────────────────────────────────────────────────────────────────


class TestSpotPassthrough:
    """A non-alias symbol takes EXACTLY the old reconcile + ReplayFeed block.

    The spot window datetimes are UTC-normalised exactly as every real sweep
    mouth passes them (the moved block hands them to ``ReplayFeed`` verbatim —
    the catalog's polars window filter is dtype-strict, so an IST-tz literal
    would raise; only the ALIAS path normalises internally).
    """

    def _spot_bars(self) -> list[CandleBar]:
        return [_bar(_ist(2025, 6, 25, 9, 15 + i), 100.0 + i, volume=0) for i in range(3)]

    def test_reconcile_true_parity_with_direct_replay_feed(
        self, lake: Path, tmp_path: Path
    ) -> None:
        with Catalog.open(tmp_path / "seed.duckdb") as seed_cat:
            _seed(lake, seed_cat, symbol="NIFTY", bars=self._spot_bars(), segment="INDEX")
        start = _ist(2025, 6, 25, 0, 0).astimezone(UTC)
        end = _ist(2025, 6, 25, 23, 59).astimezone(UTC)
        # The loader on a FRESH catalog (reconcile=True populates it) …
        with Catalog.open(tmp_path / "loader.duckdb") as cat:
            via_loader = load_sweep_slice(
                cat=cat,
                lake_root=lake,
                exchange="NSE",
                segment="INDEX",
                symbol="NIFTY",
                interval="1minute",
                start=start,
                end=end,
            )
        # … equals the old inline block spelled out by hand.
        with Catalog.open(tmp_path / "direct.duckdb") as cat:
            reconcile_catalog_from_lake(
                lake_root=lake,
                catalog=cat,
                exchange="NSE",
                segment="INDEX",
                symbol="NIFTY",
                interval="1minute",
            )
            feed = ReplayFeed(
                catalog=cat,
                lake_root=lake,
                exchange="NSE",
                segment="INDEX",
                symbols=["NIFTY"],
                interval="1minute",
                start=start,
                end=end,
            )
            direct = [event.bar for event in feed]
        assert via_loader == direct
        assert len(via_loader) == 3

    def test_reconcile_false_reads_the_catalog_as_is(self, lake: Path, tmp_path: Path) -> None:
        with Catalog.open(tmp_path / "cat.duckdb") as cat:
            _seed(lake, cat, symbol="NIFTY", bars=self._spot_bars(), segment="INDEX")
            hooked: list[ReconcileResult] = []
            out = load_sweep_slice(
                cat=cat,
                lake_root=lake,
                exchange="NSE",
                segment="INDEX",
                symbol="NIFTY",
                interval="1minute",
                start=_ist(2025, 6, 25, 0, 0).astimezone(UTC),
                end=_ist(2025, 6, 25, 23, 59).astimezone(UTC),
                reconcile=False,
                on_reconcile=hooked.append,
            )
        assert [b.close for b in out] == [Decimal("100"), Decimal("101"), Decimal("102")]
        assert hooked == []  # the hook only fires when reconcile runs

    def test_spot_empty_slice_returns_empty_never_raises(self, lake: Path, tmp_path: Path) -> None:
        # The caller's preflight owns the spot zero-bar refusal — the loader
        # hands back an honest empty list (byte-identical to the old block).
        lake.mkdir(parents=True)
        with Catalog.open(tmp_path / "cat.duckdb") as cat:
            out = load_sweep_slice(
                cat=cat,
                lake_root=lake,
                exchange="NSE",
                segment="INDEX",
                symbol="NIFTY",
                interval="1minute",
                start=_ist(2025, 6, 25, 0, 0).astimezone(UTC),
                end=_ist(2025, 6, 25, 23, 59).astimezone(UTC),
            )
        assert out == []

    def test_on_reconcile_hook_fires_once_for_spot(self, lake: Path, tmp_path: Path) -> None:
        with Catalog.open(tmp_path / "cat.duckdb") as cat:
            _seed(lake, cat, symbol="NIFTY", bars=self._spot_bars(), segment="INDEX")
            hooked: list[ReconcileResult] = []
            load_sweep_slice(
                cat=cat,
                lake_root=lake,
                exchange="NSE",
                segment="INDEX",
                symbol="NIFTY",
                interval="1minute",
                start=_ist(2025, 6, 25, 0, 0).astimezone(UTC),
                end=_ist(2025, 6, 25, 23, 59).astimezone(UTC),
                on_reconcile=hooked.append,
            )
        assert len(hooked) == 1
        assert isinstance(hooked[0], ReconcileResult)


# ────────────────────────────────────────────────────────────────────────
# The futures stitch — IST-date ownership cut
# ────────────────────────────────────────────────────────────────────────


class TestFuturesStitch:
    """The alias resolves + stitches with the exact IST-date ownership cut."""

    def test_expiry_day_owned_by_the_expiring_contract(self, lake: Path, tmp_path: Path) -> None:
        with Catalog.open(tmp_path / "cat.duckdb") as cat:
            _futures_lake(lake, cat)
            out = _load_alias(cat, lake)
        closes = [b.close for b in out]
        # Expiry-day 15:29 stays on June (101); the July overlap bar dated on
        # expiry day (555) and June's post-expiry stray (666) are BOTH cut;
        # expiry+1d 09:15 is July's (200). Muhurat-evening (202) and the
        # early-IST-morning bar whose UTC date is the PRIOR day (201) are
        # owned by their own IST dates.
        assert closes == [
            Decimal("100"),
            Decimal("101"),
            Decimal("200"),
            Decimal("201"),
            Decimal("202"),
        ]
        timestamps = [b.timestamp for b in out]
        assert timestamps == sorted(timestamps)
        assert len(set(timestamps)) == len(timestamps)

    def test_midnight_ist_1day_bar_owned_across_the_utc_month_drift(
        self, lake: Path, tmp_path: Path
    ) -> None:
        # The C-fatal regression pin: the derived FNO 1day bar for session
        # date D is stamped D 00:00 IST = (D−1) 18:30 UTC, so the Aug-1 bar
        # physically lives in July's UTC month file of the AUGUST contract —
        # the IST-date cut (never a 09:15 clamp) still owns it correctly.
        with Catalog.open(tmp_path / "cat.duckdb") as cat:
            _seed(
                lake, cat, symbol=_JUL, interval="1day", bars=[_bar(_ist(2025, 7, 31, 0, 0), 100.0)]
            )
            _seed(
                lake, cat, symbol=_AUG, interval="1day", bars=[_bar(_ist(2025, 8, 1, 0, 0), 200.0)]
            )
            aug_file = lake / "bars" / "NSE" / "FNO" / _AUG / "1day" / "2025" / "07.parquet"
            assert aug_file.is_file()  # the D−1 drift is REAL in this fixture
            assert not (aug_file.parent / "08.parquet").exists()
            out = _load_alias(
                cat,
                lake,
                interval="1day",
                start=_ist(2025, 7, 31, 0, 0),
                end=_ist(2025, 8, 5, 23, 59),
            )
        assert [b.close for b in out] == [Decimal("100"), Decimal("200")]
        assert out[1].timestamp.astimezone(IST).date() == date(2025, 8, 1)

    def test_precomputed_resolution_skips_the_resolver(
        self, lake: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # sweep-all resolves ONCE and passes the resolution down per interval —
        # the loader must NOT re-resolve.
        with Catalog.open(tmp_path / "cat.duckdb") as cat:
            _futures_lake(lake, cat)
            res = resolve_futures_series(lake, start=date(2025, 6, 25), end=date(2025, 7, 2))

            def _boom(*args: object, **kwargs: object) -> FuturesSeriesResolution:
                raise AssertionError("resolver must not be called when resolution= is given")

            monkeypatch.setattr(series_load, "resolve_futures_series", _boom)
            out = _load_alias(cat, lake, resolution=res)
        assert len(out) == 5

    def test_internal_resolve_uses_ist_calendar_dates(
        self, lake: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # start 2025-06-24 20:30 UTC is ALREADY 2025-06-25 02:00 IST — the
        # loader must hand the resolver IST dates, never UTC dates.
        captured: dict[str, date] = {}
        real = resolve_futures_series

        def _capture(lake_root: Path, *, start: date, end: date) -> FuturesSeriesResolution:
            captured["start"], captured["end"] = start, end
            return real(lake_root, start=start, end=end)

        monkeypatch.setattr(series_load, "resolve_futures_series", _capture)
        with Catalog.open(tmp_path / "cat.duckdb") as cat:
            _futures_lake(lake, cat)
            _load_alias(
                cat,
                lake,
                start=datetime(2025, 6, 24, 20, 30, tzinfo=UTC),
                end=datetime(2025, 7, 2, 10, 0, tzinfo=UTC),
            )
        assert captured["start"] == date(2025, 6, 25)
        assert captured["end"] == date(2025, 7, 2)

    def test_on_reconcile_hook_fires_once_per_contract(self, lake: Path, tmp_path: Path) -> None:
        with Catalog.open(tmp_path / "cat.duckdb") as cat:
            _futures_lake(lake, cat)
            hooked: list[ReconcileResult] = []
            _load_alias(cat, lake, on_reconcile=hooked.append)
        assert len(hooked) == 2  # one reconcile per contract dir

    def test_reconcile_false_reads_a_prepopulated_catalog(self, lake: Path, tmp_path: Path) -> None:
        with Catalog.open(tmp_path / "cat.duckdb") as cat:
            _futures_lake(lake, cat)  # the sink already upserted catalog rows
            out = _load_alias(cat, lake, reconcile=False)
        assert len(out) == 5

    def test_poison_alias_dir_is_never_read(self, lake: Path, tmp_path: Path) -> None:
        # F7: a hand-created literal NSE-NIFTY-FUT dir holding a GARBAGE
        # parquet — if the loader ever statted/read the alias as a path the
        # read would blow up; instead the launch succeeds on the real dirs.
        with Catalog.open(tmp_path / "cat.duckdb") as cat:
            _futures_lake(lake, cat)
            poison = lake / "bars" / "NSE" / "FNO" / _ALIAS / "1minute" / "2025" / "06.parquet"
            poison.parent.mkdir(parents=True)
            poison.write_bytes(b"this is not a parquet file")
            out = _load_alias(cat, lake)
        assert [b.close for b in out] == [
            Decimal("100"),
            Decimal("101"),
            Decimal("200"),
            Decimal("201"),
            Decimal("202"),
        ]


# ────────────────────────────────────────────────────────────────────────
# Loud refusals (zero-bar owned window, monotonicity)
# ────────────────────────────────────────────────────────────────────────


class TestFuturesRefusals:
    """Alias-path failures refuse loudly — never a silent empty slice."""

    def test_zero_bar_owned_window_refused_naming_contract_and_interval(
        self, lake: Path, tmp_path: Path
    ) -> None:
        # The contract DIRS exist (the resolver selects them) but the
        # requested interval was never derived — distinct message from a
        # missing contract (F4 vs F1).
        with Catalog.open(tmp_path / "cat.duckdb") as cat:
            _futures_lake(lake, cat)
            with pytest.raises(FuturesSeriesError, match="EMPTY") as exc:
                _load_alias(cat, lake, interval="5minute")
        message = str(exc.value)
        assert f"{_JUN!r} / '5minute'" in message
        assert "[2025-06-25 → 2025-06-26]" in message  # the owned window
        assert "pipeline prebuild" in message
        assert "sweep-all" not in message  # the reconcile=True variant has no hint

    def test_zero_bar_refusal_under_no_reconcile_names_the_reconcile_hint(
        self, lake: Path, tmp_path: Path
    ) -> None:
        with Catalog.open(tmp_path / "fresh.duckdb") as cat:
            # Fresh catalog + reconcile=False: the partitions exist on disk
            # but the catalog has never seen them → zero bars + the hint.
            _futures_lake_no_catalog(lake)
            with pytest.raises(FuturesSeriesError, match="brutex sweep-all") as exc:
                _load_alias(cat, lake, reconcile=False)
        assert "reads without a catalog reconcile" in str(exc.value)

    def test_boundary_monotonicity_break_refused(self, lake: Path, tmp_path: Path) -> None:
        # Structurally impossible under disjoint IST-date ownership — a
        # crafted OVERLAPPING resolution proves the invariant raise guards
        # the impossible instead of silently interleaving.
        with Catalog.open(tmp_path / "cat.duckdb") as cat:
            _futures_lake(lake, cat)
            crafted = FuturesSeriesResolution(
                segments=(
                    FuturesSeriesSegment(
                        _JUN, date(2025, 6, 26), date(2025, 6, 25), date(2025, 6, 26)
                    ),
                    # Overlaps the June window → July's overlap bar (15:29 on
                    # the 26th) is not after June's last bar.
                    FuturesSeriesSegment(
                        _JUL, date(2025, 7, 31), date(2025, 6, 26), date(2025, 7, 2)
                    ),
                ),
                roll_schedule=(date(2025, 6, 26).toordinal(), date(2025, 7, 31).toordinal()),
                contract_symbols=(_JUN, _JUL),
                unparsed_dirs=(),
                cross_checks=(),
                head_clamped=False,
                tail_unowned_days=0,
            )
            with pytest.raises(FuturesSeriesError, match="boundary monotonicity broken") as exc:
                _load_alias(cat, lake, resolution=crafted)
        assert "disjoint IST-date ownership" in str(exc.value)

    def test_segment_wholly_outside_the_window_skips_with_one_warn(
        self, lake: Path, tmp_path: Path
    ) -> None:
        # A degenerate window∩ownership intersection cannot own bars — it
        # holds NO session instant, so it is SKIPPED with one WARN (review
        # round-1: the old unconditional zero-bar refusal false-blocked
        # legitimate boundary windows), never a silent skip and never a
        # false "re-pull" refusal.
        with Catalog.open(tmp_path / "cat.duckdb") as cat:
            _futures_lake(lake, cat)
            crafted = FuturesSeriesResolution(
                segments=(
                    FuturesSeriesSegment(
                        _JUN, date(2025, 6, 26), date(2025, 9, 1), date(2025, 9, 2)
                    ),
                ),
                roll_schedule=(date(2025, 6, 26).toordinal(),),
                contract_symbols=(_JUN,),
                unparsed_dirs=(),
                cross_checks=(),
                head_clamped=False,
                tail_unowned_days=0,
            )
            with capture_logs() as logs:
                out = _load_alias(cat, lake, resolution=crafted)
        assert out == []
        warned = [rec for rec in logs if rec["event"] == "series_load.boundary_segment_no_session"]
        assert len(warned) == 1
        assert warned[0]["contract"] == _JUN


def _futures_lake_no_catalog(lake: Path) -> None:
    """Write the 2-contract lake WITHOUT touching the caller's catalog."""
    with Catalog.open(lake.parent / "throwaway.duckdb") as seed_cat:
        _futures_lake(lake, seed_cat)


# ────────────────────────────────────────────────────────────────────────
# Boundary segments with NO expectable session instant (review round-1 2/16/22)
# ────────────────────────────────────────────────────────────────────────


def _skip_warns(logs: list[dict[str, object]]) -> list[dict[str, object]]:
    return [rec for rec in logs if rec["event"] == "series_load.boundary_segment_no_session"]


class TestBoundarySegmentSessionSkip:
    """A bar-less boundary sliver SKIPS with one WARN; a real hole still refuses.

    Review round-1 findings 2/16/22: the resolver selects segments by IST
    DATE while the loader cuts by DATETIME, so a legitimate window edge (a
    day-only ``--end`` the morning after an expiry, a post-close ``--start``,
    a holiday right after a roll) intersected a fresh segment entirely
    OUTSIDE market hours → zero bars → a false whole-sweep "re-pull" refusal.
    The refusal is now gated on the intersection holding at least one
    expectable session instant (real NSE calendar).
    """

    def test_day_only_end_on_the_first_post_roll_day_skips_the_boundary_segment(
        self, lake: Path, tmp_path: Path
    ) -> None:
        # The finding-22 repro: a COMPLETE lake, `--end 2025-06-27` (day-only
        # ISO → midnight UTC = 05:30 IST) — the July segment's intersection
        # [00:00 → 05:30 IST] is entirely pre-open. Pre-fix: FuturesSeriesError
        # "EMPTY … re-pull the contract" although the lake is complete.
        with Catalog.open(tmp_path / "cat.duckdb") as cat:
            _futures_lake(lake, cat)
            with capture_logs() as logs:
                out = _load_alias(
                    cat,
                    lake,
                    start=_ist(2025, 6, 25, 0, 0),
                    end=datetime(2025, 6, 27, 0, 0, tzinfo=UTC),  # cli day-only _parse_dt shape
                )
        assert [b.close for b in out] == [Decimal("100"), Decimal("101")]  # June bars only
        warned = _skip_warns(logs)
        assert len(warned) == 1
        assert warned[0]["contract"] == _JUL
        assert warned[0]["interval"] == "1minute"

    def test_post_close_start_on_expiry_day_skips_not_refuses(
        self, lake: Path, tmp_path: Path
    ) -> None:
        # The symmetric head edge: a post-close --start on the expiry date.
        # The June segment's intersection [16:00 → 23:59 IST] starts AT/after
        # the session close → no expectable bar label → skip, empty result
        # (the caller's zero-bar preflight owns that refusal, spot-mirroring).
        with Catalog.open(tmp_path / "cat.duckdb") as cat:
            _futures_lake(lake, cat)
            with capture_logs() as logs:
                out = _load_alias(
                    cat,
                    lake,
                    start=_ist(2025, 6, 26, 16, 0),
                    end=_ist(2025, 6, 26, 23, 59),
                )
        assert out == []
        assert len(_skip_warns(logs)) == 1

    def test_holiday_after_expiry_skips_the_successor_segment(
        self, lake: Path, tmp_path: Path
    ) -> None:
        # Finding-2's second shape: window end on a holiday right after an
        # expiry (2024-03-28 March expiry; 2024-03-29 is Good Friday). The
        # April segment owns ONLY the holiday in-window → zero bars is
        # EXPECTED → skip, never a false refusal.
        mar = "NSE-NIFTY-28Mar24-FUT"
        apr = "NSE-NIFTY-25Apr24-FUT"
        with Catalog.open(tmp_path / "cat.duckdb") as cat:
            _seed(lake, cat, symbol=mar, bars=[_bar(_ist(2024, 3, 27), 100.0)])
            _seed(lake, cat, symbol=apr, bars=[_bar(_ist(2024, 4, 1), 200.0)])
            with capture_logs() as logs:
                out = _load_alias(
                    cat,
                    lake,
                    start=_ist(2024, 3, 27, 0, 0),
                    end=_ist(2024, 3, 29, 23, 59),
                )
        assert [b.close for b in out] == [Decimal("100")]
        warned = _skip_warns(logs)
        assert len(warned) == 1
        assert warned[0]["contract"] == apr

    def test_weekend_run_with_a_trading_monday_still_refuses(
        self, lake: Path, tmp_path: Path
    ) -> None:
        # A crafted Sat→Mon owned window: Sat+Sun hold no session but MONDAY
        # does — a zero-bar read there is a REAL data hole → the loud refusal
        # stands (the session walk skips non-trading days, then finds Monday).
        with Catalog.open(tmp_path / "cat.duckdb") as cat:
            _futures_lake(lake, cat)
            crafted = FuturesSeriesResolution(
                segments=(
                    FuturesSeriesSegment(
                        _JUN, date(2025, 6, 26), date(2025, 6, 28), date(2025, 6, 30)
                    ),
                ),
                roll_schedule=(date(2025, 6, 26).toordinal(),),
                contract_symbols=(_JUN,),
                unparsed_dirs=(),
                cross_checks=(),
                head_clamped=False,
                tail_unowned_days=0,
            )
            with pytest.raises(FuturesSeriesError, match="EMPTY"):
                _load_alias(
                    cat,
                    lake,
                    start=_ist(2025, 6, 28, 0, 0),
                    end=_ist(2025, 6, 30, 23, 59),
                    resolution=crafted,
                )

    def test_1day_midnight_stamp_in_window_still_refuses(self, lake: Path, tmp_path: Path) -> None:
        # The derived FNO 1day bar is stamped midnight IST: a pre-open sliver
        # [00:00 → 05:30] DOES hold the expectable 1day instant, so a missing
        # 1day partition there is a REAL prebuild gap → refusal stands.
        with Catalog.open(tmp_path / "cat.duckdb") as cat:
            _futures_lake(lake, cat)  # 1minute only — no 1day partitions
            with pytest.raises(FuturesSeriesError, match="'1day' is EMPTY"):
                _load_alias(
                    cat,
                    lake,
                    interval="1day",
                    start=_ist(2025, 6, 27, 0, 0),
                    end=_ist(2025, 6, 27, 5, 30),
                )

    def test_1day_midnight_stamp_outside_window_skips(self, lake: Path, tmp_path: Path) -> None:
        # The same trading date but an intersection strictly AFTER midnight:
        # no expectable 1day stamp in [06:00 → 07:00] → skip, empty result.
        with Catalog.open(tmp_path / "cat.duckdb") as cat:
            _futures_lake(lake, cat)
            with capture_logs() as logs:
                out = _load_alias(
                    cat,
                    lake,
                    interval="1day",
                    start=_ist(2025, 6, 27, 6, 0),
                    end=_ist(2025, 6, 27, 7, 0),
                )
        assert out == []
        assert len(_skip_warns(logs)) == 1

    def test_out_of_calendar_coverage_keeps_the_loud_refusal(
        self, lake: Path, tmp_path: Path
    ) -> None:
        # HONEST abstain: a 2027+ weekday is outside the holiday coverage —
        # the loader cannot prove the window bar-less, so the refusal stands
        # (never a silent skip the calendar cannot vouch for).
        dec27 = "NSE-NIFTY-30Dec27-FUT"
        with Catalog.open(tmp_path / "cat.duckdb") as cat:
            lake.mkdir(parents=True, exist_ok=True)
            (lake / "bars" / "NSE" / "FNO" / dec27).mkdir(parents=True)
            with pytest.raises(FuturesSeriesError, match="EMPTY"):
                _load_alias(
                    cat,
                    lake,
                    start=_ist(2027, 12, 1, 0, 0),
                    end=_ist(2027, 12, 30, 23, 59),
                )


class TestCoarseTfBucketGridGate:
    """Round-3 finding 7: the sliver gate is TF-BUCKET-GRID-aware, not overlap-only.

    Bar labels live on the resampler's fixed grid ``09:15 + k·tf`` inside
    ``[open, close)`` (DEC-RESAMPLE-ANCHOR-002). Bare session OVERLAP
    over-claimed for coarse TFs (a 4hour day carries ONLY the 09:15 + 13:15
    labels) and for a sub-minute start — a complete lake was refused with a
    wrong "re-pull" remediation on a window fragment that cannot hold a bar.
    """

    def test_coarse_tf_post_label_fragment_on_expiry_day_skips_not_refuses(
        self, lake: Path, tmp_path: Path
    ) -> None:
        # The finding's repro: expiry-day 4hour data is COMPLETE (labels
        # 09:15 + 13:15 stored) but the expiring contract's in-window slice
        # is [13:30 → midnight] — trading minutes, NO 4hour label. Pre-fix:
        # FuturesSeriesError "EMPTY … re-pull the contract" (exit 1) for
        # complete data; post-fix: one boundary WARN, empty result.
        with Catalog.open(tmp_path / "cat.duckdb") as cat:
            _futures_lake(lake, cat)
            _seed(
                lake,
                cat,
                symbol=_JUN,
                interval="4hour",
                bars=[
                    _bar(_ist(2025, 6, 26, 9, 15), 100.0),
                    _bar(_ist(2025, 6, 26, 13, 15), 101.0),
                ],
            )
            with capture_logs() as logs:
                out = _load_alias(
                    cat,
                    lake,
                    interval="4hour",
                    start=_ist(2025, 6, 26, 13, 30),
                    end=_ist(2025, 6, 26, 23, 59),
                )
        assert out == []
        warned = _skip_warns(logs)
        assert len(warned) == 1
        assert warned[0]["contract"] == _JUN
        assert warned[0]["interval"] == "4hour"

    def test_coarse_tf_fragment_holding_a_grid_label_still_refuses(
        self, lake: Path, tmp_path: Path
    ) -> None:
        # Fail-closed control: [13:00 → 23:59] DOES hold the 13:15 4hour
        # label, so a missing 4hour partition there is a REAL prebuild gap.
        with Catalog.open(tmp_path / "cat.duckdb") as cat:
            _futures_lake(lake, cat)  # 1minute only — no 4hour partitions
            with pytest.raises(FuturesSeriesError, match="'4hour' is EMPTY"):
                _load_alias(
                    cat,
                    lake,
                    interval="4hour",
                    start=_ist(2025, 6, 26, 13, 0),
                    end=_ist(2025, 6, 26, 23, 59),
                )

    def test_sub_minute_start_past_the_last_1minute_label_skips(
        self, lake: Path, tmp_path: Path
    ) -> None:
        # A sub-minute --start: [15:29:30 → 23:59] overlaps the session but
        # sits past the last 1minute label (15:29) — zero bars is EXPECTED.
        with Catalog.open(tmp_path / "cat.duckdb") as cat:
            _futures_lake(lake, cat)
            with capture_logs() as logs:
                out = _load_alias(
                    cat,
                    lake,
                    start=datetime(2025, 6, 26, 15, 29, 30, tzinfo=IST),
                    end=_ist(2025, 6, 26, 23, 59),
                )
        assert out == []
        assert len(_skip_warns(logs)) == 1

    def test_intra_bucket_sliver_between_grid_labels_holds_no_bar(self) -> None:
        # [09:30 → 10:00] on a 4hour grid: the next label (13:15) is past the
        # fragment end — no stored bar timestamp can fall inside.
        assert not series_load._expected_bar_time_in_window(
            interval="4hour",
            seg_start=_ist(2025, 6, 26, 9, 30),
            seg_end=_ist(2025, 6, 26, 10, 0),
        )

    def test_grid_label_at_the_fragment_start_counts(self) -> None:
        # A fragment STARTING exactly on a grid label holds that label.
        assert series_load._expected_bar_time_in_window(
            interval="4hour",
            seg_start=_ist(2025, 6, 26, 13, 15),
            seg_end=_ist(2025, 6, 26, 13, 15),
        )

    def test_unknown_interval_label_abstains_to_the_loud_refusal(self) -> None:
        # HONEST abstain: a non-lake TF label has no modellable grid — the
        # helper claims a bar is expectable so the caller KEEPS its refusal.
        assert series_load._expected_bar_time_in_window(
            interval="42minute",
            seg_start=_ist(2025, 6, 26, 0, 0),
            seg_end=_ist(2025, 6, 26, 23, 59),
        )


# ────────────────────────────────────────────────────────────────────────
# F12 — snapshot sensitivity (Golden Rule #4)
# ────────────────────────────────────────────────────────────────────────


class TestSnapshotSensitivity:
    """Two lakes differing by ONE bar mint DIFFERENT run-identity preimages."""

    def test_one_changed_bar_moves_the_data_snapshot_hash(self, tmp_path: Path) -> None:
        from brutex.engine.stage3_wire import data_snapshot_hash

        def _hash_for(lake: Path, cat_path: Path, *, jul_close: float) -> str:
            with Catalog.open(cat_path) as cat:
                _seed(lake, cat, symbol=_JUN, bars=[_bar(_ist(2025, 6, 26), 101.0)])
                _seed(lake, cat, symbol=_JUL, bars=[_bar(_ist(2025, 6, 27), jul_close)])
                bars = _load_alias(
                    cat, lake, start=_ist(2025, 6, 26, 0, 0), end=_ist(2025, 6, 27, 23, 59)
                )
            return data_snapshot_hash(
                exchange="NSE",
                segment="FNO",
                start="2025-06-26",
                end="2025-06-27",
                slices={(_ALIAS, "1minute"): bars},
            )

        hash_a = _hash_for(tmp_path / "lake_a", tmp_path / "a.duckdb", jul_close=200.0)
        hash_b = _hash_for(tmp_path / "lake_b", tmp_path / "b.duckdb", jul_close=200.05)
        hash_c = _hash_for(tmp_path / "lake_c", tmp_path / "c.duckdb", jul_close=200.0)
        assert hash_a != hash_b  # one bar's close moved → new identity
        assert hash_a == hash_c  # determinism control: same bytes, same id


# ────────────────────────────────────────────────────────────────────────
# Loader-cut / roll-cursor per-bar agreement (the ONE-schedule consistency pin)
# ────────────────────────────────────────────────────────────────────────


class TestLoaderCutCursorAgreement:
    """The loader's IST-date cut and stage1a's cursor agree on every bar's owner."""

    def test_per_bar_owner_agreement_over_a_roll_boundary(self, lake: Path, tmp_path: Path) -> None:
        from brutex.engine.stage1a import Stage1AMatrixBuilder

        with Catalog.open(tmp_path / "cat.duckdb") as cat:
            _futures_lake(lake, cat)
            res = resolve_futures_series(lake, start=date(2025, 6, 25), end=date(2025, 7, 2))
            bars = _load_alias(cat, lake, resolution=res)
        builder = Stage1AMatrixBuilder(roll_schedule=res.roll_schedule)
        for bar in bars:
            ist_day = bar.timestamp.astimezone(IST).date()
            loader_owner = next(
                seg for seg in res.segments if seg.own_start <= ist_day <= seg.own_end
            )
            builder.push(bar)
            # Whitebox friend-access to the cursor's per-bar key (the only
            # observable of the roll cursor between pushes).
            cursor_key = builder._current_session_key  # pyright: ignore[reportPrivateUsage]
            # The cursor's key IS the owning expiry ordinal the loader cut by …
            assert cursor_key == loader_owner.expiry.toordinal()
            # … which is exactly the resolve_near_month >= law restated.
            assert cursor_key == min(
                ordinal for ordinal in res.roll_schedule if ordinal >= ist_day.toordinal()
            )
